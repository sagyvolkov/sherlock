"""
tests/test_parsers.py — Unit tests for benchmark result parsers.

Tests parse_pgbench, parse_sysbench, parse_hammerdb, parse_ycsb, parse_fio
and the base iostat/vmstat parsers.

Each test uses realistic output captured from real benchmark runs.

Run with: python -m pytest operator/tests/test_parsers.py -v
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json

import pytest
from src.parsers.benchmarks import (
    parse_pgbench, parse_sysbench, parse_hammerdb,
    parse_ycsb, parse_fio,
)
from src.parsers.base import (
    parse_iostat_devices, parse_ifconfig_deltas, parse_vmstat, parse_storage_stats,
)


# ── pgbench ───────────────────────────────────────────────────────────────────

PGBENCH_OUTPUT = """
pgbench (PostgreSQL) 14.5
starting vacuum...end.
transaction type: <builtin: TPC-B (sort of)>
scaling factor: 100
query mode: simple
number of clients: 8
number of threads: 4
duration: 120 s
number of transactions actually processed: 123456
latency average = 7.804 ms
latency stddev = 3.221 ms
tps = 1028.786482 (including connections establishing)
tps = 1029.123456 (excluding connections establishing)
"""

class TestParsePgbench:

    def test_tps_extracted(self):
        result = parse_pgbench(PGBENCH_OUTPUT, {})
        assert "tps" in result
        assert result["tps"] == pytest.approx(1029.12, abs=0.1)

    def test_uses_excluding_connections_tps(self):
        """Should prefer 'excluding connections establishing' TPS."""
        result = parse_pgbench(PGBENCH_OUTPUT, {})
        # excluding = 1029.12, including = 1028.79 — should use excluding
        assert result["tps"] > 1029.0

    def test_latency_avg_extracted(self):
        result = parse_pgbench(PGBENCH_OUTPUT, {})
        assert result.get("latencyAvgMs") == pytest.approx(7.804, abs=0.001)

    def test_latency_stddev_extracted(self):
        result = parse_pgbench(PGBENCH_OUTPUT, {})
        assert result.get("latencyStddevMs") == pytest.approx(3.221, abs=0.001)

    def test_empty_output_returns_empty_dict(self):
        result = parse_pgbench("", {})
        assert result == {}

    def test_multiple_db_instances_tps_summed(self):
        """When multiple DB instances run, their TPS values are summed."""
        multi = PGBENCH_OUTPUT + PGBENCH_OUTPUT  # two instances
        result = parse_pgbench(multi, {})
        assert result["tps"] == pytest.approx(2 * 1029.12, abs=0.5)

    def test_ansi_codes_stripped(self):
        """ANSI escape codes in output don't break the parser."""
        ansi_output = "\x1b[32m" + PGBENCH_OUTPUT + "\x1b[0m"
        result = parse_pgbench(ansi_output, {})
        assert "tps" in result


# ── sysbench ──────────────────────────────────────────────────────────────────

SYSBENCH_OUTPUT = """
sysbench 1.0.20 (using bundled LuaJIT 2.1.0-beta2)

Running the test with following options:
Number of threads: 4

SQL statistics:
    queries performed:
        read:                            1234567
        write:                            345678
        other:                             23456
        total:                           1603701
    transactions:                        11780  (98.16 per sec.)
    queries:                            1603701 (13358.20 per sec.)
    ignored errors:                          0  (0.00 per sec.)
    reconnects:                              0  (0.00 per sec.)

Throughput:
    events/s (eps):                     98.1567
    time elapsed:                      120.0016s
    total number of events:              11780

Latency (ms):
         min:                                 0.58
         avg:                                40.74
         max:                               145.23
         95th percentile:                    65.65
         sum:                            480144.24

Threads fairness:
    events (avg/stddev):           2945.0000/14.61
    execution time (avg/stddev):   120.0361/0.01
"""

