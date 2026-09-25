import json
from pathlib import Path

import pytest

from job_hunt.collectors.amazon import probe_official_interface

pytestmark = pytest.mark.live
OUTCOME = Path(__file__).parents[1] / "fixtures" / "collectors" / "amazon" / "outcome.json"


def test_amazon_live_probe_persists_current_unsupported_outcome() -> None:
    outcome = json.loads(OUTCOME.read_text(encoding="utf-8"))
    outcome["live_probe"] = probe_official_interface(deadline_seconds=10)
    outcome["status"] = "unsupported"
    outcome["blocker"] = outcome["live_probe"]["blocker"]
    OUTCOME.write_text(json.dumps(outcome, indent=2) + "\n", encoding="utf-8")

    assert OUTCOME.is_file()
    assert outcome["status"] == "unsupported"
    assert outcome["live_probe"]["finished_at"]
    assert outcome["blocker"]
