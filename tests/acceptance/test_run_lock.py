import json
import socket
from pathlib import Path

import pytest

from job_hunt.storage import RunLockError, Storage


def test_run_lock_rejects_a_second_writer_and_releases(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    with storage.run_lock():
        with pytest.raises(RunLockError, match="another run"):
            storage.run_lock().acquire()
    with storage.run_lock():
        assert storage.path(".run.lock").exists()


def test_run_lock_recovers_only_a_demonstrably_stale_lock(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    lock_path = storage.path(".run.lock")
    lock_path.write_text(json.dumps({"pid": 0, "hostname": socket.gethostname()}), encoding="utf-8")
    with storage.run_lock():
        assert json.loads(lock_path.read_text(encoding="utf-8"))["pid"] > 0

    lock_path.write_text("not enough evidence", encoding="utf-8")
    with pytest.raises(RunLockError):
        storage.run_lock().acquire()