class TestParseSysbench:

    def test_tps_extracted(self):
        result = parse_sysbench(SYSBENCH_OUTPUT, {})
        assert "tps" in result
        assert result["tps"] == pytest.approx(98.16, abs=0.01)

    def test_qps_extracted(self):
        result = parse_sysbench(SYSBENCH_OUTPUT, {})
        assert "qps" in result
        assert result["qps"] == pytest.approx(13358.20, abs=0.1)

    def test_avg_latency_extracted(self):
        result = parse_sysbench(SYSBENCH_OUTPUT, {})
        assert result.get("latencyAvgMs") == pytest.approx(40.74, abs=0.01)

    def test_p95_latency_extracted(self):
        result = parse_sysbench(SYSBENCH_OUTPUT, {})
        assert result.get("latencyP95Ms") == pytest.approx(65.65, abs=0.01)

    def test_max_latency_extracted(self):
        result = parse_sysbench(SYSBENCH_OUTPUT, {})
        assert result.get("latencyMaxMs") == pytest.approx(145.23, abs=0.01)

    def test_empty_output_returns_empty_dict(self):
        result = parse_sysbench("", {})
        assert result == {}


# ── HammerDB ──────────────────────────────────────────────────────────────────

HAMMERDB_OUTPUT = """
Vuser 1:RUNNING
Vuser 2:RUNNING
Vuser 3:RUNNING
TEST RESULT : System achieved 45231 NOPM from 125678 SQL Server TPM
Vuser 1:FINISHED SUCCESS
Vuser 2:FINISHED SUCCESS
"""

HAMMERDB_MULTI_OUTPUT = """
TEST RESULT : System achieved 45231 NOPM from 125678 SQL Server TPM
TEST RESULT : System achieved 48102 NOPM from 131445 SQL Server TPM
"""

class TestParseHammerDB:

    def test_nopm_extracted(self):
        result = parse_hammerdb(HAMMERDB_OUTPUT, {})
        assert "nopm" in result
        assert result["nopm"] == pytest.approx(45231.0)

    def test_tpm_extracted(self):
        result = parse_hammerdb(HAMMERDB_OUTPUT, {})
        assert "tpm" in result
        assert result["tpm"] == pytest.approx(125678.0)

    def test_multiple_instances_averaged(self):
        """Multiple 'System achieved' lines are averaged."""
        result = parse_hammerdb(HAMMERDB_MULTI_OUTPUT, {})
        assert result["nopm"] == pytest.approx((45231 + 48102) / 2, abs=0.1)
        assert result["tpm"]  == pytest.approx((125678 + 131445) / 2, abs=0.1)

    def test_comma_in_numbers(self):
        """Large numbers with commas (e.g. 1,234,567) are parsed correctly."""
        output = "System achieved 1,234,567 NOPM from 3,456,789 SQL Server TPM"
        result = parse_hammerdb(output, {})
        assert result["nopm"] == pytest.approx(1234567.0)
        assert result["tpm"]  == pytest.approx(3456789.0)

    def test_no_result_line_returns_empty(self):
        result = parse_hammerdb("VUser 1: RUNNING\nVUser 1: FINISHED", {})
        assert result == {}


# ── YCSB ─────────────────────────────────────────────────────────────────────

YCSB_OUTPUT = """
2024-01-15 10:23:45:123 0 sec: 0 operations; est completion in 0 second
[OVERALL], RunTime(ms), 120000
[OVERALL], Throughput(ops/sec), 12345.67
[READ], Operations, 617283
[READ], AverageLatency(us), 234.56
[READ], MinLatency(us), 45
[READ], MaxLatency(us), 12345
[READ], 95thPercentileLatency(us), 456.78
[READ], 99thPercentileLatency(us), 789.01
[UPDATE], Operations, 382717
[UPDATE], AverageLatency(us), 345.67
[UPDATE], MinLatency(us), 67
[UPDATE], MaxLatency(us), 23456
[UPDATE], 95thPercentileLatency(us), 567.89
[UPDATE], 99thPercentileLatency(us), 890.12
"""

