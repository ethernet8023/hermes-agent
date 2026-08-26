"""Recover from npm ``EBADENGINE`` failures with the pm-pinned npm.

The repo's ``.npmrc`` sets ``engine-strict=true`` and the root ``package.json``
pins an ``engines.npm`` range, so an npm outside that range aborts every
``npm ci`` / ``npm install`` we run inside the checkout::

    npm error code EBADENGINE
    npm error notsup Required: {"node":">=26.0.0","npm":">=12.0.0"}
    npm error notsup Actual:   {"npm":"10.9.8","node":"v22.23.1"}

Rather than predicting the failure (which would mean a semver range matcher and
an ``npm --version`` probe before work that usually succeeds), we react to it:
npm states the required range in the error, so the recovery reads the
constraint straight out of the output it just produced.

Scope of the repair is deliberately narrow. A system / nvm / brew / Nix npm
belongs to the user and their other projects; Hermes never modifies those.
When the failing npm is a foreign install, Hermes ensures its own pm-pinned
node/npm packages are installed and hands the caller pm's npm to retry with —
leaving the user's toolchain untouched. When the failing npm already *is*
pm's npm, the lockfile pin itself is out of range and no runtime action can
fix that; the caller gets the manual guidance instead.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

__all__ = [
    "is_ebadengine",
    "required_npm_range",
    "maybe_repair_npm_engine",
]

# npm prints `npm error notsup Required: {...}` on npm >= 10 and
# `npm ERR! notsup Required: {...}` on older releases.
_REQUIRED_RE = re.compile(r"Required:\s*(\{.*?\})")
_ACTUAL_RE = re.compile(r"Actual:\s*(\{.*?\})")


def is_ebadengine(output: str) -> bool:
    """Return True when *output* is an npm engine-compatibility failure."""
    if not output:
        return False
    return "EBADENGINE" in output or "Unsupported engine" in output


def _iter_required_blocks(output: str) -> list[dict]:
    blocks: list[dict] = []
    for match in _REQUIRED_RE.finditer(output or ""):
        try:
            parsed = json.loads(match.group(1))
        except ValueError:
            continue
        if isinstance(parsed, dict):
            blocks.append(parsed)
    return blocks


def required_npm_range(output: str) -> str | None:
    """Return the ``engines.npm`` range npm demanded in *output*.

    Returns ``None`` when the output has no engine failure, or when the
    failure is about Node rather than npm — upgrading npm cannot fix a Node
    version mismatch, so the caller must not try.

    When several packages report conflicting npm ranges the repo's own root
    constraint is preferred (it is the one we control); otherwise the first
    range wins, since any of them is a strict improvement over an npm that
    satisfies none.
    """
    if not is_ebadengine(output):
        return None
    ranges = [
        str(block["npm"]).strip()
        for block in _iter_required_blocks(output)
        if block.get("npm")
    ]
    if not ranges:
        return None
    distinct = list(dict.fromkeys(ranges))
    if len(distinct) > 1:
        repo_range = _repo_npm_range()
        if repo_range in distinct:
            return repo_range
    return distinct[0]


def actual_npm_version(output: str) -> str | None:
    """Return the npm version npm reported as ``Actual`` in *output*."""
    for match in _ACTUAL_RE.finditer(output or ""):
        try:
            parsed = json.loads(match.group(1))
        except ValueError:
            continue
        if isinstance(parsed, dict) and parsed.get("npm"):
            return str(parsed["npm"]).strip()
    return None


def _repo_npm_range() -> str | None:
    """Return ``engines.npm`` from the checkout's root ``package.json``."""
    package_json = Path(__file__).resolve().parent.parent / "package.json"
    try:
        data = json.loads(package_json.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    engines = data.get("engines")
    if not isinstance(engines, dict):
        return None
    value = engines.get("npm")
    return str(value).strip() if value else None


def _pm_npm(*, quiet: bool = False) -> str | None:
    """Install the pm-pinned node/npm packages and return pm's npm path."""
    if not quiet:
        print(
            "→ Provisioning the Hermes-pinned Node.js runtime "
            "(the resolved npm belongs to your system and is left alone)…",
            flush=True,
        )
    try:
        import pm

        pm.ensure("npm")
        from hermes_constants import _pm_node_executable

        managed = _pm_node_executable("npm")
    except Exception:
        managed = None
    if not managed and not quiet:
        print("  ✗ Managed Node.js provisioning failed", file=sys.stderr)
    return managed


def _print_manual_fix(npm: str, npm_range: str, actual: str | None) -> None:
    have = f"npm {actual} " if actual else "This npm "
    print(
        f"\n✗ {have}does not satisfy the range this project requires: {npm_range}\n"
        f"  Resolved npm: {npm}\n"
        "  Hermes could not provision its own Node.js runtime and never\n"
        "  modifies a system/nvm/brew/Nix npm. Upgrade yours yourself with:\n"
        f'      npm install -g npm@"{npm_range}"',
        file=sys.stderr,
    )


def maybe_repair_npm_engine(
    npm: str | None,
    output: str,
    *,
    quiet: bool = False,
) -> str | None:
    """Repair an ``EBADENGINE`` failure, never touching a foreign toolchain.

    *output* is the combined stdout/stderr of the npm command that just failed.
    Returns the npm executable the caller should retry its command with — the
    pm-pinned npm, freshly ensured, when the failing npm was a foreign install
    (system / nvm / brew / Nix installs are never modified). Returns ``None``
    when no repair happened — not an engine failure, the failing npm already
    was pm's own (the lockfile pin is out of range; a runtime install cannot
    fix that), or the pm install failed — leaving the original failure to
    stand.

    The returned value is truthy exactly when the caller should retry once,
    so ``if maybe_repair_npm_engine(...)`` call sites keep working; they just
    must run the retry with the returned path.
    """
    if not npm or not is_ebadengine(output):
        return None

    managed = _pm_npm(quiet=quiet)
    if managed:
        try:
            already_managed = Path(managed).resolve() == Path(npm).resolve()
        except OSError:
            already_managed = False
        if not already_managed:
            return managed

    npm_range = required_npm_range(output)
    if not quiet and npm_range:
        _print_manual_fix(npm, npm_range, actual_npm_version(output))
    return None
