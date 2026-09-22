"""Session workspaces under the Windows sandbox backend.

A sandboxed session cannot work in a folder the policy refuses (home, drive root, a parent of
HERMES_HOME). The gateway resolves such a session to the sandbox's default workspace at creation, while
an explicit folder pick of a refused folder is reported as an error rather than silently redirected.
Other backends are untouched.

``session_workdir`` bodies are rebound onto ``tui_gateway.server``'s globals at install time, so the
tests call and patch through ``server`` like the rest of the suite.
"""

from __future__ import annotations

import pytest

from tools.environments import mxc_host
from tui_gateway import server


@pytest.fixture
def sandboxed(monkeypatch, tmp_path):
    default = tmp_path / "Hermes"
    default.mkdir()
    refused = tmp_path / "home"
    refused.mkdir()
    monkeypatch.setattr(server, "_effective_terminal_backend", lambda: "mxc")
    monkeypatch.setattr(mxc_host, "unsafe_workspace_reason",
                        lambda path: "would be your home folder" if path == str(refused) else None)
    monkeypatch.setattr(mxc_host, "default_workspace", lambda: str(default))
    return refused, default


def test_completion_cwd_replaces_a_refused_folder_with_the_default_workspace(sandboxed):
    refused, default = sandboxed
    assert server._completion_cwd({"cwd": str(refused)}) == str(default)


def test_completion_cwd_keeps_an_acceptable_folder(sandboxed, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    assert server._completion_cwd({"cwd": str(project)}) == str(project)


def test_other_backends_are_untouched(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(server, "_effective_terminal_backend", lambda: "local")
    monkeypatch.setattr(mxc_host, "unsafe_workspace_reason", lambda path: "refused")
    assert server._completion_cwd({"cwd": str(home)}) == str(home)


def test_explicit_pick_of_a_refused_folder_is_an_error_not_a_redirect(sandboxed):
    refused, _default = sandboxed
    with pytest.raises(ValueError, match="home folder"):
        server._set_session_cwd({"session_key": "k", "cwd": ""}, str(refused))


def test_resume_fallback_and_stored_rows_are_re_homed_too(sandboxed, monkeypatch):
    """Sessions saved before the sandbox was turned on carry a home cwd; resuming them must land in the
    default workspace like a fresh session does."""
    refused, default = sandboxed
    monkeypatch.setenv("TERMINAL_CWD", str(refused))
    monkeypatch.setattr(server, "_launch_configured_cwd", lambda: "")
    assert server._default_session_cwd() == str(default)

    class _Db:
        def get_session(self, key):
            return {"cwd": str(refused)}

    sid = "sid-rehome"
    with server._sessions_lock:
        server._sessions[sid] = {"cwd": str(refused)}
    try:
        server._hydrate_session_cwd(sid, "key", _Db(), None)
        assert server._sessions[sid]["cwd"] == str(default)
    finally:
        with server._sessions_lock:
            server._sessions.pop(sid, None)


def test_terminal_task_cwd_keeps_the_rehomed_session_folder_over_the_process_fallback(sandboxed, monkeypatch):
    """The sandbox runs on the host's filesystem, so the session's tracked folder (the one the policy
    re-homed it to) is what its commands must start in. Preferring the process-wide TERMINAL_CWD, as
    the remote backends do, would send every command back to the very folder the policy refused."""
    refused, default = sandboxed
    monkeypatch.setenv("TERMINAL_CWD", str(refused))
    monkeypatch.setattr(server, "_workdir_terminal_cfg", lambda key: "")
    session = {"cwd": str(default), "explicit_cwd": False, "source": "desktop"}
    assert server._terminal_task_cwd_with_source(session) == (str(default), "session")


def test_live_rehome_updates_display_and_durable_session(sandboxed, monkeypatch):
    refused, default = sandboxed
    saved = []
    monkeypatch.setattr(server, "_persist_session_cwd_and_schedule_git_meta", lambda session, cwd: saved.append(cwd))
    monkeypatch.setattr(server, "_register_session_cwd", lambda session: None)
    session = {"cwd": str(refused), "explicit_cwd": True, "session_key": "k"}
    assert server._display_session_cwd(session) == str(default)
    assert session["cwd"] == str(default)
    assert saved == [str(default)]


def test_sandbox_settle_cannot_promote_readonly_worktree(sandboxed, tmp_path, monkeypatch):
    from tools import terminal_tool
    project, readonly = tmp_path / "project", tmp_path / "readonly"
    project.mkdir()
    readonly.mkdir()
    monkeypatch.setattr(terminal_tool, "get_session_cwd", lambda key: str(readonly))
    monkeypatch.setattr(server.git_probe, "repo_root", lambda path: path)
    monkeypatch.setattr(server.git_probe, "common_repo_root", lambda path: str(project))
    monkeypatch.setattr(server, "_persist_session_cwd_and_schedule_git_meta", lambda *a: None)
    monkeypatch.setattr(server, "_register_session_cwd", lambda *a: None)
    session = {"cwd": str(project), "session_key": "k", "cwd_from_settle": True}
    assert server._reconcile_session_cwd_from_terminal(session) is False
    assert session["cwd"] == str(project)
