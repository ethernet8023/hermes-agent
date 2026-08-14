"""Tests for installation.registry — exact pins, targets, facts.

Pure logic: no network, no real install. The pin table is EXACT by
design (no ranges, no "resolve latest"), so these assert the shape of
that contract and the eager validation that enforces it.
"""

import json

import pytest

from installation import registry as rr


def _pins(tools, schema=rr.PINS_SCHEMA_VERSION):
    return {"schemaVersion": schema, "tools": tools}


def _entry(version="1.2.3", targets=("linux-x64",), sha=None):
    return {
        "version": version,
        "files": {
            t: {
                "url": f"https://example.invalid/{t}/tool-{version}.tar.gz",
                "sha256": sha or ("a" * 64),
            }
            for t in targets
        },
    }


@pytest.fixture
def pin_root(tmp_path):
    """Write a pin table and return the root it lives in."""

    def _write(tools, schema=rr.PINS_SCHEMA_VERSION):
        (tmp_path / rr.PINS_FILENAME).write_text(
            json.dumps(_pins(tools, schema)), encoding="utf-8"
        )
        return tmp_path

    return _write


class TestCurrentTarget:
    def test_reports_a_pin_table_key_for_this_host(self):
        target = rr.current_target()

        platform, _, arch = target.partition("-")
        assert platform in ("darwin", "linux", "win32")
        assert arch in ("arm64", "x64")

    def test_the_host_target_is_pinned_for_every_tool(self):
        """A platform Hermes runs on must have a download for every tool,
        or provisioning silently degrades to system PATH there."""
        target = rr.current_target()

        for tool, entry in rr.load_pins().items():
            assert rr.pinned_file(tool, target).url, f"{tool} has no {target} download"


class TestPinValidation:
    def test_loads_a_well_formed_table(self, pin_root):
        pins = rr.load_pins(pin_root({"node": _entry("26.7.0")}))

        assert pins["node"]["version"] == "26.7.0"

    def test_rejects_a_foreign_schema_version(self, pin_root):
        with pytest.raises(ValueError, match="schemaVersion"):
            rr.load_pins(pin_root({"node": _entry()}, schema=99))

    def test_rejects_a_missing_version(self, pin_root):
        entry = _entry()
        del entry["version"]

        with pytest.raises(ValueError, match="no exact version"):
            rr.load_pins(pin_root({"node": entry}))

    def test_rejects_a_tool_with_no_files(self, pin_root):
        with pytest.raises(ValueError, match="no 'files' table"):
            rr.load_pins(pin_root({"node": {"version": "1.0.0", "files": {}}}))

    def test_rejects_a_non_https_url(self, pin_root):
        entry = _entry()
        entry["files"]["linux-x64"]["url"] = "http://example.invalid/x.tar.gz"

        with pytest.raises(ValueError, match="https url"):
            rr.load_pins(pin_root({"node": entry}))

    def test_rejects_a_malformed_digest(self, pin_root):
        """A truncated sha256 must fail at LOAD, not halfway through a
        user's first launch."""
        with pytest.raises(ValueError, match="64 hex chars"):
            rr.load_pins(pin_root({"node": _entry(sha="abc123")}))

    def test_rejects_an_extends_edge_to_an_unpinned_tool(self, pin_root):
        entry = _entry()
        entry["extends"] = ["ghost"]

        with pytest.raises(ValueError, match="which is not pinned"):
            rr.load_pins(pin_root({"npm": entry}))

    def test_rejects_a_self_extending_tool(self, pin_root):
        entry = _entry()
        entry["extends"] = ["npm"]

        with pytest.raises(ValueError, match="extends itself"):
            rr.load_pins(pin_root({"npm": entry}))

    def test_rejects_an_extends_cycle_at_load(self, pin_root):
        """Both derived orders must terminate; a cycle is caught when the
        table is read, not when a user's first launch hangs."""
        a, b = _entry(), _entry()
        a["extends"] = ["b"]
        b["extends"] = ["a"]

        with pytest.raises(ValueError, match="cycle"):
            rr.load_pins(pin_root({"a": a, "b": b}))

    def test_rejects_mixing_any_with_per_target_files(self, pin_root):
        """'any' claims one artifact serves every target. A table that
        also names targets is stating two different things at once."""
        entry = _entry(targets=("linux-x64",))
        entry["files"]["any"] = {"url": "https://example.invalid/x.tgz",
                                 "sha256": "b" * 64}

        with pytest.raises(ValueError, match="mixes 'any'"):
            rr.load_pins(pin_root({"npm": entry}))


