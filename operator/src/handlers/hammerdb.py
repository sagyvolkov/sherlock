"""
handlers/hammerdb.py — SherlockSuite handler.

Follows the same pattern as handlers/pgbench.py.
Benchmark-specific: command construction and result parsing.
All orchestration is in handlers/base.py.
"""

import logging
import re
import shlex

import kopf

from src.handlers.base import (
    initialize_suite, deploy_db_infrastructure, teardown_db_suite,
    run_next_benchmark,
    BENCHMARK_IMAGE,
    on_benchmark_job_event, on_suite_timer,
)
from src.parsers.benchmarks import parse_hammerdb
from src.utils.credentials import resolve as resolve_credentials

logger = logging.getLogger(__name__)

GROUP   = "sherlock.io"
VERSION = "v1alpha1"
KIND = "SherlockHammerDBSuite"
PLURAL  = "sherlockhammerdbsuites"


SQLCMD = "/opt/mssql-tools/bin/sqlcmd"


@kopf.on.create(GROUP, VERSION, KIND)
def on_create(name, namespace, spec, patch, body, **kwargs):
    logger.info(f"[hammerdb] Suite {name} created in {namespace}")
    initialize_suite(name, namespace, spec, patch, suite_kind=KIND)
    db_type = spec.get("database", {}).get("type", "mssql")   # CRD allows only mssql
    secret_name = spec.get("database", {}).get("credentialsSecret")
    credentials = resolve_credentials(db_type, namespace, secret_name)
    deploy_db_infrastructure(name, namespace, spec, patch, credentials,
                             owner=body, suite_kind=KIND)
    patch.status["phase"] = "Ready"


@kopf.on.field(GROUP, VERSION, KIND, field='status.phase')
def on_phase_change(name, namespace, spec, status, patch, old, new, body, **kwargs):
    if new == "Ready":
        run_next_benchmark(
            suite_name=name, namespace=namespace, spec=spec, status=status,
            patch=patch, build_benchmark_command=_build_command,
            parse_results=parse_hammerdb, benchmark_image=BENCHMARK_IMAGE,
            suite_kind=KIND,
            owner=body,
            build_prepare_command=_build_prepare_command,
        )


@kopf.on.event('batch', 'v1', 'jobs',
               labels={f'{GROUP}/suite-kind': KIND})
def on_job_event(type, name, namespace, status, meta, **kwargs):
    """A benchmark Job changed; finish its run if it was the last one."""
    on_benchmark_job_event(
        event_type=type, job_name=name, namespace=namespace,
        job_meta=meta, job_status=status,
        plural=PLURAL, parse_results=parse_hammerdb,
    )


@kopf.timer(GROUP, VERSION, KIND, interval=10.0,
            when=lambda status, **_: (status or {}).get("phase") in ("Running", "Sleeping"))
def on_timer(name, namespace, status, patch, **kwargs):
    """Wake up after sleepBetweenRuns; back-stop for missed Job events."""
    on_suite_timer(name, namespace, status, patch,
                   plural=PLURAL, parse_results=parse_hammerdb)


@kopf.on.delete(GROUP, VERSION, KIND)
def on_delete(name, namespace, spec, status, meta, **kwargs):
    """Database pods go with the suite; PVCs per database.volumes.onSuiteDelete."""
    teardown_db_suite(name, namespace, spec, status, meta["uid"])


def _build_command(params: dict, db_ip: str) -> list[str]:
    """
    Run the timed TPC-C driver: rampupMins of warm-up, then testMins measured.

    The TCL is generated here rather than from the image's run.template,
    which hardcoded the database name and ignored allWarehouses.
    """
    rampup    = params.get("rampupMins", 2)
    test_mins = params.get("testMins", 5)
    # Back-stop in case the timed driver never reports completion
    timer_secs = (rampup + test_mins) * 60 + 180
    tcl = _connection_tcl(params, db_ip) + f"""
diset tpcc mssqls_dbase {_tcl_quote(params.get('_dbName', 'tpcc'))}
diset tpcc mssqls_driver timed
diset tpcc mssqls_rampup {rampup}
diset tpcc mssqls_duration {test_mins}
diset tpcc mssqls_allwarehouse {_tcl_bool(params.get('allWarehouses', False))}
diset tpcc mssqls_timeprofile false
diset tpcc mssqls_raiseerror false
vuset logtotemp 1
loadscript
vuset vu {params.get('virtualUsers', 8)}
vucreate
vurun
set x 0
while {{![vucomplete] && $x < {timer_secs}}} {{
  incr x
  after 1000
  update
  if {{$x % 60 == 0}} {{ puts "Elapsed ${{x}}s" }}
}}
vudestroy
after 5000
puts "Run completes..."
"""
    return ["bash", "-c", _run_tcl_script(tcl)]


