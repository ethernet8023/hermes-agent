"""Tests for the one-time bundled-desktop migration offer in ``hermes update``.

Covers ``_maybe_offer_bundled_migration`` (eligibility gates, prompt flow,
stamp semantics) and ``_bundled_release_asset`` (asset selection against the
real electron-builder artifact naming scheme).
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import hermes_cli.update_cmd as uc


# ── helpers ──────────────────────────────────────────────────────────────


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # hermes_constants caches nothing, but be explicit for readers: the
    # stamp path is resolved per call via get_hermes_home().
    import hermes_constants

    monkeypatch.setattr(
        hermes_constants, "get_hermes_home", lambda: home, raising=True
    )
    return home


def _stamp(hermes_home: Path) -> Path:
    return hermes_home / ".bundled-migration-prompted"


def _git_side_effect(branch="main", dirty_lines="", branches="main\n"):
    """Minimal subprocess.run side_effect for the offer's git probes."""

    def side_effect(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)
        if "rev-parse" in joined and "--abbrev-ref" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{branch}\n", stderr="")
        if "status" in joined and "--porcelain" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout=dirty_lines, stderr="")
        if "branch" in joined and "--list" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout=branches, stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return side_effect


def _run_offer(
    tmp_path,
    *,
    gateway_mode=False,
    assume_yes=False,
    tty=True,
    answer="n",
    branch="main",
    install="git",
    desktop=True,
    is_fork=False,
    origin="https://github.com/NousResearch/hermes-agent.git",
    dirty_lines="",
    branches="main\n",
    input_exc=None,
):
    """Drive _maybe_offer_bundled_migration with everything faked."""
    migrate_calls = []

    def fake_input(prompt=""):
        if input_exc is not None:
            raise input_exc
        return answer

    with patch.object(uc, "_do_bundled_migration", migrate_calls.append), \
         patch.object(uc, "_desktop_app_present", lambda d: desktop), \
         patch.object(uc.subprocess, "run",
                      side_effect=_git_side_effect(branch, dirty_lines, branches)), \
         patch.object(uc.sys.stdin, "isatty", lambda: tty, create=True), \
         patch.object(uc.sys.stdout, "isatty", lambda: tty, create=True), \
         patch("installation.tree.install_method", lambda p: install), \
         patch("builtins.input", fake_input):
        uc._maybe_offer_bundled_migration(
            ["git"],
            tmp_path,
            origin,
            is_fork,
            gateway_mode=gateway_mode,
            assume_yes=assume_yes,
        )
    return migrate_calls


# ── eligibility gates ────────────────────────────────────────────────────


def test_declining_writes_stamp_and_skips_migration(tmp_path, hermes_home, capsys):
    calls = _run_offer(tmp_path, answer="n")
    assert calls == []
    assert json.loads(_stamp(hermes_home).read_text()) == {
        "prompted": True,
        "accepted": False,
    }
    assert "Skipped" in capsys.readouterr().out


def test_accepting_writes_stamp_before_migration(tmp_path, hermes_home):
    """The stamp must land BEFORE _do_bundled_migration: the success path
    exits the process (sys.exit(0) after launching the installer), so a
    stamp written after the call would never land and the user would be
    re-prompted forever."""
    order = []

    def exiting_migration(root):
        order.append(("migrate", _stamp(hermes_home).exists()))
        raise SystemExit(0)

    with patch.object(uc, "_do_bundled_migration", exiting_migration), \
         patch.object(uc, "_desktop_app_present", lambda d: True), \
         patch.object(uc.subprocess, "run", side_effect=_git_side_effect()), \
         patch.object(uc.sys.stdin, "isatty", lambda: True, create=True), \
         patch.object(uc.sys.stdout, "isatty", lambda: True, create=True), \
         patch("installation.tree.install_method", lambda p: "git"), \
         patch("builtins.input", lambda prompt="": "y"):
        with pytest.raises(SystemExit):
            uc._maybe_offer_bundled_migration(
                ["git"], tmp_path, "https://github.com/NousResearch/hermes-agent.git",
                False, gateway_mode=False, assume_yes=False,
            )
    assert order == [("migrate", True)]
    assert json.loads(_stamp(hermes_home).read_text())["accepted"] is True