class TestParseYCSB:

    def test_throughput_extracted(self):
        result = parse_ycsb(YCSB_OUTPUT, {})
        assert "throughputOpsPerSec" in result
        assert result["throughputOpsPerSec"] == pytest.approx(12345.67)

    def test_read_latency_extracted(self):
        result = parse_ycsb(YCSB_OUTPUT, {})
        assert result.get("readLatencyAvgUs") == pytest.approx(234.56)
        assert result.get("readLatencyP95Us") == pytest.approx(456.78)
        assert result.get("readLatencyP99Us") == pytest.approx(789.01)

    def test_update_latency_extracted(self):
        result = parse_ycsb(YCSB_OUTPUT, {})
        assert result.get("updateLatencyAvgUs") == pytest.approx(345.67)
        assert result.get("updateLatencyP95Us") == pytest.approx(567.89)
        assert result.get("updateLatencyP99Us") == pytest.approx(890.12)

    def test_empty_output_returns_empty_dict(self):
        result = parse_ycsb("", {})
        assert result == {}


# ── fio ───────────────────────────────────────────────────────────────────────

FIO_JSON_OUTPUT = """
{
  "fio version" : "fio-3.33",
  "timestamp" : 1705312800,
  "jobs" : [
    {
      "jobname" : "randrw",
      "read" : {
        "io_bytes" : 4294967296,
        "io_kbytes" : 4194304,
        "bw" : 34952,
        "iops" : 8738.23,
        "slat_ns" : {"min" : 2000, "max" : 90000, "mean" : 11428.57, "stddev" : 3000.0},
        "clat_ns" : {
          "min" : 12345,
          "max" : 9876543,
          "mean" : 228571.43,
          "stddev" : 45678.9,
          "percentile" : {
            "1.000000" : 145408,
            "5.000000" : 167936,
            "10.000000" : 183296,
            "20.000000" : 204800,
            "30.000000" : 221184,
            "40.000000" : 233472,
            "50.000000" : 245760,
            "60.000000" : 260096,
            "70.000000" : 274432,
            "80.000000" : 296960,
            "90.000000" : 339968,
            "95.000000" : 385024,
            "99.000000" : 520192,
            "99.500000" : 634880,
            "99.900000" : 1011712,
            "99.950000" : 1220608,
            "99.990000" : 2179072
          }
        },
        "lat_ns" : {"min" : 14345, "max" : 9880000, "mean" : 240000.0, "stddev" : 45700.0}
      },
      "write" : {
        "io_bytes" : 1431306240,
        "io_kbytes" : 1397760,
        "bw" : 11648,
        "iops" : 2912.08,
        "clat_ns" : {
          "min" : 23456,
          "max" : 8765432,
          "mean" : 342857.14,
          "stddev" : 67890.1,
          "percentile" : {
            "50.000000" : 323584,
            "95.000000" : 602112,
            "99.000000" : 798720,
            "99.900000" : 1515520
          }
        },
        "lat_ns" : {"min" : 25456, "max" : 8770000, "mean" : 360000.0, "stddev" : 67900.0}
      }
    }
  ]
}
"""

# Real fio 3.37 output (128k sequential read, --group_reporting) captured
# from a run on OCP with Lightbits NVMe-oF
FIO_OCP_OUTPUT = open(os.path.join(os.path.dirname(__file__), "fixtures",
                                   "fio-3.37-read-128k-ocp.json")).read()


def _fio_job(read_iops, clat_mean, p99, write_iops=0.0):
    """Minimal fio job entry with read clat percentiles."""
    return {
        "read": {"iops": read_iops, "bw": 1000,
                 "clat_ns": {"mean": clat_mean,
                             "percentile": {"50.000000": clat_mean, "99.000000": p99}},
                 "lat_ns": {"mean": clat_mean + 10000}},
        "write": {"iops": write_iops, "bw": 0,
                  "clat_ns": {"mean": 0, "N": 0}, "lat_ns": {"mean": 0, "N": 0}},
    }


FIO_NORMAL_OUTPUT = """
randrw: (g=0): rw=randrw, bs=(R) 4096B-4096B, (W) 4096B-4096B, (T) 4096B-4096B, ioengine=libaio, iodepth=4
fio-3.33
Starting 4 processes

randrw: (groupid=0, jobs=4): err= 0: pid=1234: Mon Jan 15 10:30:00 2024
  read: IOPS=8738, BW=34.1MiB/s (35.8MB/s)(4096MiB/120001msec)
  write: IOPS=2912, BW=11.4MiB/s (11.9MB/s)(1365MiB/120001msec)
"""

