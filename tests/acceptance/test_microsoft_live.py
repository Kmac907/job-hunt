"""Bounded Microsoft portal probe; unsupported is a valid recorded outcome."""

from __future__ import annotations

import json
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest

from job_hunt.collectors.microsoft import (
    MICROSOFT_BLOCKER,
    MICROSOFT_CURRENT_SITE,
    MICROSOFT_ENTRYPOINT,
)

pytestmark = pytest.mark.live
OUTCOME = Path(__file__).parents[1] / "fixtures" / "collectors" / "microsoft" / "live_outcome.json"
DEADLINE_SECONDS = 15


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "live: bounded live provider probe")


def test_microsoft_live_probe_persists_current_outcome() -> None:
    started = time.monotonic()
    outcome = {
        "portal": "microsoft",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "entrypoint": MICROSOFT_ENTRYPOINT,
        "current_site": MICROSOFT_CURRENT_SITE,
    }
    try:
        request = Request(MICROSOFT_ENTRYPOINT, headers={"User-Agent": "job-hunt/1.0"})
        probe_result: list[tuple[str, object]] = []

        def probe() -> None:
            try:
                with urlopen(request, timeout=DEADLINE_SECONDS) as response:  # noqa: S310
                    probe_result.append(("response", (response.status, response.geturl())))
            except (HTTPError, URLError, TimeoutError, socket.timeout, OSError) as exc:
                probe_result.append(("blocked", f"official probe blocked: {type(exc).__name__}: {exc}"))

        worker = threading.Thread(target=probe, daemon=True)
        worker.start()
        worker.join(max(0, started + DEADLINE_SECONDS - time.monotonic()))
        if worker.is_alive():
            outcome.update(status="unsupported", blocker="official probe exceeded its hard deadline")
        elif probe_result[0][0] == "response":
            status, final_url = probe_result[0][1]  # type: ignore[misc]
            outcome.update(status="unsupported", http_status=status, final_url=final_url, blocker=MICROSOFT_BLOCKER)
        else:
            outcome.update(status="unsupported", blocker=probe_result[0][1])
    finally:
        outcome["elapsed_seconds"] = round(time.monotonic() - started, 3)
        OUTCOME.write_text(json.dumps(outcome, indent=2) + "\n", encoding="utf-8")

    assert time.monotonic() - started <= DEADLINE_SECONDS + 1
    assert OUTCOME.is_file()
    saved = json.loads(OUTCOME.read_text(encoding="utf-8"))
    assert saved["status"] in {"success", "unsupported"}
    if saved["status"] == "unsupported":
        assert saved.get("blocker")
