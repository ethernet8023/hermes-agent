"""The chromium pin and playwright's browsers.json cannot drift apart.

The pin table stages chromium + chromium-headless-shell into the shared
tool store under playwright's OWN directory names, and playwright picks
which revision to look for from the browsers.json inside the
playwright-core that npm resolved from the ROOT package-lock.json. That
lock is the revision authority (grounded 2026-08-14: python-playwright
does not exist in pyproject; the installers run `npx playwright install`
against the root node_modules).

If someone bumps playwright without the pin, every install keeps staging
a browser playwright will refuse to see (wrong dir name) and headless
launches re-download at runtime — the exact unpinned behavior the pin
exists to kill. If someone bumps the pin without playwright, same
failure mirrored: the staged revision is invisible to the resolver.

So: same commit, both files, and this test is what enforces it. It reads
data files only — no imports of playwright, no network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PINS = REPO_ROOT / "installation" / "runtime-pins.json"
BROWSERS_JSON = (
    REPO_ROOT / "node_modules" / "playwright-core" / "browsers.json"
)

BROWSER_TOOLS = ("chromium", "chromium-headless-shell")


def _pins() -> dict:
    return json.loads(PINS.read_text(encoding="utf-8"))["tools"]


def _browsers() -> dict[str, dict]:
    if not BROWSERS_JSON.exists():
        # Hermetic runners may not have node_modules. The lockstep is
        # still enforced where it matters: any environment that can RUN
        # playwright (dev machines, the JS lanes, the installers' own
        # smoke) has the file, and a pin/lock bump PR runs those.
        pytest.skip("root node_modules not installed — browsers.json absent")
    data = json.loads(BROWSERS_JSON.read_text(encoding="utf-8"))
    return {b["name"]: b for b in data["browsers"]}


class TestChromiumPinLockstep:
    def test_pin_revision_matches_playwrights_expectation(self):
        """The staged dir name is derived from the pin version; playwright
        looks for the revision in ITS browsers.json. Equal or invisible."""
        pins = _pins()
        browsers = _browsers()
        for tool in BROWSER_TOOLS:
            assert pins[tool]["version"] == browsers[tool]["revision"], (
                f"{tool}: pin stages revision {pins[tool]['version']} but the "
                f"root package-lock's playwright-core expects "
                f"{browsers[tool]['revision']}. Bump installation/runtime-pins.json "
                f"and the root playwright dependency in the SAME commit — "
                f"re-ground the CDN digests while you're there "
                f"(cdn.playwright.dev, see the pin's $comment for URL shapes)."
            )

    def test_the_two_browser_pins_are_one_revision(self):
        """chromium and its headless shell ship as a pair — playwright
        treats them as one browser split across two directories."""
        pins = _pins()
        assert pins["chromium"]["version"] == pins["chromium-headless-shell"]["version"]

    def test_cft_urls_carry_the_browser_version_playwright_records(self):
        """The CfT download paths are keyed by browserVersion, not
        revision — a pin bump that edits the revision but forgets the
        URLs would download the OLD chromium and verify it happily."""
        pins = _pins()
        browsers = _browsers()
        browser_version = browsers["chromium"]["browserVersion"]
        for tool in BROWSER_TOOLS:
            for target, spec in pins[tool]["files"].items():
                if "missing" in spec:
                    # A declared gap has no url to check (win32-arm64: no
                    # upstream build exists on either CDN).
                    continue
                url = spec["url"]
                if "/builds/cft/" in url:
                    assert f"/builds/cft/{browser_version}/" in url, (
                        f"{tool}/{target}: CfT url {url} does not carry "
                        f"browsers.json's browserVersion {browser_version}"
                    )
                else:
                    # Classic revision path (linux-arm64).
                    assert f"/{pins[tool]['version']}/" in url

    def test_browser_pins_are_optional_and_off_path(self):
        """A browser tree is ~180MB nobody PATH-resolves into: staging it
        eagerly or exposing it as a CLI surface would both be wrong."""
        pins = _pins()
        for tool in BROWSER_TOOLS:
            assert pins[tool].get("optional") is True
            assert pins[tool].get("onPath") is False

    def test_store_entries_land_on_playwrights_dir_names(self):
        """The whole design hangs on this rename — playwright resolves
        chromium_headless_shell-<rev>, never our <tool>-<ver>-<target>."""
        import sys

        sys.path.insert(0, str(REPO_ROOT))
        from installation.registry import store_entry_name

        pins = _pins()
        rev = pins["chromium"]["version"]
        assert store_entry_name("chromium", rev, "linux-x64") == f"chromium-{rev}"
        assert (
            store_entry_name("chromium-headless-shell", rev, "linux-x64")
            == f"chromium_headless_shell-{rev}"
        )
        # And the rename is SCOPED: every other tool keeps the full tuple.
        assert store_entry_name("node", "26.7.0", "linux-x64") == "node-26.7.0-linux-x64"
