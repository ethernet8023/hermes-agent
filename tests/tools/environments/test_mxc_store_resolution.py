"""The MXC kit and shell resolve from the pm store and nowhere else.

A hand copy on PATH or under C:\\mxc-kit\\bin does not count, and a missing
entry is reported as missing rather than downloaded. Provisioning is the
toggle's job, and it lands in the writable store so a sealed payload is never
the write target.
"""

from pathlib import Path

import pytest

from tools.environments import mxc_host


def _stage(monkeypatch, tmp_path, *, kit=True, shell=True):
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setattr(mxc_host, "store_binary",
                        lambda name: (store / f"{name}.exe") if (store / f"{name}.exe").is_file() else None)
    if kit:
        (store / "mxc-kit.exe").write_bytes(b"kit")
    if shell:
        (store / "busybox.exe").write_bytes(b"shell")
    return store


def test_find_wxc_exec_returns_only_the_store_entry(monkeypatch, tmp_path):
    store = _stage(monkeypatch, tmp_path)
    hand_copy = tmp_path / "mxc-kit" / "bin"
    hand_copy.mkdir(parents=True)
    (hand_copy / "wxc-exec.exe").write_bytes(b"hand")
    monkeypatch.setenv("PATH", str(hand_copy))

    assert mxc_host.find_wxc_exec() == str(store / "mxc-kit.exe")
    # A configured path used to win. It is no longer consulted.
    assert mxc_host.find_wxc_exec("C:/somewhere/wxc-exec.exe") == str(store / "mxc-kit.exe")


def test_find_wxc_exec_is_none_when_the_store_has_no_kit(monkeypatch, tmp_path):
    _stage(monkeypatch, tmp_path, kit=False)
    assert mxc_host.find_wxc_exec() is None


def test_ensure_shell_returns_the_store_entry_and_never_downloads(monkeypatch, tmp_path):
    store = _stage(monkeypatch, tmp_path)
    fetched = []
    monkeypatch.setattr(mxc_host.urllib.request, "urlopen", lambda *a, **k: fetched.append(a))

    path, error = mxc_host.ensure_shell(download=True)
    assert (path, error) == (str(store / "busybox.exe"), None)
    assert fetched == []


def test_ensure_shell_reports_missing_instead_of_downloading(monkeypatch, tmp_path):
    _stage(monkeypatch, tmp_path, shell=False)
    fetched = []
    monkeypatch.setattr(mxc_host.urllib.request, "urlopen", lambda *a, **k: fetched.append(a))

    path, error = mxc_host.ensure_shell(download=True)
    assert path is None and "not installed" in error
    assert fetched == []


def test_status_names_the_store_when_the_kit_is_missing(monkeypatch, tmp_path):
    _stage(monkeypatch, tmp_path, kit=False)
    monkeypatch.setattr(mxc_host, "_IS_WINDOWS", True)
    record = mxc_host.status(settings=mxc_host.resolve_settings({}))
    assert record["available"] is False
    assert "pm store" in record["reason"]
    assert "mxc_wxc_exec_path" not in record["reason"]