class TestDerivedOrder:
    """Both orders come from one `extends` declaration, so they cannot
    contradict each other the way two hand-kept lists could."""

    def test_a_tool_installs_after_what_it_extends(self):
        pins = {"node": {}, "npm": {"extends": ["node"]}}

        assert rr.install_order(pins) == ["node", "npm"]

    def test_a_tool_is_found_before_what_it_extends(self):
        """The whole point: npm exists to supersede the npm inside node."""
        pins = {"node": {}, "npm": {"extends": ["node"]}}

        assert rr.path_order(pins) == ["npm", "node"]

    def test_a_declaration_written_out_of_order_still_resolves(self):
        pins = {"npm": {"extends": ["node"]}, "node": {}}

        assert rr.install_order(pins) == ["node", "npm"]
        assert rr.path_order(pins) == ["npm", "node"]

    def test_transitive_edges_chain(self):
        pins = {"a": {}, "b": {"extends": ["a"]}, "c": {"extends": ["b"]}}

        assert rr.install_order(pins) == ["a", "b", "c"]
        assert rr.path_order(pins) == ["c", "b", "a"]

    def test_unrelated_tools_keep_the_pin_table_order(self):
        """Between two tools with no edge the order is arbitrary, and
        churning it would rewrite every PATH for no reason."""
        pins = {"uv": {}, "git": {}, "gh": {}, "ripgrep": {}}

        assert rr.install_order(pins) == ["uv", "git", "gh", "ripgrep"]
        assert rr.path_order(pins) == ["uv", "git", "gh", "ripgrep"]

    def test_both_orders_contain_every_tool_exactly_once(self):
        pins = {"node": {}, "npm": {"extends": ["node"]}, "uv": {}, "gh": {}}

        for order in (rr.install_order(pins), rr.path_order(pins)):
            assert sorted(order) == sorted(pins)


class TestPinnedFile:
    def test_resolves_url_version_and_digest_for_a_target(self, pin_root):
        root = pin_root({"gh": _entry("2.97.0", ("linux-x64", "darwin-arm64"))})

        pin = rr.pinned_file("gh", "darwin-arm64", install_root=root)

        assert pin.version == "2.97.0"
        assert pin.url.endswith("darwin-arm64/tool-2.97.0.tar.gz")
        assert pin.sha256 == "a" * 64

    def test_filename_comes_from_the_url(self, pin_root):
        root = pin_root({"gh": _entry("2.97.0")})

        assert rr.pinned_file("gh", "linux-x64", install_root=root).filename == (
            "tool-2.97.0.tar.gz"
        )

    def test_unknown_tool_raises(self, pin_root):
        with pytest.raises(KeyError, match="not in the pin table"):
            rr.pinned_file("nope", "linux-x64", install_root=pin_root({"gh": _entry()}))

    def test_unpinned_target_raises_rather_than_guessing(self, pin_root):
        """An unpinned platform is a gap in the table to fill, not a URL
        to construct hopefully."""
        root = pin_root({"gh": _entry(targets=("linux-x64",))})

        with pytest.raises(KeyError, match="no pinned download for darwin-arm64"):
            rr.pinned_file("gh", "darwin-arm64", install_root=root)

    def test_an_any_artifact_resolves_for_every_target(self, pin_root):
        """A registry tarball's bytes do not vary by platform, so one
        pinned artifact serves all six targets rather than six identical
        rows nobody can keep honest."""
        root = pin_root({"npm": _entry("12.0.2", ("any",))})

        for target in ("linux-x64", "darwin-arm64", "win32-arm64"):
            pin = rr.pinned_file("npm", target, install_root=root)
            assert pin.version == "12.0.2"
            assert pin.sha256 == "a" * 64