class TestParseFio:

    def test_json_read_iops(self):
        result = parse_fio(FIO_JSON_OUTPUT, {})
        assert "readIops" in result
        assert result["readIops"] == pytest.approx(8738.23, abs=0.1)

    def test_json_write_iops(self):
        result = parse_fio(FIO_JSON_OUTPUT, {})
        assert result["writeIops"] == pytest.approx(2912.08, abs=0.1)

    def test_json_total_iops(self):
        result = parse_fio(FIO_JSON_OUTPUT, {})
        assert result["totalIops"] == pytest.approx(8738.23 + 2912.08, abs=0.5)

    def test_json_bandwidth_mb(self):
        result = parse_fio(FIO_JSON_OUTPUT, {})
        # 34952 kB/s → ~34.1 MB/s
        assert result.get("readBwMBs") == pytest.approx(34952 / 1024, abs=0.1)

    def test_json_read_latency_percentiles(self):
        result = parse_fio(FIO_JSON_OUTPUT, {})
        # p95 from JSON: 385024 ns → 385.024 us
        assert result.get("readLatP95Us") == pytest.approx(385.024, abs=1.0)
        # p99: 520192 ns → 520.192 us
        assert result.get("readLatP99Us") == pytest.approx(520.192, abs=1.0)
        # p99.9: 1011712 ns → 1011.712 us
        assert result.get("readLatP999Us") == pytest.approx(1011.712, abs=1.0)

    def test_json_avg_latency_uses_same_source_as_percentiles(self):
        # Percentiles exist only under clat_ns, so the average is clat too
        result = parse_fio(FIO_JSON_OUTPUT, {})
        assert result["readLatAvgUs"] == pytest.approx(228.57, abs=0.01)
        assert result["writeLatAvgUs"] == pytest.approx(342.86, abs=0.01)
        assert result["writeLatP99Us"] == pytest.approx(798.72, abs=0.01)

    def test_json_prefers_total_latency_when_it_has_percentiles(self):
        # fio --lat_percentiles=1 puts percentiles under lat_ns
        data = json.loads(FIO_JSON_OUTPUT)
        data["jobs"][0]["read"]["lat_ns"]["percentile"] = {"99.000000": 600000}
        result = parse_fio(json.dumps(data), {})
        assert result["readLatP99Us"] == pytest.approx(600.0)
        assert result["readLatAvgUs"] == pytest.approx(240.0)

    def test_json_real_ocp_output(self):
        result = parse_fio(FIO_OCP_OUTPUT, {})
        assert result["readIops"] == pytest.approx(13761.02, abs=0.01)
        assert result["readLatAvgUs"] == pytest.approx(1117.1, abs=0.1)
        assert result["readLatP50Us"] > 0
        assert result["readLatP95Us"] <= result["readLatP99Us"] <= result["readLatP999Us"]
        # No writes in a pure read run: no write latency keys
        assert not any(k.startswith("writeLat") for k in result)

    def test_json_multiple_jobs_weight_avg_and_take_worst_percentile(self):
        data = {"jobs": [_fio_job(3000, 100000, 400000),
                         _fio_job(1000, 200000, 900000)]}
        result = parse_fio(json.dumps(data), {})
        assert result["readIops"] == pytest.approx(4000)
        # (3000*100 + 1000*200) / 4000 = 125 us
        assert result["readLatAvgUs"] == pytest.approx(125.0)
        assert result["readLatP99Us"] == pytest.approx(900.0)

    def test_normal_format_fallback(self):
        """Falls back to normal format parsing when JSON not present."""
        result = parse_fio(FIO_NORMAL_OUTPUT, {})
        assert "readIops" in result
        assert result["readIops"] == pytest.approx(8738.0, abs=1.0)
        assert result["writeIops"] == pytest.approx(2912.0, abs=1.0)

    def test_json_takes_priority_over_normal(self):
        """Mixed output: JSON block should be used, not normal text."""
        mixed = FIO_NORMAL_OUTPUT + "\n" + FIO_JSON_OUTPUT
        result = parse_fio(mixed, {})
        # JSON gives fractional IOPS; normal text gives integer — check for fraction
        assert result["readIops"] == pytest.approx(8738.23, abs=0.1)


# ── stats collector parser ────────────────────────────────────────────────────

