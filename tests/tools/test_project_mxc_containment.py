"""Model-selected projects must not mint a new sandbox authority root."""

import json
from unittest.mock import Mock

import pytest

from hermes_constants import get_hermes_home
from hermes_cli import projects_db
from tools import project_tools
from tools.registry import registry


@pytest.mark.parametrize("action", ["create", "switch"])
@pytest.mark.parametrize("policy", ["backend: mxc", "backend: mxc\n  mxc_network: true", "backend: ["])
def test_project_mutation_refused_before_database_or_workspace_change(tmp_path, monkeypatch, action, policy):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    with projects_db.connect_closing() as conn:
        current = projects_db.create_project(conn, name="Current", primary_path=str(workspace))
        target = projects_db.create_project(conn, name="Target", primary_path=str(outside))
        projects_db.set_active(conn, current)
    callback = Mock()
    monkeypatch.setattr(project_tools, "_workspace_callback", callback)
    (get_hermes_home() / "config.yaml").write_text(f"terminal:\n  {policy}\n", encoding="utf-8")

    # Invoke the registered handler directly: the shared dispatch guard is not the
    # only entry point to these helpers.
    result = json.loads(registry.get_entry("desktop_project").handler(
        {"action": action, "name": "New" if action == "create" else target, "path": str(outside / "new")},
        task_id="containment-project"))

    assert result.get("success") is False, result
    assert result.get("error"), result
    callback.assert_not_called()
    with projects_db.connect_closing() as conn:
        assert projects_db.get_active_id(conn) == current
        assert {p.id for p in projects_db.list_projects(conn)} == {current, target}
    listed = json.loads(project_tools.project_list())
    assert listed["active_id"] == current
    assert {p["id"] for p in listed["projects"]} == {current, target}


def test_non_mxc_projects_keep_creation_switch_and_listing(tmp_path, monkeypatch):
    (get_hermes_home() / "config.yaml").write_text("terminal:\n  backend: local\n", encoding="utf-8")
    workspace = tmp_path / "project"
    workspace.mkdir()
    callback = Mock()
    monkeypatch.setattr(project_tools, "_workspace_callback", callback)
    created = json.loads(project_tools.project_create("Fixture", str(workspace), task_id="fixture"))
    assert created["success"] is True
    switched = json.loads(project_tools.project_switch(created["id"], task_id="fixture"))
    assert switched["success"] is True
    assert callback.call_count == 2
    assert callback.call_args.args == ("fixture", str(workspace), "Fixture")
    assert json.loads(project_tools.project_list())["active_id"] == created["id"]