class TestRecordedPathOrder:
    """The facts file carries the derived order so both language readers
    consume one answer instead of each restating a literal."""

    def test_save_records_the_order_it_was_given(self, tmp_path):
        facts = {
            "node": rr.RuntimeFact(version="26.7.0", path="node/bin/node"),
            "npm": rr.RuntimeFact(version="12.0.2", path="npm/bin/npm"),
        }
        rr.save_facts(facts, tmp_path, path_order=["npm", "node"])

        assert rr.load_path_order(tmp_path) == ["npm", "node"]

    def test_an_unprovisioned_tool_is_dropped_from_the_recorded_order(self, tmp_path):
        """The order names what to look for; a tool that failed to
        provision has nothing to find."""
        facts = {"node": rr.RuntimeFact(version="26.7.0", path="node/bin/node")}
        rr.save_facts(facts, tmp_path, path_order=["npm", "node"])

        assert rr.load_path_order(tmp_path) == ["node"]

    def test_a_later_single_fact_update_keeps_the_recorded_order(self, tmp_path):
        """record_fact has no pin table in hand; it must not silently
        reset the order to insertion order."""
        rr.save_facts(
            {
                "node": rr.RuntimeFact(version="26.7.0", path="node/bin/node"),
                "npm": rr.RuntimeFact(version="12.0.2", path="npm/bin/npm"),
            },
            tmp_path,
            path_order=["npm", "node"],
        )

        rr.record_fact("node", "26.8.0", "node/bin/node", tmp_path)

        assert rr.load_path_order(tmp_path) == ["npm", "node"]

    def test_no_facts_file_means_no_order(self, tmp_path):
        assert rr.load_path_order(tmp_path) == []


class TestRealPinTable:
    """The shipped table, as a contract rather than a snapshot."""

    def test_every_tool_resolves_or_declares_its_gap_on_every_target(self):
        expected = {
            "darwin-arm64",
            "darwin-x64",
            "linux-x64",
            "linux-arm64",
            "win32-x64",
            "win32-arm64",
        }

        for tool, entry in rr.load_pins().items():
            declared_missing = entry.get("missingTargets", {})
            for target in expected:
                if target in declared_missing:
                    # The gap is allowed ONLY as an explicit, reasoned
                    # declaration — and the resolver must surface it.
                    with pytest.raises(KeyError, match="has no build for"):
                        rr.pinned_file(tool, target)
                    continue
                # Either a per-target row or a target-independent 'any'
                # artifact — what matters is that nothing is unreachable.
                assert rr.pinned_file(tool, target).url, f"{tool}/{target}"

    def test_every_download_is_https_with_a_full_digest(self):
        for tool, entry in rr.load_pins().items():
            for target, spec in entry["files"].items():
                assert spec["url"].startswith("https://"), f"{tool}/{target}"
                assert len(spec["sha256"]) == 64, f"{tool}/{target}"
                int(spec["sha256"], 16)  # raises unless it is hex

    def test_no_version_ranges_survive_anywhere(self):
        """Exact pins only: a range would make two builds of one commit
        disagree and need a GitHub API call to resolve."""
        for tool, entry in rr.load_pins().items():
            version = entry["version"]
            assert not version.endswith(".x"), tool
            assert not version.startswith(">="), tool

    def test_digests_and_urls_agree_in_both_directions(self):
        """Copy-paste is the likely failure when hand-editing 30 digests.

        The bug that matters is a digest pasted onto the WRONG url: that
        target then downloads a file whose bytes cannot match, and fails
        verification. So the rule is a bijection rather than plain
        uniqueness -- one url has one digest, and one digest belongs to
        one url.

        Two targets legitimately share a row when upstream ships no build
        for one of them: camoufox has no Windows arm64 artifact, so
        win32-arm64 points at the x86_64 zip and runs it emulated. Same
        url, same digest, deliberately -- the aliasing is visible in the
        url, which is exactly what a copy-paste error is not.
        """
        for tool, entry in rr.load_pins().items():
            by_url: dict[str, str] = {}
            by_digest: dict[str, str] = {}
            for target, spec in entry["files"].items():
                url, digest = spec["url"], spec["sha256"]
                if url in by_url:
                    assert by_url[url] == digest, (
                        f"{tool}/{target}: same url, two digests -- "
                        f"one of them cannot verify"
                    )
                if digest in by_digest:
                    assert by_digest[digest] == url, (
                        f"{tool}/{target}: digest is reused across two urls "
                        f"({by_digest[digest]} and {url}) -- a pasted digest"
                    )
                by_url[url] = digest
                by_digest[digest] = url

    def test_every_extends_edge_names_a_pinned_tool(self):
        """A dangling edge would silently drop out of both derived
        orders instead of failing."""
        pins = rr.load_pins()

        for tool, entry in pins.items():
            for dep in entry.get("extends", []):
                assert dep in pins, f"{tool} extends unpinned {dep}"

    def test_npm_is_ordered_around_the_node_it_extends(self):
        """npm ships INSIDE node, so both derived orders have to place
        it deliberately: installed after node (node unpacks it), found
        before node (or node's bundled npm shadows it)."""
        pins = rr.load_pins()
        install = rr.install_order(pins)
        path = rr.path_order(pins)

        assert install.index("npm") > install.index("node")
        assert path.index("npm") < path.index("node")

    def test_git_ships_the_same_version_from_both_suppliers(self):
        """dugite-native (POSIX) and PortableGit (Windows) are different
        builds of the same git. Letting them drift apart would make git
        behaviour depend on the user's platform."""
        git = rr.load_pins()["git"]

        assert "dugite-native" in git["files"]["darwin-arm64"]["url"]
        assert "PortableGit" in git["files"]["win32-x64"]["url"]
        # One version field covers both — the table cannot express a skew.
        assert git["version"] == "2.53.0"

    def test_windows_git_is_portablegit_not_mingit(self):
        """MinGit omits bash.exe, which the desktop needs
        (find-git-bash.ts). dugite's own windows build omits it too."""
        for target in ("win32-x64", "win32-arm64"):
            url = rr.load_pins()["git"]["files"][target]["url"]
            assert "PortableGit" in url
            assert "MinGit" not in url


