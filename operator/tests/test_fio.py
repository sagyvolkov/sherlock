"""
tests/test_fio.py — fio command construction and volume wiring.

Run with: python -m pytest operator/tests/test_fio.py -v
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.handlers import fio

PARAMS = {"blockSize": "32k", "rwMixWrite": 30, "ioPattern": "randrw", "runtime": 120,
          "jobs": 4, "ioDepth": 16, "workSize": "10g", "directIO": 1}


def test_block_mode_runs_run_fio_on_the_raw_device():
    cmd = fio._build_fio_command(PARAMS, "Block")
    assert cmd == ["/scripts/run_fio", "30", "32k", fio.FIO_DEVICE, "120", "4", "16",
                   "10g", "RAW", "1", "randrw", "json"]


def test_filesystem_mode_uses_mounted_directory():
    cmd = fio._build_fio_command(PARAMS, "Filesystem")
    assert cmd[3] == fio.FIO_DIRECTORY and cmd[8] == "FS"


def test_volume_args():
    assert fio._volume_args("p", "Block") == {
        "block_devices": [{"pvc_name": "p", "device_path": fio.FIO_DEVICE}]}
    assert fio._volume_args("p", "Filesystem") == {
        "pvc_mounts": [{"pvc_name": "p", "mount_path": fio.FIO_DIRECTORY}]}
