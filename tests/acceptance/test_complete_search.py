from pathlib import Path

import pytest

from job_hunt.config import ConfigError, load_config


def test_unknown_configured_adapter_is_rejected_safely(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "resume_path: resume.txt\ncompanies_path: companies.yaml\noutput_dir: runs\n"
        "as_of: 2026-09-25\ntimezone: UTC\ncollectors: [unknown]\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="not supported yet"):
        load_config(config)
