from pathlib import Path

from job_hunt.cli import DISCLOSURE, main


def write_config(tmp_path: Path, *, model: str = "gpt-5", collectors: str = "[]") -> Path:
    (tmp_path / "resume.pdf").write_bytes(b"resume")
    (tmp_path / "companies.yaml").write_text("companies: [{name: Acme}]\n", encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(
        "resume_path: resume.pdf\ncompanies_path: companies.yaml\noutput_dir: runs\n"
        f"as_of: 2026-09-21\ntimezone: UTC\nruntime: {{model: {model}}}\ncollectors: {collectors}\n",
        encoding="utf-8",
    )
    return config


def test_doctor_discloses_processing_and_passes(tmp_path: Path, capsys) -> None:
    config = write_config(tmp_path)
    assert main(["doctor", "--config", str(config)]) == 0
    output = capsys.readouterr()
    assert output.out.splitlines()[0] == DISCLOSURE
    assert "doctor: OK" in output.out


def test_doctor_rejects_unsupported_collectors(tmp_path: Path, capsys) -> None:
    config = write_config(tmp_path, collectors="[linkedin]")
    assert main(["doctor", "--config", str(config)]) == 1
    output = capsys.readouterr()
    assert "not supported yet: linkedin" in output.err


def test_doctor_rejects_unset_model(tmp_path: Path, monkeypatch, capsys) -> None:
    config = write_config(tmp_path, model="null")
    monkeypatch.delenv("CODEX_MODEL", raising=False)
    assert main(["doctor", "--config", str(config)]) == 1
    assert "set runtime.model or CODEX_MODEL" in capsys.readouterr().err


def test_doctor_rejects_missing_inputs_and_blocked_output(tmp_path: Path, capsys) -> None:
    config = write_config(tmp_path)
    (tmp_path / "resume.pdf").unlink()
    assert main(["doctor", "--config", str(config)]) == 1
    assert "resume file does not exist" in capsys.readouterr().err

    (tmp_path / "resume.pdf").write_bytes(b"resume")
    (tmp_path / "companies.yaml").unlink()
    assert main(["doctor", "--config", str(config)]) == 1
    assert "companies file does not exist" in capsys.readouterr().err

    (tmp_path / "companies.yaml").write_text("companies: [{name: Acme}]\n", encoding="utf-8")
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    config.write_text(config.read_text(encoding="utf-8").replace("output_dir: runs", "output_dir: blocked/runs"), encoding="utf-8")
    assert main(["doctor", "--config", str(config)]) == 1
    assert "output location is not writable" in capsys.readouterr().err
