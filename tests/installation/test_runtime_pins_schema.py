"""The pins schema and the runtime loader must agree about the pins file.

Two validators read installation/runtime-pins.json: this JSON Schema, which
an editor consults and CI enforces, and registry.load_pins, the hand-rolled
check the provisioner actually runs. When they disagree the schema is worse
than no schema: an editor marks a valid, shipping pin file as broken, and a
maintainer adding a tool trusts the marker.

So the faults live in ONE table and every fault is pushed through BOTH
validators. Writing a case once and checking it twice is the point: a rule
enforced in only one place is a rule that silently stops being enforced.

Each fault names which validators must reject it, because the two are not
identical and the difference is deliberate. The schema validates the
shipping table, where a silently absent target row and a declared gap read
alike and only one of them is a bug. load_pins also reads the small
single-target tables that tests and tools build by hand, so it cannot
demand full coverage. The table below is where that split is written down.

jsonschema is a test-only dependency. installation/ runs during bootstrap,
before anything is installed, so it validates with the standard library
alone.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List

import pytest

from installation import registry

jsonschema = pytest.importorskip("jsonschema")

REPO_ROOT = Path(__file__).resolve().parents[2]
PINS_PATH = REPO_ROOT / "installation" / "runtime-pins.json"
SCHEMA_PATH = REPO_ROOT / "installation" / "runtime-pins.schema.json"

SCHEMA = "schema"
RUNTIME = "runtime"
BOTH = frozenset({SCHEMA, RUNTIME})

Document = Dict[str, Any]


def _schema() -> Document:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _pins_document() -> Document:
    return json.loads(PINS_PATH.read_text(encoding="utf-8"))


def _schema_errors(document: Document) -> List[Any]:
    return list(jsonschema.Draft202012Validator(_schema()).iter_errors(document))


def _load_through_runtime(document: Document, root: Path) -> Dict[str, Any]:
    """Run the provisioner's own validator over *document*."""
    (root / registry.PINS_FILENAME).write_text(
        json.dumps(document), encoding="utf-8"
    )
    return registry.load_pins(root)


# ─── the fault table ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Fault:
    """One way of breaking the pin table, and who has to catch it."""

    name: str
    mutate: Callable[[Document], None]
    rejected_by: FrozenSet[str]
    why: str


def _gap_without_a_reason(doc: Document) -> None:
    doc["tools"]["chromium"]["files"]["win32-arm64"] = {"missing": ""}


def _gap_and_artifact_at_once(doc: Document) -> None:
    doc["tools"]["chromium"]["files"]["win32-arm64"] = {
        "missing": "no upstream build",
        "url": "https://example.invalid/chromium.zip",
        "sha256": "a" * 64,
    }


def _required_tool_declares_a_gap(doc: Document) -> None:
    doc["tools"]["ripgrep"]["files"]["win32-arm64"] = {"missing": "no build"}


def _unnamed_target(doc: Document) -> None:
    del doc["tools"]["ripgrep"]["files"]["win32-arm64"]


def _any_mixed_with_a_target(doc: Document) -> None:
    doc["tools"]["npm"]["files"]["linux-x64"] = {
        "url": "https://example.invalid/npm.tgz",
        "sha256": "b" * 64,
    }


def _plain_http_artifact(doc: Document) -> None:
    doc["tools"]["ripgrep"]["files"]["linux-x64"]["url"] = "http://example.invalid/rg.tgz"


def _malformed_digest(doc: Document) -> None:
    doc["tools"]["ripgrep"]["files"]["linux-x64"]["sha256"] = "not-a-digest"


def _unknown_entry_field(doc: Document) -> None:
    doc["tools"]["ripgrep"]["totallyMadeUp"] = "x"


def _version_range(doc: Document) -> None:
    doc["tools"]["ripgrep"]["version"] = ">=15.2.0"


def _python_pin_on_a_non_uv_tool(doc: Document) -> None:
    doc["tools"]["ripgrep"]["python"] = "3.11.15"


def _extends_an_unpinned_tool(doc: Document) -> None:
    doc["tools"]["ripgrep"]["extends"] = ["nosuchtool"]