class TestPythonPin:
    """Decision 3: the interpreter pin rides the uv entry."""

    def test_the_real_table_pins_an_exact_python(self):
        python = rr.pinned_python()
        assert python is not None
        assert len(python.split(".")) == 3

    def test_the_pin_satisfies_requires_python(self):
        """Engines-style lint, symmetric with the node check: a python
        pin outside pyproject's requires-python window would install an
        interpreter the project itself refuses to run on."""
        import re
        import tomllib
        from pathlib import Path

        pyproject = tomllib.loads(
            (Path(rr.__file__).resolve().parents[1] / "pyproject.toml").read_text()
        )
        spec = pyproject["project"]["requires-python"]
        python = rr.pinned_python()
        assert python is not None
        pinned = tuple(int(p) for p in python.split("."))

        for clause in (c.strip() for c in spec.split(",")):
            m = re.fullmatch(r"(>=|<=|<|>|==)\s*(\d+(?:\.\d+)*)", clause)
            assert m, f"unhandled requires-python clause {clause!r}"
            op, bound_s = m.groups()
            bound = tuple(int(p) for p in bound_s.split("."))
            key = pinned[: len(bound)]
            satisfied = {
                ">=": key >= bound,
                "<=": key <= bound,
                "<": key < bound,
                ">": key > bound,
                "==": key == bound,
            }[op]
            assert satisfied, (
                f"python pin {python} violates requires-python clause "
                f"{clause!r} — the pinned interpreter could not run the code"
            )

    def test_only_uv_may_carry_the_python_pin(self, tmp_path):
        bad = {
            "schemaVersion": rr.PINS_SCHEMA_VERSION,
            "tools": {
                "node": {
                    "version": "1.0.0",
                    "python": "3.11.15",
                    "files": {"any": {
                        "url": "https://example.com/x.tar.gz",
                        "sha256": "0" * 64,
                    }},
                },
            },
        }
        (tmp_path / rr.PINS_FILENAME).write_text(json.dumps(bad))

        with pytest.raises(ValueError, match="only 'uv' may"):
            rr.load_pins(tmp_path)

    def test_a_range_python_pin_is_rejected(self, tmp_path):
        """Strict exact X.Y.Z: a range would reintroduce the drift the
        pin exists to end (two installs resolving 3.11 to different
        patch releases)."""
        bad = {
            "schemaVersion": rr.PINS_SCHEMA_VERSION,
            "tools": {
                "uv": {
                    "version": "0.12.3",
                    "python": "3.11",
                    "files": {"any": {
                        "url": "https://example.com/x.tar.gz",
                        "sha256": "0" * 64,
                    }},
                },
            },
        }
        (tmp_path / rr.PINS_FILENAME).write_text(json.dumps(bad))

        with pytest.raises(ValueError, match="exact X.Y.Z"):
            rr.load_pins(tmp_path)