_HDR = ("Device            r/s     rMB/s   rrqm/s  %rrqm r_await rareq-sz     w/s     wMB/s"
        "   wrqm/s  %wrqm w_await wareq-sz     d/s     dMB/s   drqm/s  %drqm d_await dareq-sz"
        "     f/s f_await  aqu-sz  %util")


def _dev(name, r_s, r_mb, r_await, w_s, w_mb, w_await, util):
    return (f"{name} {r_s} {r_mb} 0.00 0.00 {r_await} 4.00 {w_s} {w_mb} 0.00 0.00 {w_await}"
            f" 4.00 0.00 0.00 0.00 0.00 0.00 0.00 0.00 0.00 1.00 {util}")


def _ifconfig(prefix, rx, tx):
    return "\n".join(f"{prefix}{l}" for l in [
        "bond0.100: flags=4163<UP,BROADCAST,RUNNING,MULTICAST>  mtu 9000",
        "        inet 10.205.0.111  netmask 255.255.255.0  broadcast 10.205.0.255",
        f"        RX packets 100  bytes {rx} (1.0 GB)",
        "        RX errors 0  dropped 0  overruns 0  frame 0",
        f"        TX packets 200  bytes {tx} (2.0 GB)",
        "",
        "lo: flags=73<UP,LOOPBACK,RUNNING>  mtu 65536",
        "        RX packets 5  bytes 1000 (1.0 KB)",
        "        TX packets 5  bytes 1000 (1.0 KB)",
    ])


# Two 10s samples as the stats container prints them (iostat -dtxmyz):
# nvme0n1 is a multipath head, nvme0c0n1 its path (same I/O), sda the root disk
STATS_LOG = "\n".join(f"IOSTAT-{l}" for l in [
    "Linux 5.14.0-570 (worker1.lbits) \t10/14/26 \t_x86_64_\t(48 CPU)", "",
    "10/14/26 18:56:13", _HDR,
    _dev("nvme0c0n1", "1000.00", "4.00", "0.50", "500.00", "2.00", "1.00", "60.00"),
    _dev("nvme0n1",   "1000.00", "4.00", "0.50", "500.00", "2.00", "1.00", "60.00"),
    _dev("sda",       "0.00", "0.00", "0.00", "30.00", "0.10", "0.10", "0.10"), "",
    "10/14/26 18:56:23", _HDR,
    _dev("nvme0c0n1", "3000.00", "12.00", "0.70", "0.00", "0.00", "0.00", "80.00"),
    _dev("nvme0n1",   "3000.00", "12.00", "0.70", "0.00", "0.00", "0.00", "80.00"),
]) + "\n" + "\n".join(f"VMSTAT-{l}" for l in [
    " r  b   swpd   free   buff  cache   si   so    bi    bo   in   cs us sy id wa st",
    " 2  0      0 1234567  12345 678901    0    0     0  1234 5678 9012 15  5 75  4  0",
    " 3  1      0 1234100  12500 679000    0    0     0  2345 6789  100 18  6 68  8  0",
]) + "\nDATE.BEFORE-1791990000\n" + _ifconfig("IFCONFIG.BEFORE-", 1_000_000, 2_000_000) \
   + "\nDATE.AFTER-1791990030\n" + _ifconfig("IFCONFIG.AFTER-", 11_485_760, 106_857_600)


class TestParseIostatDevices:

    def test_totals_are_rate_times_sample_length(self):
        devs = parse_iostat_devices(STATS_LOG)
        nvme = devs["nvme0n1"]
        # 4 MB/s * 10s + 12 MB/s * 10s; 1000/s * 10s + 3000/s * 10s
        assert nvme["readMiB"] == pytest.approx(160.0)
        assert nvme["readIos"] == pytest.approx(40000)
        assert nvme["writeMiB"] == pytest.approx(20.0)
        assert nvme["writeIos"] == pytest.approx(5000)
        # I/O-weighted: (0.5*10000 + 0.7*30000) / 40000
        assert nvme["readAwaitMs"] == pytest.approx(0.65)
        assert nvme["maxUtilPct"] == 80.0

    def test_columns_found_by_header_in_old_kb_layout(self):
        old = ("Device r/s w/s rkB/s wkB/s rrqm/s wrqm/s %rrqm %wrqm r_await w_await "
               "aqu-sz rareq-sz wareq-sz %util\n"
               "nvme0n1 100.00 50.00 2048.00 1024.00 0 0 0 0 0.40 0.90 0.1 20 20 30.00\n")
        nvme = parse_iostat_devices(old, interval=10)["nvme0n1"]
        assert nvme["readMiB"] == pytest.approx(20.0)     # 2 MB/s * 10s
        assert nvme["writeIos"] == pytest.approx(500)

    def test_empty(self):
        assert parse_iostat_devices("") == {}