def _build_prepare_command(params: dict, db_ip: str) -> list[str]:
    """
    Build the TPC-C schema for this run's warehouse count.

    Generating the data is CPU bound, so one loader virtual user runs per
    client CPU (execution.clientResources limits.cpu), at most one per
    warehouse. HammerDB refuses to build into a database that already has
    tables, so the database is dropped first. hammerdbcli exits 0 even when the build
    fails, so the warehouse count is checked afterwards.
    base skips all of this when the volumes already hold the warehouses.
    """
    warehouses = params.get("warehouses", 100)
    db_name = params.get("_dbName", "tpcc")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", db_name):
        raise ValueError(f"Unsupported MSSQL database name {db_name!r}")
    tcl = _connection_tcl(params, db_ip) + f"""
diset tpcc mssqls_dbase {_tcl_quote(db_name)}
diset tpcc mssqls_count_ware {warehouses}
diset tpcc mssqls_num_vu {min(warehouses, params.get('_clientCpus', 1))}
buildschema
while {{![vucomplete]}} {{ after 5000; update }}
"""
    sqlcmd = (f"{SQLCMD} -b -S {shlex.quote(db_ip + ',1433')} -U sa "
              f"-P {shlex.quote(params['_saPassword'])}")
    drop = (f"IF DB_ID('{db_name}') IS NOT NULL BEGIN "
            f"ALTER DATABASE [{db_name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; "
            f"DROP DATABASE [{db_name}]; END")
    script = f"""set -euo pipefail
# SQL Server accepts TCP before it accepts logins; wait for a login to work
for i in $(seq 1 60); do
  {sqlcmd} -Q "SELECT 1" >/dev/null 2>&1 && break
  echo "waiting for SQL Server ($i)"; sleep 5
done
{sqlcmd} -Q {shlex.quote(drop)}
{_run_tcl_script(tcl)}
count=$({sqlcmd} -d {db_name} -h -1 -W -Q "SET NOCOUNT ON; SELECT COUNT(*) FROM dbo.warehouse" | tr -d '[:space:]')
if [ "$count" != "{warehouses}" ]; then
  echo "SCHEMA BUILD FAILED: expected {warehouses} warehouses, found '$count'"
  exit 1
fi
echo "SCHEMA VERIFIED: {warehouses} warehouses"
"""
    return ["bash", "-c", script]


def _connection_tcl(params: dict, db_ip: str) -> str:
    """HammerDB settings to reach SQL Server on Linux as sa over TCP."""
    return f"""dbset db mssqls
dbset bm TPC-C
diset connection mssqls_tcp true
diset connection mssqls_server {_tcl_quote(db_ip)}
diset connection mssqls_linux_server {_tcl_quote(db_ip)}
diset connection mssqls_port 1433
diset connection mssqls_linux_authent sql
diset connection mssqls_uid sa
diset connection mssqls_pass {_tcl_quote(params['_saPassword'])}"""


def _run_tcl_script(tcl: str) -> str:
    """Shell lines that write tcl to a file and run it with hammerdbcli."""
    return ("cat > /tmp/sherlock.tcl <<'SHERLOCK_TCL'\n"
            f"{tcl}\nSHERLOCK_TCL\n"
            "cd /hammerdb && ./hammerdbcli auto /tmp/sherlock.tcl")


def _tcl_quote(value) -> str:
    """Quote a value as one TCL word, with no substitution."""
    return '"' + re.sub(r'([\\"$\[\]{}])', r'\\\1', str(value)) + '"'


def _tcl_bool(value) -> str:
    return "true" if value else "false"
