"""Install-scoped runtime tool registry.

The single source of truth for which tool versions THIS install of Hermes
manages and where they live. Two files, one owner each:

- ``<repo>/runtime-pins.json`` — the PINS. Every tool pins an EXACT
  version plus, per target, the exact download URL and its sha256.
  Versioned in the repo, code-reviewed, updated with the code that needs
  them. A tool may also declare ``extends``, naming the tools it plugs
  into; ORDER is derived from those edges rather than restated as a list
  in each reader (see ``install_order`` / ``path_order``).
- ``<install>/.hermes-runtime/runtimes.json`` — the FACTS. What is
  actually installed: version, path relative to the TOOL STORE, install
  timestamp, and the derived PATH order. Written ONLY by the
  provisioner; everything else reads.
- ``~/.hermes/tools/<tool>-<version>-<target>/`` — the BYTES. One entry
  per pinned tuple, shared by every install on the machine. Entries are
  immutable: a version bump writes a NEW entry and repoints the fact,
  because another install may be running the old one right now.

Facts and bytes are separate so that N installs cost N small JSON files
and ONE copy of node. Nothing on disk links the two — a fact names a
store-relative path, so the facts file IS the indirection layer.
``installation.paths.resolve_bases`` answers "which facts dir, which
store" for every reader, so none of them can disagree.

Readers (locators, the PATH assembler, doctor, uninstall) consume facts
through this module instead of probing paths. No path literals anywhere
else — that scatter is exactly what this replaces.

**Exact pins only, by design.** There is no version-range grammar and no
"resolve latest, then check it satisfies a range": that shape needs a
GitHub API call per tool (60 requests/hour unauthenticated), makes two
builds of the same commit disagree, and lets a tool change under users
without a code review. A pin bump is a deliberate edit — new version, new
urls, new digests, verified, committed.

Design doc: ``.hermes/plans/2026-08-12_hermes-home-lifetime-split.md``.

Pure logic (pin/facts parsing, target resolution, round-trip) lives here
with no side effects beyond explicit ``save_facts`` calls, so it is fully
unit-testable without a network or a real install.
"""

from __future__ import annotations

import json
import os
import platform
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from installation.paths import get_runtime_dir, resolve_bases

PINS_FILENAME = "runtime-pins.json"
FACTS_FILENAME = "runtimes.json"
FACTS_SCHEMA_VERSION = 2
PINS_SCHEMA_VERSION = 2

__all__ = [
    "FACTS_FILENAME",
    "FACTS_SCHEMA_VERSION",
    "PINS_FILENAME",
    "PINS_SCHEMA_VERSION",
    "PinnedFile",
    "RuntimeFact",
    "UnavailableOnTarget",
    "current_target",
    "facts_path",
    "install_order",
    "load_facts",
    "load_pins",
    "path_order",
    "pinned_file",
    "pins_path",
    "record_fact",
    "save_facts",
    "store_entry_name",
    "tool_bin_dir",
    "tool_path",
]

# The files-table key for an artifact whose bytes are the same everywhere
# (a registry/source tarball). Distinct from a per-target key so a tool
# cannot half-declare one: `files` is either keyed by target, or it is
# this single key.
ANY_TARGET = "any"


# ─── targets ────────────────────────────────────────────────────────────────


def current_target() -> str:
    """This host as a pin-table target key: ``<platform>-<arch>``.

    Node/Python spellings (darwin|linux|win32 x arm64|x64) so one string
    works on both sides of the JS/Python boundary.
    """
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        arch = "arm64"
    elif machine in ("x86_64", "amd64", "x64"):
        arch = "x64"
    else:
        raise RuntimeError(f"unsupported architecture: {platform.machine()}")

    if sys.platform.startswith("win"):
        return f"win32-{arch}"
    if sys.platform == "darwin":
        return f"darwin-{arch}"
    return f"linux-{arch}"


# ─── pins (repo-owned, exact) ───────────────────────────────────────────────


