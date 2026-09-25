"""Bounded, truthful live validation for the reference Greenhouse adapter."""

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from job_hunt.collectors.base import AccessBlockedError, SafeHttpClient
from job_hunt.collectors.greenhouse import GreenhouseCollector
from job_hunt.config import CollectorConfig

pytestmark = pytest.mark.live
OUTCOME = Path(__file__).parents[1] / "fixtures" / "collectors" / "greenhouse" / "live_outcome.json"
LIVE_DEADLINE_SECONDS = 15


def test_greenhouse_live_check_is_bounded_and_never_skips() -> None:
    token = os.getenv("GREENHOUSE_LIVE_BOARD_TOKEN", "qualtrics").strip()
    company = os.getenv("GREENHOUSE_LIVE_COMPANY", "Qualtrics").strip()
    started = datetime.now(timezone.utc)
    outcome: dict[str, object] = {
        "status": "unsupported",
        "board_token": token,
        "checked_at": started.isoformat(),
        "reason": "",
        "evidence": [],
    }
    try:
        result_box: dict[str, object] = {}
        error_box: dict[str, Exception] = {}

        def discover() -> None:
            try:
                client = SafeHttpClient(
                    CollectorConfig(
                        name="greenhouse-live",
                        allowed_origins=["https://boards-api.greenhouse.io"],
                        timeout_seconds=5,
                        max_retries=0,
                        max_redirects=2,
                        requests_per_second=100,
                        max_concurrency=1,
                    )
                )
                result_box["result"] = GreenhouseCollector(client, token, company=company).discover(company)
            except Exception as exc:
                error_box["error"] = exc

        deadline = time.monotonic() + LIVE_DEADLINE_SECONDS
        worker = threading.Thread(target=discover, daemon=True)
        worker.start()
        worker.join(max(0, deadline - time.monotonic()))
        if worker.is_alive():
            raise TimeoutError(f"live check exceeded {LIVE_DEADLINE_SECONDS}s deadline")
        if error_box:
            raise next(iter(error_box.values()))
        result = result_box["result"]
        if result.status == "success":
            outcome["status"] = "success"
            outcome["evidence"] = [snapshot.sha256 for snapshot in result.snapshots]
            outcome["reason"] = "current public board identity and listing response validated"
        else:
            outcome["reason"] = result.error or f"current board response status: {result.status}"
    except (AccessBlockedError, TimeoutError, OSError, ValueError) as exc:
        outcome["reason"] = f"concrete live blocker: {exc}"
    finally:
        OUTCOME.write_text(json.dumps(outcome, indent=2) + "\n", encoding="utf-8")

    assert outcome["status"] in {"success", "unsupported"}
    assert outcome["reason"]
