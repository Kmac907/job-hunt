import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from job_hunt.collectors.amazon import probe_official_interface

pytestmark = pytest.mark.live
OUTCOME = Path(__file__).parents[1] / "fixtures" / "collectors" / "amazon" / "outcome.json"


def test_amazon_live_probe_persists_current_outcome() -> None:
    outcome = json.loads(OUTCOME.read_text(encoding="utf-8"))
    probe_error = None
    try:
        outcome["live_probe"] = probe_official_interface(deadline_seconds=10)
    except Exception as exc:  # noqa: BLE001
        probe_error = exc
        outcome["live_probe"] = {
            "status": "unsupported",
            "blocker": f"official search probe raised {type(exc).__name__}: {exc}",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "deadline_seconds": 10,
        }
    outcome["status"] = "unsupported"
    outcome["blocker"] = outcome["live_probe"]["blocker"]
    OUTCOME.write_text(json.dumps(outcome, indent=2) + "\n", encoding="utf-8")

    if probe_error is not None:
        raise probe_error
    assert OUTCOME.is_file()
    assert outcome["status"] == "unsupported"
    assert outcome["live_probe"]["finished_at"]
    assert outcome["blocker"]
