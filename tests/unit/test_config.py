from datetime import date
from pathlib import Path

import pytest

from job_hunt.config import ConfigError, load_config, load_companies, resolve_safe


def test_defaults_and_company_overrides(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "resume_path: resume.pdf\ncompanies_path: companies.yaml\noutput_dir: runs\n"
        "as_of: 2026-09-21\ntimezone: UTC\n",
        encoding="utf-8",
    )
    companies_file = tmp_path / "companies.yaml"
    companies_file.write_text(
        "companies:\n  - name: Acme\n    since: 2026-09-01\n"
        "    scope:\n      titles: [engineer]\n      remote: true\n",
        encoding="utf-8",
    )

    config = load_config(config_file)
    companies = load_companies(companies_file)
    assert config.match_threshold == 0.85
    assert config.scoring_weights.model_dump() == {
        "requirements": 0.60,
        "preferences": 0.25,
        "company": 0.15,
    }
    assert config.preferences is None
    assert companies.companies[0].since == date(2026, 9, 1)
    assert config.snapshot(companies)["companies"][0]["scope"]["remote"] is True


def test_bad_yaml_weights_and_unsafe_paths_are_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("[unterminated", encoding="utf-8")
    with pytest.raises(ConfigError, match="malformed YAML"):
        load_config(bad)

    bad.write_text(
        "resume_path: r\ncompanies_path: c\noutput_dir: o\nas_of: 2026-09-21\n"
        "timezone: UTC\nscoring_weights: {requirements: .7, preferences: .2, company: .2}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="must total 1.0"):
        load_config(bad)
    with pytest.raises(ConfigError, match="unsafe"):
        resolve_safe(tmp_path, Path("../secret"), "resume")