def test_stamped_user_is_never_reprompted(tmp_path, hermes_home, capsys):
    _stamp(hermes_home).write_text(json.dumps({"prompted": True, "accepted": False}))
    calls = _run_offer(tmp_path, answer="y")
    assert calls == []
    assert "bundled" not in capsys.readouterr().out.lower()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"gateway_mode": True},
        {"assume_yes": True},
        {"tty": False},
        {"gateway_mode": True, "assume_yes": True},
        {"tty": False, "assume_yes": True},
    ],
    ids=["gateway", "yes", "non-tty", "gateway+yes", "non-tty+yes"],
)
def test_non_interactive_contexts_never_prompt_or_migrate(
    tmp_path, hermes_home, capsys, kwargs
):
    """--yes means "don't block on prompts", never "migrate installs".
    The desktop bootstrap updater runs `hermes update --yes --gateway`
    (scripts/desktop-update/posix.sh, windows.ps1) — auto-accepting there
    would hijack every automated update into an installer download + exit."""
    calls = _run_offer(tmp_path, answer="y", **kwargs)
    assert calls == []
    assert "bundled" not in capsys.readouterr().out.lower()
    # And no stamp: the user was never asked.
    assert not _stamp(hermes_home).exists()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"branch": "dev"},
        {"install": "source"},
        {"install": "desktop-app"},
        {"is_fork": True},
        {"origin": None},
        {"desktop": False},
    ],
    ids=["branch", "unmanaged", "sealed", "fork", "no-origin", "no-desktop-build"],
)
def test_ineligible_checkouts_skip_silently(tmp_path, hermes_home, capsys, kwargs):
    calls = _run_offer(tmp_path, answer="y", **kwargs)
    assert calls == []
    assert "bundled" not in capsys.readouterr().out.lower()
    assert not _stamp(hermes_home).exists()


@pytest.mark.parametrize("exc", [KeyboardInterrupt, EOFError])
def test_interrupt_at_prompt_skips_without_stamp(tmp_path, hermes_home, capsys, exc):
    """Ctrl+C / EOF at the prompt is "not now", not "never": no stamp, no
    migration, no propagating exception (the git update continues)."""
    calls = _run_offer(tmp_path, input_exc=exc())
    assert calls == []
    assert not _stamp(hermes_home).exists()
    assert "Skipped" in capsys.readouterr().out


def test_dirty_tree_and_branches_warn_but_still_offer(tmp_path, hermes_home, capsys):
    calls = _run_offer(
        tmp_path,
        answer="n",
        dirty_lines=" M foo.py\n?? bar.py\n",
        branches="main\nfeature-x\n",
    )
    out = capsys.readouterr().out
    assert calls == []
    assert "2 uncommitted change(s)" in out
    assert "1 extra local branch(es)" in out


# ── asset selection ──────────────────────────────────────────────────────


_REAL_ASSETS = [
    # The real electron-builder naming scheme (verified against a live
    # release): HermesBundled-<semver>-<os>-<arch>.<ext>, linux x64 is
    # spelled x86_64, plus .blockmap/zip/msixbundle noise and the light
    # variant that must never be picked.
    "HermesBundled-0.27.0-linux-arm64.AppImage",
    "HermesBundled-0.27.0-linux-x86_64.AppImage",
    "HermesBundled-0.27.0-mac-arm64.dmg",
    "HermesBundled-0.27.0-mac-arm64.dmg.blockmap",
    "HermesBundled-0.27.0-mac-arm64.zip",
    "HermesBundled-0.27.0-mac-x64.dmg",
    "HermesBundled-0.27.0-win-arm64.exe",
    "HermesBundled-0.27.0-win-x64.exe",
    "HermesBundled-0.27.0-win.msixbundle",
    "HermesLight-0.27.0-mac-arm64.dmg",
    "HermesLight-0.27.0-win-x64.exe",
]


def _release_json():
    return {
        "assets": [
            {
                "name": n,
                "browser_download_url": f"https://example.invalid/{n}",
            }
            for n in _REAL_ASSETS
        ]
    }


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Darwin", "arm64", "HermesBundled-0.27.0-mac-arm64.dmg"),
        ("Darwin", "x86_64", "HermesBundled-0.27.0-mac-x64.dmg"),
        ("Windows", "ARM64", "HermesBundled-0.27.0-win-arm64.exe"),
        ("Windows", "AMD64", "HermesBundled-0.27.0-win-x64.exe"),
        ("Linux", "aarch64", "HermesBundled-0.27.0-linux-arm64.AppImage"),
        ("Linux", "x86_64", "HermesBundled-0.27.0-linux-x86_64.AppImage"),
    ],
)
def test_bundled_release_asset_matches_real_artifact_names(system, machine, expected):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(_release_json()).encode("utf-8")

    with patch("urllib.request.urlopen", return_value=_Resp()):
        found = uc._bundled_release_asset("v2026.8.18", system, machine)
    assert found is not None
    name, url = found
    assert name == expected
    assert url.endswith(expected)


def test_bundled_release_asset_none_when_release_has_no_assets():
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"assets": []}).encode("utf-8")

    with patch("urllib.request.urlopen", return_value=_Resp()):
        assert uc._bundled_release_asset("v2026.8.18", "Darwin", "arm64") is None


def test_bundled_release_asset_unsupported_platform():
    assert uc._bundled_release_asset("v1", "FreeBSD", "amd64") is None
