import subprocess
from pathlib import Path

from job_hunt.cli import DISCLOSURE, main
from job_hunt.codex_adapter import CodexPreflight, codex_preflight


def _config(tmp_path: Path) -> Path:
    (tmp_path / "resume.pdf").write_bytes(b"resume")
    (tmp_path / "companies.yaml").write_text("companies: [{name: Acme}]\n", encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(
        "resume_path: resume.pdf\ncompanies_path: companies.yaml\noutput_dir: runs\n"
        "as_of: 2026-09-21\ntimezone: UTC\nruntime: {model: gpt-requested}\n",
        encoding="utf-8",
    )
    return config


def test_codex_preflight_checks_version_and_login_without_inference() -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        stdout = "codex-cli 1.2.3\n" if command[-1] == "--version" else "Logged in\n"
        return subprocess.CompletedProcess(command, 0, stdout, "")

    result = codex_preflight(runner=run)
    assert result == CodexPreflight(version="codex-cli 1.2.3")
    assert [Path(command[0]).stem.casefold() for command in calls] == ["codex", "codex"]
    assert [command[1:] for command in calls] == [["--version"], ["login", "status"]]
    assert all("exec" not in command for command in calls)


def test_doctor_discloses_external_processing_and_reports_codex(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr("job_hunt.cli.codex_preflight", lambda **_: CodexPreflight("codex-cli 1.2.3"))
    assert main(["doctor", "--config", str(_config(tmp_path))]) == 0
    output = capsys.readouterr()
    assert output.out.splitlines()[0] == DISCLOSURE
    assert "doctor: OK" in output.out and "codex-cli 1.2.3" in output.out
