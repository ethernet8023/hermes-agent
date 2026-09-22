"""Persisted cron scripts must consult live policy at the host launch boundary."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cron import scheduler_script
from hermes_constants import get_hermes_home
from tools.terminal_scope import install_and_reset_profile_terminal_scope


@pytest.mark.parametrize("policy", ["backend: mxc", "backend: mxc\n  mxc_network: true", "backend: ["])
def test_existing_script_cannot_launch_after_live_policy_change(tmp_path, monkeypatch, policy):
    home = get_hermes_home()
    scripts = home / "scripts"
    scripts.mkdir()
    (scripts / "fixture.py").write_text("print('fixture')\n", encoding="utf-8")
    config = home / "config.yaml"
    config.write_text("terminal:\n  backend: local\n", encoding="utf-8")
    launch = Mock(return_value=SimpleNamespace(
        returncode=0, communicate=lambda **kw: ("fixture", "")))
    monkeypatch.setattr(scheduler_script.subprocess, "Popen", launch)

    with install_and_reset_profile_terminal_scope(home):
        assert scheduler_script._run_job_script("fixture.py") == (True, "fixture")
        launch.assert_called_once()
        launch.reset_mock()
        config.write_text(f"terminal:\n  {policy}\n", encoding="utf-8")
        success, reason = scheduler_script._run_job_script("fixture.py")

    assert launch.call_count == 0
    assert success is False
    assert reason
