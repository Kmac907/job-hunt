import json
import socket
import threading
from pathlib import Path

import pytest

from job_hunt.storage import RunLock, RunLockError, Storage


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


def test_concurrent_stale_lock_recovery_keeps_one_writer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = Storage(tmp_path)
    lock_path = storage.path(".run.lock")
    lock_path.write_text(json.dumps({"pid": 0, "hostname": socket.gethostname()}), encoding="utf-8")
    first = storage.run_lock()
    second = storage.run_lock()
    removing_stale = threading.Event()
    second_recovering = threading.Event()
    resume = threading.Event()
    original_unlink = Path.unlink
    original_remove_stale = RunLock._remove_stale

    def pause_first_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if path == lock_path and threading.current_thread().name == "first":
            removing_stale.set()
            assert resume.wait(timeout=5)
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", pause_first_unlink)

    def note_second_recovery(lock: RunLock) -> bool:
        if lock is second:
            second_recovering.set()
        return original_remove_stale(lock)

    monkeypatch.setattr(RunLock, "_remove_stale", note_second_recovery)
    results: list[object] = []

    def acquire(lock: RunLock) -> None:
        try:
            results.append(lock.acquire())
        except RunLockError as exc:
            results.append(exc)

    first_thread = threading.Thread(target=acquire, args=(first,), name="first")
    second_thread = threading.Thread(target=acquire, args=(second,), name="second")
    first_thread.start()
    assert removing_stale.wait(timeout=5)
    second_thread.start()
    raced_stale_check = second_recovering.wait(timeout=1)
    resume.set()
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)

    try:
        assert not raced_stale_check
        assert results.count(first) + results.count(second) == 1
        assert sum(isinstance(result, RunLockError) for result in results) == 1
    finally:
        first.release()
        second.release()
