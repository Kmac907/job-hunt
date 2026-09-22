"""Atomic, local-only persistence for run artifacts."""

from __future__ import annotations

import errno
import json
import os
import socket
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .models import RunManifest


class StorageError(RuntimeError):
    """A storage failure safe to show to an operator."""


class RunLockError(StorageError):
    """Raised when another process owns the workspace writer lock."""


class Storage:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise StorageError(f"storage root is not a directory: {self.root}")

    def path(self, relative: str | Path) -> Path:
        """Resolve a path while refusing traversal outside the storage root."""
        relative = Path(relative)
        candidate = relative.resolve() if relative.is_absolute() else (self.root / relative).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise StorageError(f"path escapes storage root: {relative}")
        return candidate

    def write_record(
        self, record_type: str, record_id: str, version: str | int, record: BaseModel | dict[str, Any]
    ) -> Path:
        path = self.path(Path("records") / _component(record_type) / _component(record_id) / f"{_component(version)}.json")
        _atomic_json(path, record, replace=False)
        return path

    def read_record(self, record_type: str, record_id: str, version: str | int) -> dict[str, Any]:
        path = self.path(Path("records") / _component(record_type) / _component(record_id) / f"{_component(version)}.json")
        return self.read_json(path)

    def write_snapshot(self, name: str | Path, content: str | bytes) -> Path:
        snapshots = self.path("snapshots")
        path = self.path(Path("snapshots") / name)
        if path != snapshots and snapshots not in path.parents:
            raise StorageError(f"snapshot path escapes snapshots directory: {name}")
        _atomic_bytes(path, content.encode("utf-8") if isinstance(content, str) else content, replace=False)
        return path

    def read_bytes(self, relative: str | Path) -> bytes:
        return self.path(relative).read_bytes()

    def read_json(self, relative: str | Path) -> dict[str, Any]:
        path = self.path(relative)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise StorageError(f"cannot read JSON file {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise StorageError(f"JSON file must contain an object: {path}")
        return value

    def write_json(
        self,
        relative: str | Path,
        value: BaseModel | dict[str, Any],
        *,
        replace: bool = True,
    ) -> Path:
        """Commit one JSON object without exposing a partially written destination."""
        path = self.path(relative)
        _atomic_json(path, value, replace=replace)
        return path

    def write_bytes(
        self, relative: str | Path, content: str | bytes, *, replace: bool = True
    ) -> Path:
        path = self.path(relative)
        _atomic_bytes(
            path,
            content.encode("utf-8") if isinstance(content, str) else content,
            replace=replace,
        )
        return path

    def read_cache(self, artifact: str, key: str) -> dict[str, Any] | None:
        path = self.path(Path("cache") / _component(artifact) / f"{_component(key)}.json")
        return None if not path.is_file() else self.read_json(path)

    def write_cache(self, artifact: str, key: str, value: dict[str, Any]) -> Path:
        path = self.path(Path("cache") / _component(artifact) / f"{_component(key)}.json")
        _atomic_json(path, value, replace=True)
        return path

    def save_manifest(self, manifest: RunManifest) -> Path:
        path = self.path(Path("runs") / _component(manifest.run_id) / "manifest.json")
        _atomic_json(path, manifest, replace=True)
        return path

    def load_manifest(self, run_id: str) -> RunManifest:
        return RunManifest.model_validate(self.read_json(Path("runs") / _component(run_id) / "manifest.json"))

    def rebuild_index(self) -> dict[str, Any]:
        records: list[dict[str, Any]] = []
        records_root = self.path("records")
        if records_root.exists():
            for path in sorted(records_root.glob("*/*/*.json")):
                relative = path.relative_to(records_root)
                data = self.read_json(path)
                records.append(
                    {
                        "record_type": relative.parts[0],
                        "record_id": relative.parts[1],
                        "version": path.stem,
                        "schema_version": data.get("schema_version"),
                        "path": path.relative_to(self.root).as_posix(),
                    }
                )
        index = {"records": records}
        _atomic_json(self.path("index.json"), index, replace=True)
        return index

    def run_lock(self) -> "RunLock":
        return RunLock(self.path(".run.lock"))


class RunLock(AbstractContextManager["RunLock"]):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.token = uuid.uuid4().hex
        self._held = False

    def acquire(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        metadata = {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "token": self.token,
        }
        payload = json.dumps(metadata).encode("utf-8")
        with _lock_guard(self.path):
            while True:
                try:
                    created = _atomic_create(self.path, payload)
                except OSError as exc:
                    raise RunLockError(f"cannot create workspace lock {self.path}: {exc}") from exc
                if not created:
                    if self._remove_stale():
                        continue
                    raise RunLockError(f"another run holds the workspace lock: {self.path}") from None
                self._held = True
                return self

    def _remove_stale(self) -> bool:
        try:
            metadata = json.loads(self.path.read_text(encoding="utf-8"))
            pid = metadata["pid"]
            hostname = metadata["hostname"]
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
            return False
        if hostname != socket.gethostname() or not isinstance(pid, int) or _pid_exists(pid):
            return False
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        return True

    def release(self) -> None:
        if not self._held:
            return
        try:
            with _lock_guard(self.path):
                metadata = json.loads(self.path.read_text(encoding="utf-8"))
                if metadata.get("token") == self.token:
                    self.path.unlink(missing_ok=True)
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
        self._held = False

    def __enter__(self) -> "RunLock":
        return self.acquire()

    def __exit__(self, *args: object) -> None:
        self.release()


def _component(value: str | int) -> str:
    value = str(value)
    if not value or value in {".", ".."} or Path(value).name != value or "/" in value or "\\" in value:
        raise StorageError(f"invalid storage path component: {value!r}")
    return value


@contextmanager
def _lock_guard(path: Path) -> Iterator[None]:
    """Serialize changes to a lock path; the OS releases this guard after crashes."""
    with path.with_name(f"{path.name}.guard").open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _json_bytes(value: BaseModel | dict[str, Any]) -> bytes:
    data = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _atomic_json(path: Path, value: BaseModel | dict[str, Any], *, replace: bool) -> None:
    _atomic_bytes(path, _json_bytes(value), replace=replace)


def _atomic_bytes(path: Path, content: bytes, *, replace: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if replace:
            temporary = _temporary_file(path, content)
            try:
                _replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        elif not _atomic_create(path, content):
            raise StorageError(f"immutable artifact already exists: {path}")
    except OSError as exc:
        raise StorageError(f"cannot write {path}: {exc}") from exc


def _atomic_create(path: Path, content: bytes) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_file(path, content)
    try:
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        return True
    finally:
        temporary.unlink(missing_ok=True)


def _temporary_file(path: Path, content: bytes) -> Path:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        return temporary
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def _replace(source: Path, destination: Path) -> None:
    """Tolerate short-lived Windows file locks without making writes unbounded."""
    for attempt in range(3):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == 2:
                raise
            time.sleep(0.01 * (attempt + 1))


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return ctypes.windll.kernel32.GetLastError() == 5
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno != errno.ESRCH
    return True
