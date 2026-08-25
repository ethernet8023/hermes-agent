"""ensure_dependency routes through pm: availability checks stay local,
installs go to pm.ensure, and pm's lazy-install policy owns refusal."""

from unittest.mock import patch

import pytest


def test_unknown_dep_refused():
    from hermes_cli.dep_ensure import ensure_dependency

    assert ensure_dependency("not-a-dep") is False


def test_available_dep_short_circuits(monkeypatch):
    from hermes_cli import dep_ensure

    monkeypatch.setitem(
        dep_ensure._DEPS, "node", (lambda: True, ("node",))
    )
    called = []
    with patch("pm.ensure", side_effect=lambda *a, **k: called.append(a)):
        assert dep_ensure.ensure_dependency("node") is True
    assert called == []


def test_missing_dep_installs_through_pm(monkeypatch):
    from hermes_cli import dep_ensure

    state = {"installed": False}
    monkeypatch.setitem(
        dep_ensure._DEPS, "node", (lambda: state["installed"], ("node",))
    )

    def fake_ensure(name, **kw):
        assert name == "node"
        state["installed"] = True

    with patch("pm.ensure", side_effect=fake_ensure):
        assert dep_ensure.ensure_dependency("node") is True


def test_pm_refusal_reports_and_returns_false(monkeypatch, capsys):
    from hermes_cli import dep_ensure

    monkeypatch.setitem(
        dep_ensure._DEPS, "node", (lambda: False, ("node",))
    )
    import pm as pm_mod

    def refuse(name, **kw):
        raise pm_mod.InstallError(name, "lazy installs are disabled", "run `hermes pm install`")

    with patch("pm.ensure", side_effect=refuse):
        assert dep_ensure.ensure_dependency("node", interactive=True) is False
    out = capsys.readouterr().out
    assert "hermes pm install" in out


def test_browser_check_consults_pm(monkeypatch):
    from hermes_cli import dep_ensure

    monkeypatch.setattr("shutil.which", lambda *a, **k: None)
    with patch("pm.is_installed", return_value=True):
        assert dep_ensure._browser_available() is True
    with patch("pm.is_installed", return_value=False):
        assert dep_ensure._browser_available() is False
