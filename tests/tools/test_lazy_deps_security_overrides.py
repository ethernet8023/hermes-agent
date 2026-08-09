"""Lazy installs must not downgrade security-pinned core packages.

``uv pip install`` / ``pip install`` do not read ``[tool.uv]
override-dependencies`` from pyproject.toml. A backend whose transitive deps
cap a security-pinned package below its patched floor therefore *downgrades*
the core venv the first time that backend is enabled.

The measured case: the core venv ships ``cryptography==50.0.0``; enabling
DingTalk pulls ``alibabacloud-dingtalk`` -> ``alibabacloud-tea-openapi==0.4.5``,
which caps ``cryptography<49``, and the install resolves ``cryptography`` back
to 48.0.1 — re-introducing GHSA-m2h6-j472-rp4c, GHSA-jwv3-5hgf-82ww and
CVE-2026-69247.

``tools/lazy_deps.py`` guards this by reading ``override-dependencies``
straight from pyproject.toml and passing it as an overrides file to every
lazy install. These tests assert the *contract* — the reader returns exactly
the pyproject list, the list is usable as requirement specs, and both
installer tiers receive it — rather than snapshotting the current overrides.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from hermes_cli import managed_uv
from tools import lazy_deps as ld


REPO_ROOT = Path(__file__).resolve().parents[2]


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _requirement_name(spec: str) -> str:
    head = spec.split(";", 1)[0].split("@", 1)[0].split("[", 1)[0]
    return _canonical(re.split(r"[=<>!~]", head, maxsplit=1)[0])


def _pyproject() -> dict:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


class TestOverridesComeFromPyproject:
    """The lazy path forces exactly what ``[tool.uv]`` forces — no drift."""

    def test_reader_returns_the_uv_override_list(self):
        """``_security_overrides()`` must return ``override-dependencies``
        verbatim. The single source of truth is pyproject.toml; a transform
        or filter here would reopen the gap this module closes.
        """
        expected = tuple(
            _pyproject().get("tool", {}).get("uv", {}).get("override-dependencies", [])
        )
        assert ld._security_overrides() == expected

    def test_reader_swallows_a_missing_pyproject(self, monkeypatch):
        """Outside a checkout the reader degrades to no overrides, not a crash."""
        real = Path.read_text

        def boom(self, *a, **kw):
            if self.name == "pyproject.toml":
                raise FileNotFoundError(self)
            return real(self, *a, **kw)

        monkeypatch.setattr(Path, "read_text", boom)
        assert ld._security_overrides() == ()

    def test_overrides_are_valid_requirement_specs(self):
        for spec in ld._security_overrides():
            assert _requirement_name(spec), f"unparseable override spec: {spec!r}"
            assert any(op in spec for op in ("==", ">=", "<", ">", "~=")), (
                f"override {spec!r} has no version constraint — it would not "
                "force anything"
            )

    def test_override_floor_is_not_below_the_core_pin(self):
        """An override must never permit a version older than the core pin.

        If pyproject pins ``cryptography==50.0.0`` but the override says
        ``>=48``, the lazy path can still install 48 and undo the pin.
        """
        core = {}
        for dep in _pyproject()["project"]["dependencies"]:
            m = re.match(r"^\s*([A-Za-z0-9._-]+)\s*==\s*([0-9][^\s,;]*)", dep)
            if m:
                core[_canonical(m.group(1))] = m.group(2)

        for spec in ld._security_overrides():
            name = _requirement_name(spec)
            pinned = core.get(name)
            if not pinned:
                continue
            lower = re.search(r">=\s*([0-9][^\s,]*)", spec)
            assert lower, (
                f"{name} is exact-pinned to {pinned} in pyproject but its "
                f"override {spec!r} has no lower bound"
            )
            got = tuple(int(x) for x in re.findall(r"\d+", lower.group(1)))
            want = tuple(int(x) for x in re.findall(r"\d+", pinned))
            assert got >= want[: len(got)], (
                f"override for {name} allows {lower.group(1)}, which is "
                f"below the core pin {pinned}"
            )


class TestOverridesReachBothInstallerTiers:
    """Both tiers of the install ladder must receive the floor.

    The two tiers are reached by uv being present or absent, never by uv
    running and failing: that outcome is authoritative and ends the ladder.
    The last test holds that boundary, because a fallthrough there would
    discard uv policy such as exclude-newer.
    """

    BACKEND = "alibabacloud-dingtalk==2.2.42"

    @pytest.fixture
    def install(self, monkeypatch):
        """Run ``_venv_pip_install`` with the ladder stubbed, capturing argv.

        The returned callable takes ``uv``, whether the ladder finds a uv
        binary, and ``uv_ok``, whether that uv succeeds. Temp files are read
        *during* the stubbed call, because ``_venv_pip_install`` unlinks them
        in its ``finally`` block.
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
            ld._venv_pip_install((self.BACKEND,))
            return calls, contents

        return run

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

    def test_pip_tier_receives_the_floor_as_a_constraint(self, install):
        """pip has no --overrides; it must still get the floor via --constraint."""
        calls, contents = install(uv=False)
        pip_installs = [
            c for c in calls if "-m" in c and "pip" in c and "install" in c
        ]
        assert pip_installs, f"no pip tier install captured: {calls}"
        cmd = pip_installs[0]
        assert "--constraint" in cmd, (
            f"pip tier must receive the security floor as a constraint: {cmd}"
        )
        body = contents.get("--constraint", "")
        for spec in ld._security_overrides():
            assert spec in body

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
        installs = [c for c in calls if "install" in c]
        assert installs, f"no install invocation captured: {calls}"
        for cmd in installs:
            assert self.BACKEND in cmd, (
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
