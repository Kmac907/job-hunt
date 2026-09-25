"""Bounded Ashby public-board probe; failures are persisted as unsupported."""

from __future__ import annotations

import json
import queue
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest

from job_hunt.collectors.ashby import ASHBY_API_ORIGIN, ASHBY_API_VERSION

pytestmark = pytest.mark.live
OUTCOME = Path(__file__).parents[1] / "fixtures" / "collectors" / "ashby" / "live_outcome.json"
BOARD_NAME = "Ashby"
ENDPOINT = f"{ASHBY_API_ORIGIN}/posting-api/job-board/{BOARD_NAME}"
DEADLINE_SECONDS = 15


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "live: bounded live provider probe")


def test_ashby_live_probe_persists_current_outcome() -> None:
    started = time.monotonic()
    outcome: dict[str, object] = {
        "portal": "ashby",
        "board_name": BOARD_NAME,
        "endpoint": ENDPOINT,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "status": "unsupported",
    }
    result: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)

    def probe() -> None:
        try:
            request = Request(
                ENDPOINT,
                headers={"Accept": "application/json", "User-Agent": "job-hunt-live-probe/1"},
            )
            with urlopen(request, timeout=DEADLINE_SECONDS) as response:  # noqa: S310
                payload = json.loads(response.read(50_000_000))
                result.put(("response", (response.status, response.geturl(), payload)))
        except Exception as exc:  # noqa: BLE001
            result.put(("error", exc))

    worker = threading.Thread(target=probe, daemon=True)
    worker.start()
    worker.join(DEADLINE_SECONDS)
    try:
        if worker.is_alive():
            outcome["blocker"] = f"official Ashby probe exceeded {DEADLINE_SECONDS}-second deadline"
        else:
            try:
                kind, value = result.get_nowait()
                if kind == "error":
                    raise value
                http_status, final_url, payload = value
                outcome.update(http_status=http_status, final_url=final_url)
                if (
                    final_url == ENDPOINT
                    and isinstance(payload, dict)
                    and payload.get("apiVersion") == ASHBY_API_VERSION
                    and isinstance(payload.get("jobs"), list)
                    and all(isinstance(job, dict) for job in payload["jobs"])
                ):
                    outcome.update(status="success", job_count=len(payload["jobs"]))
                else:
                    outcome["blocker"] = "public Ashby response did not match the documented board contract"
            except (HTTPError, URLError, TimeoutError, socket.timeout, OSError, ValueError) as exc:
                outcome["blocker"] = f"official Ashby probe blocked: {type(exc).__name__}: {exc}"
            except Exception as exc:  # noqa: BLE001
                outcome["blocker"] = f"official Ashby probe failed: {type(exc).__name__}: {exc}"
    finally:
        outcome["elapsed_seconds"] = round(time.monotonic() - started, 3)
        OUTCOME.write_text(json.dumps(outcome, indent=2) + "\n", encoding="utf-8")

    assert time.monotonic() - started <= DEADLINE_SECONDS + 1
    assert OUTCOME.is_file()
    saved = json.loads(OUTCOME.read_text(encoding="utf-8"))
    assert saved["status"] in {"success", "unsupported"}
    if saved["status"] == "unsupported":
        assert saved.get("blocker")
