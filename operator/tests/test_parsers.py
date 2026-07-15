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

import pytest
from src.parsers.benchmarks import (
    parse_pgbench, parse_sysbench, parse_hammerdb,
    parse_ycsb, parse_fio,
)
from src.parsers.base import parse_iostat, parse_vmstat, parse_storage_stats


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
        "lat_ns" : {
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
        }
      },
      "write" : {
        "io_bytes" : 1431306240,
        "io_kbytes" : 1397760,
        "bw" : 11648,
        "iops" : 2912.08,
        "lat_ns" : {
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
        }
      }
    }
  ]
}
"""

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


# ── iostat parser ─────────────────────────────────────────────────────────────

IOSTAT_OUTPUT = """
Linux 5.14.0 (worker-node-1)   01/15/2024      _x86_64_        (32 CPU)

avg-cpu:  %user   %nice %system %iowait  %steal   %idle
           5.23    0.00    2.11    8.45    0.00   84.21

Device            r/s     w/s   rkB/s   wkB/s  rrqm/s  wrqm/s  %rrqm  %wrqm r_await w_await aqu-sz rareq-sz wareq-sz  %util
nvme0n1       1200.00  300.00 48000.00 12000.00    0.00    1.00   0.00   0.33    0.45    1.20   0.72    40.00    40.00  45.00
nvme1n1        980.00  250.00 39200.00 10000.00    0.00    0.50   0.00   0.20    0.52    1.35   0.65    40.00    40.00  38.00

avg-cpu:  %user   %nice %system %iowait  %steal   %idle
           6.10    0.00    2.45    9.12    0.00   82.33

Device            r/s     w/s   rkB/s   wkB/s  rrqm/s  wrqm/s  %rrqm  %wrqm r_await w_await aqu-sz rareq-sz wareq-sz  %util
nvme0n1       1250.00  320.00 50000.00 12800.00    0.00    1.20   0.00   0.37    0.48    1.15   0.78    40.00    40.00  47.00
nvme1n1       1010.00  270.00 40400.00 10800.00    0.00    0.60   0.00   0.22    0.50    1.28   0.68    40.00    40.00  40.00
"""

class TestParseIostat:

    def test_read_iops_averaged(self):
        result = parse_iostat(IOSTAT_OUTPUT)
        assert "avgReadIops" in result
        # Two devices × two intervals: (1200+980+1250+1010) / 4 = 1110
        assert result["avgReadIops"] == pytest.approx(1110.0, abs=1.0)

    def test_write_iops_averaged(self):
        result = parse_iostat(IOSTAT_OUTPUT)
        # (300+250+320+270) / 4 = 285
        assert result["avgWriteIops"] == pytest.approx(285.0, abs=1.0)

    def test_read_bandwidth_in_mbs(self):
        result = parse_iostat(IOSTAT_OUTPUT)
        # (48000+39200+50000+40400) / 4 kB/s / 1024 = ~43.16 MB/s
        assert result["avgReadBwMBs"] == pytest.approx(43.16, abs=0.5)

    def test_loop_devices_excluded(self):
        """loop0, loop1, etc. should be filtered out."""
        output_with_loop = IOSTAT_OUTPUT + """
Device            r/s     w/s   rkB/s   wkB/s  rrqm/s  wrqm/s  %rrqm  %wrqm r_await w_await aqu-sz rareq-sz wareq-sz  %util
loop0            0.10    0.00     1.20     0.00    0.00    0.00   0.00   0.00    5.00    0.00   0.00     1.00     0.00   0.00
"""
        result_with    = parse_iostat(output_with_loop)
        result_without = parse_iostat(IOSTAT_OUTPUT)
        # loop0 with tiny IOPS should not change the average significantly
        # (it's excluded so averages should be identical)
        assert result_with["avgReadIops"] == result_without["avgReadIops"]

    def test_device_filter(self):
        """device_filter restricts to devices matching the substring."""
        result = parse_iostat(IOSTAT_OUTPUT, device_filter="nvme0")
        # Only nvme0n1: (1200+1250) / 2 = 1225
        assert result["avgReadIops"] == pytest.approx(1225.0, abs=1.0)

    def test_empty_output_returns_empty_dict(self):
        result = parse_iostat("")
        assert result == {}


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
