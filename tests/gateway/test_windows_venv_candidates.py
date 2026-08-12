"""Which venv roots the Windows gateway searches for site-packages.

``_ensure_windows_gateway_venv_imports`` patches ``sys.path`` so a detached
Windows gateway (running under uv's base ``pythonw.exe``) can still import
packages that live only in the venv — notably the MCP SDK. It probed
``<root>/venv`` and nothing else, so a uv-created ``.venv`` install matched
nothing, the patch silently no-opped, and MCP tool injection came up empty
with no error to explain it.

The candidate list is a pure function taking the root and VIRTUAL_ENV as
data, so these run on every host rather than faking ``sys.platform``.
"""

from pathlib import Path

from gateway.run import _windows_venv_candidates


class TestWindowsVenvCandidates:
    def test_probes_both_on_disk_layouts(self):
        root = Path("C:/hermes")
        assert _windows_venv_candidates(root) == [root / "venv", root / ".venv"]

    def test_virtual_env_outranks_both(self):
        # An explicit VIRTUAL_ENV is the launcher telling us exactly which
        # environment it meant; a guess from the tree never overrides it.
        root = Path("C:/hermes")
        candidates = _windows_venv_candidates(root, "D:/envs/custom")
        assert candidates[0] == Path("D:/envs/custom")
        assert root / "venv" in candidates
        assert root / ".venv" in candidates

    def test_empty_virtual_env_is_ignored(self):
        # An unset var arrives as "" through os.environ.get, and Path("")
        # resolves to the cwd — which would prepend a bogus candidate.
        root = Path("C:/hermes")
        assert _windows_venv_candidates(root, "") == [root / "venv", root / ".venv"]

    def test_dot_venv_is_reachable_at_all(self):
        # The regression itself: before the fix a .venv-only install had no
        # matching candidate, so the sys.path patch found nothing to add.
        root = Path("C:/hermes")
        assert root / ".venv" in _windows_venv_candidates(root)
