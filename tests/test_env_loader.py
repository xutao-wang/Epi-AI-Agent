from __future__ import annotations

from utils.env_loader import remove_local_env_values


def test_remove_local_env_values_preserves_unrelated_lines_and_mode(
    tmp_path,
) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "# local\n"
        "OPENAI_API_KEY=secret\n"
        "REPORT_AGENT_MODEL=gpt-5.4\n"
        "REPORT_AGENT_ALLOWED_MODELS=gpt-5.4\n"
        "REPORT_AGENT_RUNTIME_ROOT=/data\n",
        encoding="utf-8",
    )

    remove_local_env_values(
        tmp_path,
        {"REPORT_AGENT_MODEL", "REPORT_AGENT_ALLOWED_MODELS"},
    )

    assert path.read_text(encoding="utf-8") == (
        "# local\nOPENAI_API_KEY=secret\nREPORT_AGENT_RUNTIME_ROOT=/data\n"
    )
    assert path.stat().st_mode & 0o777 == 0o600


def test_remove_local_env_values_is_safe_when_file_is_absent(tmp_path) -> None:
    remove_local_env_values(tmp_path, {"REPORT_AGENT_MODEL"})

    assert not (tmp_path / ".env").exists()
