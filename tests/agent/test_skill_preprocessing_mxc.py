"""Skill text remains readable; inline shell does not escape terminal policy."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent import skill_preprocessing
from hermes_constants import get_hermes_home


@pytest.mark.parametrize("policy", ["backend: mxc", "backend: mxc\n  mxc_network: true", "backend: ["])
def test_inline_shell_refuses_host_launch_without_hiding_skill(tmp_path, monkeypatch, policy):
    (get_hermes_home() / "config.yaml").write_text(f"terminal:\n  {policy}\n", encoding="utf-8")
    launch = Mock(return_value=SimpleNamespace(stdout="HOST_EXECUTED", stderr="", returncode=0))
    monkeypatch.setattr(skill_preprocessing.subprocess, "run", launch)
    content = "# Fixture skill\nFolder: ${HERMES_SKILL_DIR}\nDynamic: !`printf HOST_EXECUTED`\nKeep these instructions."

    rendered = skill_preprocessing.preprocess_skill_content(
        content, tmp_path, skills_cfg={"inline_shell": True})

    launch.assert_not_called()
    assert rendered.startswith(f"# Fixture skill\nFolder: {tmp_path}\n")
    assert "Keep these instructions." in rendered
    assert "inline-shell refused" in rendered
    assert "HOST_EXECUTED" not in rendered


def test_non_mxc_inline_shell_still_expands(tmp_path):
    (get_hermes_home() / "config.yaml").write_text("terminal:\n  backend: local\n", encoding="utf-8")
    rendered = skill_preprocessing.preprocess_skill_content(
        "# Fixture\n!`printf inline-local-sentinel`", tmp_path, skills_cfg={"inline_shell": True})
    assert rendered == "# Fixture\ninline-local-sentinel"


def test_disabled_inline_shell_keeps_harmless_skill_text(tmp_path):
    (get_hermes_home() / "config.yaml").write_text("terminal:\n  backend: mxc\n", encoding="utf-8")
    content = "# Fixture\n!`printf example`\n${HERMES_SESSION_ID}"
    assert skill_preprocessing.preprocess_skill_content(
        content, tmp_path, session_id="fixture", skills_cfg={"inline_shell": False}
    ) == "# Fixture\n!`printf example`\nfixture"
