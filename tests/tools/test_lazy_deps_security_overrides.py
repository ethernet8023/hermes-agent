"""Lazy installs must not downgrade a security-pinned package.

``uv pip install`` and ``pip install`` do not read ``[tool.uv]
override-dependencies``. A backend whose transitive dependencies cap a
pinned package below its patched version therefore downgrades the core venv
the first time a user enables that backend.

The measured case: the venv holds ``cryptography==50.0.0``, and enabling
DingTalk pulls ``alibabacloud-tea-openapi==0.4.5``, which caps
``cryptography<49``, so the install resolves 48.0.1 and re-opens
GHSA-m2h6-j472-rp4c, GHSA-jwv3-5hgf-82ww and CVE-2026-69247.

tools/lazy_deps.py reads the override list out of pyproject.toml and gives
it to each installer tier. These tests check that both tiers receive it.
There is no test that two lists agree, because there is one list.

The two tiers are reached by uv being present or absent, never by uv
running and failing: that outcome is authoritative and ends the ladder.
The last test holds that boundary, because a fallthrough there would
discard uv policy such as exclude-newer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import managed_uv
from tools import lazy_deps as ld

BACKEND = "alibabacloud-dingtalk==2.2.42"


@pytest.fixture
def install(monkeypatch):
    """Run ``_venv_pip_install`` with the ladder stubbed, capturing argv.

    The returned callable takes ``uv``, whether the ladder finds a uv binary,
    and ``uv_ok``, whether that uv succeeds. Temp files are read *during* the
    stubbed call, because ``_venv_pip_install`` unlinks them in its
    ``finally`` block.
    """

    def run(*, uv: bool, uv_ok: bool = True):
        calls: list[list[str]] = []
        contents: dict[str, str] = {}

        def fake_run(cmd, *a, **kw):
            cmd = list(cmd)
            calls.append(cmd)
            for flag in ("--overrides", "--constraint"):
                if flag in cmd:
                    p = Path(cmd[cmd.index(flag) + 1])
                    if p.exists():
                        contents[flag] = p.read_text(encoding="utf-8")

            class R:
                returncode = 0 if (uv_ok or "uv" not in cmd[0]) else 1
                stdout = "pip 24.0"
                stderr = "stubbed"

            return R()

        uv_bin = "/usr/bin/uv" if uv else None
        import installation.pip_ladder as ladder

        # The mechanics live in the shared ladder now: stub ITS
        # subprocess (lazy_deps' own is no longer the executor) and
        # its managed-uv lookup, or uv=False finds the real store uv.
        monkeypatch.setattr(ladder.subprocess, "run", fake_run)
        monkeypatch.setattr(ladder, "default_uv", lambda: uv_bin)
        monkeypatch.setattr(ld.subprocess, "run", fake_run)
        monkeypatch.setattr(ld.shutil, "which", lambda _n: uv_bin)
        monkeypatch.setattr(managed_uv, "resolve_uv", lambda: uv_bin)
        monkeypatch.delenv(ld._LAZY_TARGET_ENV, raising=False)
        import installation.tree as tree_mod

        monkeypatch.setattr(
            tree_mod, "runtime_tree", lambda _root: object(), raising=True
        )
        ld._venv_pip_install((BACKEND,))
        return calls, contents

    return run


class TestOverridesReachBothInstallerTiers:
    """Both tiers of the install ladder must receive the floor."""

    def test_uv_tier_receives_overrides_flag(self, install):
        calls, contents = install(uv=True)
        uv_calls = [c for c in calls if "uv" in c[0] and "pip" in c]
        assert uv_calls, f"no uv tier invocation captured: {calls}"
        cmd = uv_calls[0]
        assert "--overrides" in cmd, (
            f"uv tier must pass --overrides so [tool.uv] semantics apply: {cmd}"
        )
        body = contents.get("--overrides", "")
        for spec in ld._security_overrides():
            assert spec in body, (
                f"override {spec!r} missing from the file handed to uv: {body!r}"
            )

    def test_pip_tier_reasserts_the_floor_with_no_deps(self, install):
        """pip has no --overrides; it must re-assert the floor via --no-deps.

        Passing the floor as a --constraint instead would hold the pinned
        package but resolve the *backend* backwards, so the repair pass is the
        behaviour under test.
        """
        calls, _ = install(uv=False)
        repair = [c for c in calls if "install" in c and "--no-deps" in c]
        assert repair, (
            f"pip tier must re-assert security overrides with --no-deps: {calls}"
        )
        cmd = repair[0]
        for spec in ld._security_overrides():
            assert spec in cmd, (
                f"override {spec!r} missing from the pip repair pass: {cmd}"
            )

    def test_pip_repair_pass_does_not_reinstall_the_backend(self, install):
        """The repair pass must touch only the overridden packages.

        Including the backend specs would re-run resolution and undo the point
        of --no-deps.
        """
        calls, _ = install(uv=False)
        repair = [c for c in calls if "install" in c and "--no-deps" in c]
        assert repair
        assert BACKEND not in repair[0], (
            f"repair pass must not re-install the backend: {repair[0]}"
        )

    @pytest.mark.parametrize("uv", [True, False])
    def test_temp_files_are_cleaned_up(self, install, uv):
        calls, _ = install(uv=uv)
        for cmd in calls:
            for flag in ("--overrides", "--constraint"):
                if flag in cmd:
                    leaked = Path(cmd[cmd.index(flag) + 1])
                    assert not leaked.exists(), (
                        f"{flag} temp file leaked after install: {leaked}"
                    )

    @pytest.mark.parametrize("uv", [True, False])
    def test_specs_still_reach_the_installer(self, install, uv):
        """The override plumbing must not displace the actual packages."""
        calls, _ = install(uv=uv)
        # Exclude the --no-deps repair pass, which deliberately carries only
        # the overridden packages (see test_pip_repair_pass_does_not_reinstall).
        installs = [c for c in calls if "install" in c and "--no-deps" not in c]
        assert installs, f"no install invocation captured: {calls}"
        for cmd in installs:
            assert BACKEND in cmd, (
                f"requested spec missing from install command: {cmd}"
            )

    def test_failed_uv_does_not_fall_through_to_pip(self, install):
        """A uv resolver failure ends the ladder.

        pip reads neither exclude-newer nor override-dependencies, so a
        fallthrough here could install a release the project quarantined.
        The pip tier stays reachable through uv being absent, which the
        tests above use.
        """
        calls, _ = install(uv=True, uv_ok=False)
        assert all("uv" in c[0] for c in calls), (
            f"a failed uv run must not reach the pip tier: {calls}"
        )