class TestParseStorageStats:

    def test_counts_multipath_heads_only_and_rates_over_runtime(self):
        r = parse_storage_stats({"worker1": STATS_LOG}, runtime_secs=20,
                                network_interfaces=["bond0.100"])
        # nvme0c0n1 (path) and sda (root disk) excluded by the default pattern
        assert r["storageReadMiB"] == pytest.approx(160.0)
        assert r["storageWriteMiB"] == pytest.approx(20.0)
        assert r["storageReadMiBs"] == pytest.approx(8.0)
        assert r["storageReadIops"] == pytest.approx(2000.0)
        assert set(r["storageByNode"]["worker1"]["devices"]) == {"nvme0n1"}
        # ifconfig deltas: 10 MiB received, 100 MiB sent
        assert r["netRxMiB"] == pytest.approx(10.0)
        assert r["netTxMiB"] == pytest.approx(100.0)
        assert r["storageByNode"]["worker1"]["network"]["lo"] == {"rxBytes": 0, "txBytes": 0}
        assert r["avgIoWaitPct"] == pytest.approx(6.0)

    def test_nodes_are_summed(self):
        r = parse_storage_stats({"w1": STATS_LOG, "w2": STATS_LOG}, runtime_secs=20)
        assert r["storageReadMiB"] == pytest.approx(320.0)
        assert "netRxMiB" not in r      # no interfaces selected

    def test_device_pattern_override(self):
        r = parse_storage_stats({"w1": STATS_LOG}, runtime_secs=20, device_pattern=r"^sda$")
        assert r["storageWriteMiB"] == pytest.approx(1.0)
        assert r["storageReadMiB"] == 0


def test_ifconfig_deltas():
    d = parse_ifconfig_deltas(STATS_LOG)
    assert d["bond0.100"] == {"rxBytes": 10_485_760, "txBytes": 104_857_600}


# ── vmstat parser ─────────────────────────────────────────────────────────────

VMSTAT_OUTPUT = """
procs -----------memory---------- ---swap-- -----io---- -system-- ------cpu-----
 r  b   swpd   free   buff  cache   si   so    bi    bo   in   cs us sy id wa st
 2  0      0 1234567  12345 678901    0    0     0  1234 5678 9012 15  5 75  5  0
 3  1      0 1234100  12500 679000    0    0     0  2345 6789  100 18  6 68  8  0
 1  0      0 1235000  12400 678500    0    0     0   987 4567 8901 12  4 81  3  0
"""

class TestParseVmstat:

    def test_iowait_averaged(self):
        result = parse_vmstat(VMSTAT_OUTPUT)
        assert "avgIoWaitPct" in result
        # (5 + 8 + 3) / 3 = 5.33
        assert result["avgIoWaitPct"] == pytest.approx(5.33, abs=0.01)

    def test_max_iowait(self):
        result = parse_vmstat(VMSTAT_OUTPUT)
        assert result.get("maxIoWaitPct") == pytest.approx(8.0)

    def test_empty_output_returns_empty_dict(self):
        result = parse_vmstat("")
        assert result == {}


def test_stats_settings_and_count():
    from src.plugins.storage.generic import stats_settings, stats_count
    assert stats_settings({}) == (10, 30)
    assert stats_settings({"statsIntervalSeconds": 5, "statsMarginSeconds": 0}) == (5, 0)
    # 600s run, 10s samples, 30s margin → 63 samples; 5s/0s → 120
    assert stats_count(600, 10, 30) == 63
    assert stats_count(600, 5, 0) == 120
    assert stats_count(125, 10, 0) == 13          # rounds up