@dataclass(frozen=True)
class PinnedFile:
    """One tool's download for one target: exactly where and exactly what.

    ``also`` carries further archives unpacked into the SAME store entry
    after this one — for an upstream that splits a single runtime across
    downloads that must land in one directory. Empty for every tool whose
    artifact stands alone, which is nearly all of them.
    """

    version: str
    url: str
    sha256: str
    also: tuple["PinnedFile", ...] = ()

    @property
    def filename(self) -> str:
        return self.url.rsplit("/", 1)[-1]

def pins_path(install_root: Path | None = None) -> Path:
    """Path to the pin table.

    Pins ship WITH the code, so the default is this package's own
    directory: the table is package data, versioned and reviewed with the
    module that reads it, rather than a repo-root file this module has to
    reach up for. ``get_install_root()`` is deliberately not consulted —
    that is where tools get INSTALLED, and callers may point it elsewhere.

    ``HERMES_RUNTIME_PINS`` overrides that for packagers who ship the
    table into its own store path. The explicit *install_root* argument
    still wins, because a caller naming a root means that root.
    """
    if install_root is not None:
        return install_root / PINS_FILENAME
    override = os.getenv("HERMES_RUNTIME_PINS", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / PINS_FILENAME


# Loopback http is allowed so tests can serve real archives from a local
# server and exercise the true download path. Everything a user ever
# fetches is https: a plain-http pin would let a network attacker choose
# the bytes, and the digest check alone cannot help if the attacker also
# picks which digest you compare against.
_LOOPBACK_PREFIXES = ("http://127.0.0.1:", "http://localhost:", "http://[::1]:")


def _is_allowed_url(url: str) -> bool:
    return url.startswith("https://") or url.startswith(_LOOPBACK_PREFIXES)


def load_pins(install_root: Path | None = None) -> dict[str, dict]:
    """Load the repo's pin table: tool name → entry with version + files.

    Raises on missing/malformed: the pins ship with the code, so absence
    means a broken install, not a fresh one. Validation is eager and
    total — a typo in a digest should fail at load, not halfway through a
    user's first launch.

    One rule lives in ``runtime-pins.schema.json`` instead of here: that a
    per-target entry names EVERY target, each with an artifact or a
    reasoned gap. The schema validates the shipping table, where a
    silently absent row and a deliberate gap read alike and only one is a
    bug. This function also loads the small single-target tables that
    tests and tools build by hand, and the provisioner only ever resolves
    the one target it runs on.
    """
    path = pins_path(install_root)
    data = json.loads(path.read_text(encoding="utf-8"))

    schema = data.get("schemaVersion")
    if schema != PINS_SCHEMA_VERSION:
        raise ValueError(
            f"{path}: pins schemaVersion {schema!r}, expected {PINS_SCHEMA_VERSION}"
        )

    tools = data.get("tools")
    if not isinstance(tools, dict) or not tools:
        raise ValueError(f"{path}: no 'tools' table")

    for name, entry in tools.items():
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: tool {name!r} is not an object")
        version = entry.get("version")
        if not isinstance(version, str) or not version:
            raise ValueError(f"{path}: tool {name!r} has no exact version")
        extends = entry.get("extends", [])
        if not isinstance(extends, list) or not all(
            isinstance(dep, str) for dep in extends
        ):
            raise ValueError(f"{path}: tool {name!r} 'extends' must be a list of names")
        optional = entry.get("optional", False)
        if not isinstance(optional, bool):
            raise ValueError(f"{path}: tool {name!r} 'optional' must be true or false")
        python = entry.get("python")
        if python is not None:
            # Decision 3: the interpreter pin rides the uv entry
            # (extends-style — uv is what installs it). Exact X.Y.Z, no
            # sha256 of our own: uv's python-build-standalone pins carry
            # their checksums, and duplicating them here would drift.
            if name != "uv":
                raise ValueError(
                    f"{path}: tool {name!r} carries a 'python' pin; only 'uv' "
                    f"may (uv is the installer the pin configures)"
                )
            if not isinstance(python, str) or len(python.split(".")) != 3 or not all(
                part.isdigit() for part in python.split(".")
            ):
                raise ValueError(
                    f"{path}: python pin must be exact X.Y.Z, got {python!r}"
                )
        for dep in extends:
            if dep not in tools:
                raise ValueError(
                    f"{path}: tool {name!r} extends {dep!r}, which is not pinned"
                )
            if dep == name:
                raise ValueError(f"{path}: tool {name!r} extends itself")
        requires = entry.get("requires", [])
        if not isinstance(requires, list) or not all(
            isinstance(dep, str) for dep in requires
        ):
            raise ValueError(
                f"{path}: tool {name!r} 'requires' must be a list of names"
            )
        for dep in requires:
            if dep not in tools:
                raise ValueError(
                    f"{path}: tool {name!r} requires {dep!r}, which is not pinned"
                )
            if dep == name:
                raise ValueError(f"{path}: tool {name!r} requires itself")
        files = entry.get("files")
        if not isinstance(files, dict) or not files:
            raise ValueError(f"{path}: tool {name!r} has no 'files' table")
        if ANY_TARGET in files and len(files) > 1:
            # The one legal mix: 'any' plus DECLARED GAPS. The artifact is
            # target-independent, but a target can still be excluded from
            # it (agent-browser's registry tarball ships every platform's
            # binary EXCEPT win32-arm64). Any other per-target row next to
            # 'any' is a contradiction.
            for extra_target, spec in files.items():
                if extra_target == ANY_TARGET:
                    continue
                if not (isinstance(spec, dict) and "missing" in spec):
                    raise ValueError(
                        f"{path}: tool {name!r} mixes {ANY_TARGET!r} with per-target files; "
                        f"one artifact serves every target, or each target names its own "
                        f"(only a declared gap may sit beside {ANY_TARGET!r})"
                    )
        declared_missing = {
            target for target, spec in files.items()
            if isinstance(spec, dict) and "missing" in spec
        }
        if declared_missing and not entry.get("optional", False):
            # A REQUIRED tool with a hole would brick the whole install
            # on that platform; only capability tools may declare gaps.
            raise ValueError(
                f"{path}: tool {name!r} is required but declares missing targets "
                f"({', '.join(sorted(declared_missing))})"
            )
        for target, spec in files.items():
            if not isinstance(spec, dict):
                raise ValueError(f"{path}: {name}/{target} is not an object")
            if "missing" in spec:
                # A declared gap must SAY WHY — the reason string is what
                # separates "upstream ships no such build" from "someone
                # forgot a row", and the resolver shows it when refusing.
                reason = spec["missing"]
                if not isinstance(reason, str) or not reason:
                    raise ValueError(
                        f"{path}: {name}/{target} 'missing' needs a reason"
                    )
                if set(spec) != {"missing"}:
                    raise ValueError(
                        f"{path}: {name}/{target} is both missing and pinned"
                    )
                continue
            url = spec.get("url")
            sha256 = spec.get("sha256")
            if not isinstance(url, str) or not _is_allowed_url(url):
                raise ValueError(f"{path}: {name}/{target} needs an https url")
            if not isinstance(sha256, str) or len(sha256) != 64:
                raise ValueError(
                    f"{path}: {name}/{target} sha256 must be 64 hex chars"
                )
            # Extras unpack into the same entry, so they are the same trust
            # decision as the primary artifact and get the same eager check.
            # An unvalidated 'also' url would be a hole in exactly the wall
            # this loader exists to be.
            also = spec.get("also", [])
            if not isinstance(also, list):
                raise ValueError(f"{path}: {name}/{target} 'also' must be a list")
            for index, extra in enumerate(also):
                if not isinstance(extra, dict):
                    raise ValueError(
                        f"{path}: {name}/{target} also[{index}] is not an object"
                    )
                extra_url = extra.get("url")
                extra_sha = extra.get("sha256")
                if not isinstance(extra_url, str) or not _is_allowed_url(extra_url):
                    raise ValueError(
                        f"{path}: {name}/{target} also[{index}] needs an https url"
                    )
                if not isinstance(extra_sha, str) or len(extra_sha) != 64:
                    raise ValueError(
                        f"{path}: {name}/{target} also[{index}] sha256 must be "
                        f"64 hex chars"
                    )

    # Cycles are rejected at load, not discovered halfway through a user's
    # first launch: install_order() must always terminate.
    install_order(tools, _source=path)
    return tools


def _extends(tool: str, pins: dict[str, dict]) -> list[str]:
    return list(pins.get(tool, {}).get("extends", []))


def pinned_python(install_root: Path | None = None) -> Optional[str]:
    """The exact interpreter version this install standardizes on.

    Rides the uv pin entry (decision 3): uv is what installs it, via
    ``uv python install`` with ``UV_PYTHON_INSTALL_DIR`` pointed at the
    managed runtime dir. None when the table predates the pin — callers
    fall back to their historical version literals.

    Staleness for the interpreter is a VERSION probe, not a digest:
    uv's python-build-standalone pins carry their own checksums.
    """
    pins = load_pins(install_root)
    python = pins.get("uv", {}).get("python")
    return python if isinstance(python, str) else None


def is_optional(tool: str, pins: dict[str, dict]) -> bool:
    """True when *tool* is provisioned on demand rather than for everyone.

    A required tool is part of every install: the sweep brings it to its
    pin and a failure fails the install. An OPTIONAL tool backs a
    capability the user may never touch (a browser engine), so it is
    staged only when something asks for it — and, once staged, it is
    recorded in the facts like any other tool, which is what makes a
    later sweep keep it at its pin.
    """
    return bool(pins.get(tool, {}).get("optional", False))


def extends_closure(tool: str, pins: dict[str, dict]) -> set[str]:
    """*tool* plus every tool it transitively extends.

    Staging a tool may RUN the tools it extends, so "provision just this
    one" really means "provision this one and the chain under it".
    ``load_pins`` rejects cycles, so the walk terminates.
    """
    seen: set[str] = set()
    stack = [tool]
    while stack:
        name = stack.pop()
        if name in seen or name not in pins:
            continue
        seen.add(name)
        stack.extend(_extends(name, pins))
    return seen


def requires_closure(tool: str, pins: dict[str, dict]) -> set[str]:
    """*tool* plus every tool it transitively requires.

    ``requires`` drives SELECTION and nothing else: asking for a tool
    means asking for what it cannot function without (agent-browser
    launches a Chromium; a driver with no browser is not a configuration
    anyone wants). Deliberately a separate walk from ``extends_closure``
    — same shape, different meaning. ``extends`` also drives install
    order and PATH precedence (the extender supersedes a copy the
    extended tool ships); merging the two edges is how those
    consequences would leak onto tools that only need co-selection.
    The walk tolerates a hypothetical cycle (seen-set) rather than
    relying on table validation for termination.
    """
    seen: set[str] = set()
    stack = [tool]
    while stack:
        name = stack.pop()
        if name in seen or name not in pins:
            continue
        seen.add(name)
        stack.extend(pins[name].get("requires", []))
    return seen


def _ordered_by(
    pins: dict[str, dict],
    blockers: dict[str, list[str]],
    _source: Path | str | None = None,
) -> list[str]:
    """Emit every tool after the tools listed as its blockers.

    Stable: at each step the FIRST still-blocked-free tool in pin-table
    order wins, so tools with no relationship keep the order they were
    written in. Order that is arbitrary should not churn between runs —
    a reordered PATH would otherwise show up as a diff on every edit.
    """
    emitted: list[str] = []
    remaining = list(pins)
    while remaining:
        ready = next(
            (t for t in remaining if all(b in emitted for b in blockers[t])), None
        )
        if ready is None:
            where = f"{_source}: " if _source is not None else ""
            raise ValueError(
                f"{where}'extends' cycle among {', '.join(sorted(remaining))}"
            )
        emitted.append(ready)
        remaining.remove(ready)
    return emitted


def install_order(
    pins: dict[str, dict], _source: Path | str | None = None
) -> list[str]:
    """Tool names ordered so every tool follows what it extends.

    Staging a tool may RUN the tools it extends (npm is unpacked by the
    node it extends), so the dependency edge is a real ordering
    constraint, not a preference.
    """
    blockers = {tool: _extends(tool, pins) for tool in pins}
    return _ordered_by(pins, blockers, _source)


def path_order(pins: dict[str, dict]) -> list[str]:
    """Tool names ordered for PATH assembly: extenders before extended.

    A tool that extends another exists to supersede a copy that other
    one ships, so it has to be FOUND first — npm ahead of node, or
    node's bundled npm wins. Same edge as ``install_order``, read the
    other way: one declaration in the pin table, both consequences
    derived, so they cannot drift apart.

    Tools pinned with ``onPath: false`` are excluded entirely: a
    playwright browser tree is program DATA a library resolves by
    directory, not a CLI surface — putting ``chrome`` ahead of the
    user's own on PATH would be a hijack, not a convenience. Exclusion
    here covers every consumer at once, because the facts' recorded
    ``pathOrder`` (which the TS reader also trusts) is derived from
    this list.
    """
    blockers: dict[str, list[str]] = {
        tool: [] for tool in pins if pins[tool].get("onPath", True)
    }
    for tool in blockers:
        for dep in _extends(tool, pins):
            if dep in blockers:
                blockers[dep].append(tool)
    return _ordered_by({tool: pins[tool] for tool in blockers}, blockers)


class UnavailableOnTarget(KeyError):
    """A DECLARED gap: this tool has no artifact for this target, on purpose.

    Raised by ``pinned_file`` when the pin table carries a
    ``{"missing": reason}`` row — upstream genuinely ships no build, or the
    table chose not to bundle one. The type IS the classification (a plain
    ``KeyError`` remains "the table has a hole", which is a bug to fix):
    consumers branch on the exception class, never on message text.
    """

    def __init__(self, tool: str, target: str, reason: str):
        super().__init__(f"{tool!r} has no build for {target}: {reason}")
        self.tool = tool
        self.target = target
        self.reason = reason

    def __str__(self) -> str:
        # KeyError.__str__ is repr(args[0]), which wraps the message in
        # quotes; a declared gap is prose for humans, not a mapping key.
        return self.args[0]


def pinned_file(
    tool: str,
    target: str | None = None,
    install_root: Path | None = None,
    pins: dict[str, dict] | None = None,
) -> PinnedFile:
    """The exact download for *tool* on *target* (default: this host).

    Raises ``UnavailableOnTarget`` for a DECLARED gap (a
    ``{"missing": reason}`` row) and plain ``KeyError`` when the tool or
    target simply is not in the table — an unpinned platform is a gap in
    the table to fill, not something to guess a URL for. A tool whose
    artifact is target-independent pins the single ``any`` key and
    resolves to it for every target.
    """
    table = pins if pins is not None else load_pins(install_root)
    entry = table.get(tool)
    if entry is None:
        raise KeyError(f"{tool!r} is not in the pin table")

    files = entry["files"]
    key = target or current_target()
    # An explicit row for the key wins over 'any': the only legal mix is
    # 'any' + declared gaps, so this is how a target-independent artifact
    # still excludes a target it does not serve.
    spec = files.get(key)
    if spec is None:
        spec = files.get(ANY_TARGET)
    if spec is None:
        raise KeyError(f"{tool!r} has no pinned download for {key}")
    if "missing" in spec:
        raise UnavailableOnTarget(tool, key, spec["missing"])

    return PinnedFile(
        version=entry["version"],
        url=spec["url"],
        sha256=spec["sha256"],
        also=tuple(
            PinnedFile(version=entry["version"], url=extra["url"], sha256=extra["sha256"])
            for extra in spec.get("also", ())
        ),
    )


# ─── facts (install-owned, provisioner-written) ─────────────────────────────


@dataclass
class RuntimeFact:
    """One installed tool as recorded in runtimes.json."""

    version: str
    # RELATIVE to the tool store: ``<tool>-<version>-<target>/<binary>``.
    # Relative rather than absolute so one facts file stays valid when the
    # store moves (a different HOME, a packaged bundle that IS its own
    # store), and so nothing has to rewrite N installs' facts to relocate.
    #
    # EXCEPTION: a ``source: "system"`` fact records a tool the machine
    # already had (decision 1: system-git-first), and its path is
    # ABSOLUTE — there is no store entry to be relative to. Old readers
    # degrade safely by accident of path semantics: pathlib's ``/`` lets
    # an absolute right side win, so ``store / "/usr/bin/git"`` IS
    # ``/usr/bin/git``, and Node's ``path.join`` concatenates into a
    # path that does not exist, so the TS reader skips the entry.
    path: str
    installed_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    # Optional override: PATH dirs (relative to the store) for tools
    # whose surface spans several bin dirs (PortableGit: cmd, bin,
    # usr/bin). When None, the assembler derives the single dir containing
    # `path`.
    path_dirs: Optional[list[str]] = None
    # "managed" = the provisioner published these bytes into the store,
    # digest-verified. "system" = a machine-provided binary accepted by
    # the version-floor probe; not ours, never on the managed PATH, and
    # never handed tool-specific env (a system git resolves its own
    # helpers; exporting GIT_EXEC_PATH at it would break it).
    source: str = "managed"

    def to_json(self) -> dict:
        data: dict[str, object] = {
            "version": self.version,
            "path": self.path,
            "installedAt": self.installed_at,
        }
        if self.path_dirs is not None:
            data["pathDirs"] = self.path_dirs
        # Written only when it deviates: every fact ever recorded before
        # this field existed was managed, so absence == managed keeps old
        # files readable without a schema bump.
        if self.source != "managed":
            data["source"] = self.source
        return data

    @classmethod
    def from_json(cls, data: dict) -> "RuntimeFact":
        return cls(
            version=data["version"],
            path=data["path"],
            installed_at=data.get("installedAt", ""),
            path_dirs=data.get("pathDirs"),
            source=data.get("source", "managed"),
        )


def facts_path(runtime_dir: Path | None = None) -> Path:
    base = runtime_dir if runtime_dir is not None else get_runtime_dir()
    return base / FACTS_FILENAME


def load_facts(runtime_dir: Path | None = None) -> dict[str, RuntimeFact]:
    """Load installed-tool facts. Missing file → empty dict (nothing
    provisioned yet — a normal state, unlike missing pins)."""
    path = facts_path(runtime_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if raw.get("schemaVersion") != FACTS_SCHEMA_VERSION:
        # A foreign/older facts file: treat as unprovisioned. The
        # provisioner rewrites it wholesale; readers never limp along on
        # a shape they don't understand.
        return {}
    return {
        name: RuntimeFact.from_json(entry)
        for name, entry in raw.get("tools", {}).items()
    }


def load_path_order(runtime_dir: Path | None = None) -> list[str]:
    """The PATH assembly order the provisioner derived from the pins.

    Written into the facts so both readers (installation/env.py and
    apps/desktop/electron/backend-env.ts) consume the SAME data rather
    than each restating a literal list that has to be kept in sync by
    hand. Empty when nothing is provisioned yet.
    """
    path = facts_path(runtime_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if raw.get("schemaVersion") != FACTS_SCHEMA_VERSION:
        return []
    order = raw.get("pathOrder")
    if isinstance(order, list) and all(isinstance(name, str) for name in order):
        return order
    # No recorded order: fall back to the tool names as written. Facts are
    # provisioner-written and always carry pathOrder, so this only covers
    # a hand-edited file, where insertion order is the best guess left.
    return list(raw.get("tools", {}))


def save_facts(
    facts: dict[str, RuntimeFact],
    runtime_dir: Path | None = None,
    path_order: list[str] | None = None,
) -> Path:
    """Write the facts file atomically (tmp + rename). Provisioner-only.

    *path_order* is the pin-derived PATH assembly order; it is recorded so
    readers in both languages consume one answer. Omitted only by callers
    that are updating a single fact and have no pin table in hand, in
    which case any previously recorded order is preserved.
    """
    path = facts_path(runtime_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path_order is None:
        # Preserve a previously recorded order (a single-fact update has
        # no pin table in hand). With nothing recorded either, fall back
        # to the facts' own keys: an order that lists no tools would drop
        # every managed tool off PATH, which is worse than an arbitrary
        # one. Anything the fallback misses is appended for the same
        # reason — EXCEPT tools the pin table deliberately keeps off PATH
        # (onPath: false): the append is a safety net for legacy facts,
        # not a bypass of the exclusion. The pin lookup is best-effort;
        # with no readable table nothing is known to be excluded and the
        # old behavior stands.
        path_order = load_path_order(runtime_dir)
    try:
        pins = load_pins()
        off_path = {name for name, entry in pins.items()
                    if not entry.get("onPath", True)}
    except Exception:  # noqa: BLE001 — the append fallback predates pins
        off_path = set()
    ordered = [name for name in path_order if name in facts and name not in off_path]
    ordered += [
        name for name in facts if name not in ordered and name not in off_path
    ]
    payload = {
        "schemaVersion": FACTS_SCHEMA_VERSION,
        "pathOrder": ordered,
        "tools": {name: fact.to_json() for name, fact in sorted(facts.items())},
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def record_fact(
    name: str,
    version: str,
    rel_path: str,
    runtime_dir: Path | None = None,
) -> dict[str, RuntimeFact]:
    """Read-modify-write one tool's fact. Returns the updated table."""
    facts = load_facts(runtime_dir)
    facts[name] = RuntimeFact(version=version, path=rel_path)
    save_facts(facts, runtime_dir)
    return facts


# ─── the tool store (machine-wide bytes) ────────────────────────────────────


# The pinned tools playwright itself resolves by directory name (see
# store_entry_name). Every consumer that special-cases the browsers keys
# off this one tuple.
PLAYWRIGHT_BROWSER_TOOLS = ("chromium", "chromium-headless-shell")


def store_entry_name(tool: str, version: str, target: str) -> str:
    """The store directory name for one pinned tuple.

    ``<tool>-<version>-<target>`` is exactly what the pin table keys on,
    so two installs agreeing on a pin land on the same name and share the
    bytes, and two disagreeing get one entry each. The name is also what
    makes an entry safe to treat as immutable: same name means the same
    verified artifact, so a publisher that finds one already there can
    keep it instead of rewriting bytes another install is running.

    The playwright browsers are the one exception: playwright resolves
    ``<name with dashes as underscores>-<revision>`` under
    PLAYWRIGHT_BROWSERS_PATH and that spelling is not negotiable, so
    their entries carry playwright's name INSTEAD of ours — the browsers
    path points at the store root and playwright reads the entries
    directly (no links — banned — and no per-install copies). The name
    drops the target key; that costs nothing real, because a store only
    ever holds the host's own target (cross-provisioning is deleted) and
    the entry marker still records the full tuple for verification.
    """
    if tool in PLAYWRIGHT_BROWSER_TOOLS:
        return f"{tool.replace('-', '_')}-{version}"
    return f"{tool}-{version}-{target}"


# ─── lookups (what locators/assemblers consume) ─────────────────────────────


def tool_path(
    name: str,
    runtime_dir: Path | None = None,
    store_dir: Path | None = None,
) -> Optional[Path]:
    """Absolute path to a managed tool's binary, or None when not
    provisioned (or recorded but vanished — treat as unprovisioned; the
    provisioner heals on next update).

    The fact says WHICH entry; the store says WHERE the entries are. That
    split is the whole indirection layer — there are no symlinks.
    """
    facts_dir, store = resolve_bases(runtime_dir, store_dir)
    fact = load_facts(facts_dir).get(name)
    if fact is None:
        return None
    # A system fact's path is absolute; pathlib's `/` already resolves
    # `store / "/usr/bin/git"` to the right side alone, but spell it out
    # rather than lean on an operator subtlety a reader has to know.
    candidate = Path(fact.path) if fact.source == "system" else store / fact.path
    if not candidate.is_file():
        return None
    return candidate


def tool_bin_dir(
    name: str,
    runtime_dir: Path | None = None,
    store_dir: Path | None = None,
) -> Optional[Path]:
    """Directory containing a managed tool's binary — the PATH-assembler
    unit. None when the tool is not provisioned."""
    resolved = tool_path(name, runtime_dir, store_dir)
    return resolved.parent if resolved is not None else None
