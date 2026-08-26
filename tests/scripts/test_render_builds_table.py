"""render-builds-table.py: tables from REAL asset names, spliced idempotently.

The contract: table rows exist only for assets that are actually on the
release (missing artifact = missing row, never a dead link), msixbundle /
zip / feed manifests stay out, and re-rendering replaces the previous block
instead of stacking a second one.

Adapted from the restack suite for this branch's artifact shapes: the
Windows per-arch artifact here is .msix (the msixbundle folds both arches
and stays out of the tables), not the NSIS .exe.
"""

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "render-builds-table.py"
_SPEC = importlib.util.spec_from_file_location("render_builds_table", _SCRIPT)
assert _SPEC and _SPEC.loader
rbt = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rbt)


ASSETS = [
    "HermesBundled-0.28.0-mac-arm64.dmg",
    "HermesBundled-0.28.0-mac-arm64.zip",           # updater delta target — no row
    "HermesBundled-0.28.0-win-x64.msix",
    "HermesBundled-0.28.0-win-arm64.msix",
    "HermesBundled-0.28.0-win.msixbundle",          # store/sideload channel — no row
    "HermesBundled-0.28.0-linux-x64.AppImage",
    "HermesLight-0.28.0-win-x64.msix",
    "latest.yml",                                   # feed manifest — no row
    "light.yml",
    "HermesBundled-0.28.0-win-x64.msix.blockmap",   # no row
]


class TestParseAssets:
    def test_only_table_shaped_assets_parse(self):
        parsed = rbt.parse_assets(ASSETS)
        assert ("mac", "arm64") in parsed["HermesBundled"]
        assert ("win", "x64") in parsed["HermesBundled"]
        assert ("win", "arm64") in parsed["HermesBundled"]
        assert ("linux", "x64") in parsed["HermesBundled"]
        assert len(parsed["HermesBundled"]) == 4   # zip/msixbundle/blockmap/yml excluded
        assert parsed["HermesLight"] == {("win", "x64"): ("HermesLight-0.28.0-win-x64.msix", "msix")}

    def test_nightly_versions_parse(self):
        parsed = rbt.parse_assets(["HermesBundled-0.28.0-nightly.20260818-win-x64.msix"])
        assert ("win", "x64") in parsed["HermesBundled"]


class TestRenderAndSplice:
    def test_rows_only_for_present_assets(self):
        block = rbt.render_tables(rbt.parse_assets(ASSETS), "v0.28.0", "NousResearch/hermes-agent")
        assert "HermesBundled-0.28.0-mac-arm64.dmg" in block
        assert "HermesBundled-0.28.0-win.msixbundle" not in block
        assert ".zip" not in block
        assert ".blockmap" not in block
        # A leg that never uploaded leaves no row at all.
        assert "linux-arm64" not in block

    def test_splice_replaces_marker_and_is_idempotent(self):
        body = f"# Notes\n\n{rbt.MARKER}\n\n## Changes"
        block = rbt.render_tables(rbt.parse_assets(ASSETS), "v0.28.0", "NousResearch/hermes-agent")
        once = rbt.splice(body, block)
        assert "## Downloads" in once
        assert once.count(rbt.MARKER) == 1
        # Second render (e.g. a re-run with more assets) replaces, not stacks.
        twice = rbt.splice(once, block)
        assert twice.count("## Downloads") == 1
        assert twice.count(rbt.END_MARKER) == 1

    def test_no_marker_leaves_body_alone(self):
        block = rbt.render_tables(rbt.parse_assets(ASSETS), "v0.28.0", "NousResearch/hermes-agent")
        assert rbt.splice("no marker here", block) == "no marker here"


class TestPendingPlaceholder:
    def test_final_render_replaces_the_pending_link(self):
        # The lifecycle: marker → pending link (builds-pending job) →
        # tables (builds-table job). The link must not survive step 3.
        body = f"# Notes\n\n{rbt.MARKER}\n\n## Changes"
        pending = rbt.render_pending("https://github.com/o/r/actions/runs/123")
        with_pending = rbt.splice(body, pending)
        assert "actions/runs/123" in with_pending
        assert with_pending.count(rbt.MARKER) == 1  # wrapper survives for step 3
        tables = rbt.render_tables(rbt.parse_assets(ASSETS), "v0.28.0", "NousResearch/hermes-agent")
        final = rbt.splice(with_pending, tables)
        assert "actions/runs/123" not in final
        assert "## Downloads" in final
        assert final.count(rbt.END_MARKER) == 1

    def test_pending_rerender_replaces_not_stacks(self):
        once = rbt.splice(rbt.MARKER, rbt.render_pending("https://x/runs/1"))
        twice = rbt.splice(once, rbt.render_pending("https://x/runs/2"))
        assert "https://x/runs/1" not in twice
        assert twice.count("https://x/runs/2") == 1
        assert twice.count(rbt.END_MARKER) == 1
