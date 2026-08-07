from __future__ import annotations

from pathlib import Path


def test_python_worker_launcher_enforces_the_fixed_privilege_boundary() -> None:
    launcher = (
        Path(__file__).parents[1] / "deploy" / "aws" / "bin" / "epi-agent-python-worker"
    )
    source = launcher.read_text(encoding="utf-8")

    assert "realpath -e" in source
    assert "/srv/epi-agent/runtime/users" in source
    assert "/threads/" in source
    assert "/execution/" in source
    assert "find -P" in source
    assert "stat -c %u" in source
    assert "env -i" in source
    assert "runuser --user epi-agent-exec" in source
    assert "/opt/epi-agent/current/epi_agent/runtimes/python/worker.py" in source
    web_acl_command = "runuser --user epi-agent-web -- /usr/bin/setfacl"
    assert source.count(web_acl_command) == 2
    assert "\n/usr/bin/setfacl" not in source
    assert source.index(web_acl_command) < source.index(
        "runuser --user epi-agent-exec"
    )
    assert 'eval ' not in source
    assert 'bash -c' not in source