FAULTS: List[Fault] = [
    Fault(
        "gap_without_a_reason",
        _gap_without_a_reason,
        BOTH,
        "the reason is the whole difference between 'upstream ships nothing' "
        "and an oversight, and the resolver quotes it when refusing",
    ),
    Fault(
        "gap_and_artifact_at_once",
        _gap_and_artifact_at_once,
        BOTH,
        "the union exists so this state cannot be written down",
    ),
    Fault(
        "required_tool_declares_a_gap",
        _required_tool_declares_a_gap,
        frozenset({RUNTIME}),
        "a required tool with a hole bricks the install on that platform. "
        "The schema does not tie 'missing' to 'optional' because expressing "
        "that dependency in JSON Schema costs more than it returns",
    ),
    Fault(
        "unnamed_target",
        _unnamed_target,
        frozenset({SCHEMA}),
        "an absent row and a declared gap read alike, and only one is a bug. "
        "load_pins cannot demand this: it also reads the single-target "
        "tables that tests and tools build by hand",
    ),
    Fault(
        "any_mixed_with_a_target",
        _any_mixed_with_a_target,
        BOTH,
        "'any' means the bytes do not vary, so a sibling target contradicts it",
    ),
    Fault(
        "plain_http_artifact",
        _plain_http_artifact,
        BOTH,
        "https is a supply-chain control, not a style preference",
    ),
    Fault(
        "malformed_digest",
        _malformed_digest,
        BOTH,
        "the digest is what makes a download verifiable",
    ),
    Fault(
        "unknown_entry_field",
        _unknown_entry_field,
        frozenset({SCHEMA}),
        "a typo'd field name is silent otherwise. load_pins reads the fields "
        "it knows and ignores the rest, which keeps it forward compatible",
    ),
    Fault(
        "version_range",
        _version_range,
        frozenset({SCHEMA}),
        "a range makes two builds of one commit disagree. load_pins checks "
        "that a version is present, and the shape check is the schema's",
    ),
    Fault(
        "python_pin_on_a_non_uv_tool",
        _python_pin_on_a_non_uv_tool,
        frozenset({RUNTIME}),
        "uv IS the installer the interpreter pin configures. Tying one "
        "field to one tool name is a rule about content, not shape",
    ),
    Fault(
        "extends_an_unpinned_tool",
        _extends_an_unpinned_tool,
        frozenset({RUNTIME}),
        "a dangling edge drops out of both derived orders in silence. The "
        "schema cannot see whether a named tool exists elsewhere in the table",
    ),
]

_SCHEMA_FAULTS = [f for f in FAULTS if SCHEMA in f.rejected_by]
_RUNTIME_FAULTS = [f for f in FAULTS if RUNTIME in f.rejected_by]


# ─── mutations that must stay ACCEPTED ──────────────────────────────────────


def _optional_flag(doc: Document) -> None:
    doc["tools"]["ripgrep"]["optional"] = True


def _off_path_flag(doc: Document) -> None:
    doc["tools"]["ripgrep"]["onPath"] = False


def _uv_python_pin(doc: Document) -> None:
    doc["tools"]["uv"]["python"] = "3.12.7"


def _revision_style_version(doc: Document) -> None:
    doc["tools"]["ripgrep"]["version"] = "1208"


ACCEPTED: List[Fault] = [
    Fault("optional_flag", _optional_flag, BOTH, "on-demand capability tools"),
    Fault("off_path_flag", _off_path_flag, BOTH, "a browser tree is not a CLI"),
    Fault("uv_python_pin", _uv_python_pin, BOTH, "the interpreter pin rides uv"),
    Fault(
        "revision_style_version",
        _revision_style_version,
        BOTH,
        "playwright numbers chromium builds rather than releasing versions",
    ),
]


# ─── the shipping table ─────────────────────────────────────────────────────


def test_schema_is_itself_valid() -> None:
    """A schema with a malformed keyword silently validates nothing."""
    jsonschema.Draft202012Validator.check_schema(_schema())


def test_both_validators_accept_the_shipping_pins() -> None:
    """The table Hermes ships passes each validator that guards it."""
    errors = _schema_errors(_pins_document())

    assert not errors, "the shipping pin table does not match its schema:\n" + "\n".join(
        f"  {'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
        for e in errors
    )
    assert registry.load_pins(), "the shipping pin table fails its runtime loader"


def test_schema_version_matches_the_runtime_constant() -> None:
    """One number, asserted as a relationship rather than as a literal."""
    schema_const = _schema()["properties"]["schemaVersion"]["const"]

    assert schema_const == registry.PINS_SCHEMA_VERSION
    assert _pins_document()["schemaVersion"] == registry.PINS_SCHEMA_VERSION


# ─── the table, run through each validator ──────────────────────────────────


@pytest.mark.parametrize("fault", _SCHEMA_FAULTS, ids=lambda f: f.name)
def test_schema_rejects_the_fault(fault: Fault) -> None:
    document = _pins_document()
    fault.mutate(document)

    assert _schema_errors(document), f"schema accepted {fault.name}: {fault.why}"


@pytest.mark.parametrize("fault", _RUNTIME_FAULTS, ids=lambda f: f.name)
def test_runtime_loader_rejects_the_fault(fault: Fault, tmp_path: Path) -> None:
    document = _pins_document()
    fault.mutate(document)

    with pytest.raises(ValueError):
        _load_through_runtime(document, tmp_path)


@pytest.mark.parametrize("accepted", ACCEPTED, ids=lambda f: f.name)
def test_both_validators_accept_the_real_fields(
    accepted: Fault, tmp_path: Path
) -> None:
    """A field the runtime honors but the schema rejects is the disagreement."""
    document = _pins_document()
    accepted.mutate(document)

    assert not _schema_errors(document), (
        f"schema rejected {accepted.name}: {accepted.why}"
    )
    assert _load_through_runtime(document, tmp_path), (
        f"runtime loader rejected {accepted.name}: {accepted.why}"
    )


def test_every_fault_is_somebody_s_job() -> None:
    """A row that no validator checks is a rule nobody enforces."""
    unguarded = [f.name for f in FAULTS if not f.rejected_by]

    assert not unguarded, f"faults with no validator: {unguarded}"
