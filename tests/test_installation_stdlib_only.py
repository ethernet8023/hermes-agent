"""The installation package must work before anything is installed.

The provisioner installs the tools the rest of Hermes needs — uv, node, npm,
git, gh, ripgrep. It therefore runs at a moment when the venv does not exist
and no dependency is importable. If any module in this package reaches for a
PyPI package, a fresh install deadlocks: you cannot install the installer.

This is checked by RUNNING the package, not by reading it. A parser can tell
you which names appear in an import statement; it cannot tell you whether the
code works when those names resolve to nothing. So every test here drives the
real functions in a subprocess whose interpreter has been stripped:

* ``-I`` (isolated) removes site-packages, PYTHONPATH and the user site dir.
* ``-S`` skips ``site`` entirely, so even a ``.pth`` file cannot smuggle a
  path back in.
* ``sys.path`` is then rebuilt by hand as [repo root] + stdlib only.

That is an empty venv, without the cost of building one. A function that
needs ``requests`` fails here the same way it would on a user's machine.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def run_bare(body: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Execute *body* with no third-party packages reachable.

    The path is rebuilt rather than merely filtered: ``-S`` means ``site``
    never ran, so what remains is the interpreter's own stdlib entries plus
    the repo. Anything the package imports beyond that has to be stdlib.
    """
    program = textwrap.dedent(
        """
        import sys, os
        stdlib = [p for p in sys.path if p and 'site-packages' not in p]
        sys.path = [os.environ['HERMES_REPO_ROOT']] + stdlib
        """
    ) + textwrap.dedent(body)
    full_env = {
        "HERMES_REPO_ROOT": str(REPO_ROOT),
        "PATH": "/usr/bin:/bin",
        "HOME": os.environ.get("HOME", "/tmp"),
    }
    full_env.update(env or {})
    return subprocess.run(
        [sys.executable, "-I", "-S", "-c", program],
        capture_output=True,
        text=True,
        timeout=120,
        env=full_env,
    )



class TestTheWholePackageImportsWithNothingInstalled:
    def test_every_module_imports(self):
        result = run_bare(
            """
            import installation.paths
            import installation.registry
            import installation.provisioner
            import installation.env
            import installation.tree
            print("ok")
            """
        )
        assert result.returncode == 0, result.stderr
        assert "ok" in result.stdout

    def test_no_third_party_module_ends_up_loaded(self):
        """Nothing pulled a dependency in transitively.

        Import succeeding is not enough on its own: a module could import
        something that happens to be vendored or stdlib-shadowed. Compare
        what actually landed in sys.modules against the stdlib list.
        """
        result = run_bare(
            """
            import sys
            before = set(sys.modules)
            import installation.provisioner, installation.env, installation.tree
            new = set(sys.modules) - before
            ours = {"installation", "hermes_constants"}
            foreign = sorted(
                m for m in new
                if m.split(".")[0] not in sys.stdlib_module_names
                and m.split(".")[0] not in ours
                and not m.startswith("_")
            )
            print(repr(foreign))
            """
        )
        assert result.returncode == 0, result.stderr
        foreign = eval(result.stdout.strip().splitlines()[-1])
        assert foreign == [], (
            "importing the installation package loaded non-stdlib modules, so "
            f"it cannot run before the venv exists: {foreign}"
        )


class TestEveryPublicFunctionRunsBare:
    """Call them, do not just import them.

    A lazy ``import requests`` inside a function body is invisible to an
    import-time check and to a reader skimming the header. It shows up the
    moment the function is called — which, for this package, is during a
    fresh install.
    """

    def test_paths_resolve(self, tmp_path):
        result = run_bare(
            """
            from installation.paths import (
                get_install_root, get_runtime_dir, get_tool_store, resolve_bases,
                set_install_root_override, reset_install_root_override,
            )
            root = get_install_root()
            assert root.is_dir(), root
            assert get_runtime_dir().name == ".hermes-runtime"
            token = set_install_root_override("/tmp/probe-root")
            assert str(get_install_root()) == "/tmp/probe-root"
            assert str(get_runtime_dir()).startswith("/tmp/probe-root")
            reset_install_root_override(token)
            assert get_install_root() == root
            print("ok")
            """
        )
        assert result.returncode == 0, result.stderr

    def test_the_tool_store_resolves_with_nothing_installed(self, tmp_path):
        """The store is where the provisioner PUTS the first tool, so it
        has to resolve on a machine that has none of them yet — and
        without importing anything a fresh box does not have."""
        result = run_bare(
            f"""
            from pathlib import Path
            from installation.paths import get_tool_store, resolve_bases

            store = get_tool_store()
            assert store.name == "tools", store
            assert str(store).startswith({str(tmp_path)!r}), store

            # An explicit runtime dir with no store means a self-contained
            # artifact: one directory holds both facts and bytes.
            facts, bytes_ = resolve_bases(Path("/tmp/rt"))
            assert facts == bytes_ == Path("/tmp/rt"), (facts, bytes_)

            # Naming both keeps them apart.
            facts, bytes_ = resolve_bases(Path("/tmp/rt"), Path("/tmp/store"))
            assert (facts, bytes_) == (Path("/tmp/rt"), Path("/tmp/store"))

            # Neither: this install's facts, the machine's store.
            facts, bytes_ = resolve_bases()
            assert bytes_ == store and facts != store, (facts, bytes_)
            print("ok")
            """,
            env={"HOME": str(tmp_path), "HERMES_HOME": str(tmp_path / ".hermes")},
        )
        assert result.returncode == 0, result.stderr

    def test_a_packager_runtime_dir_override_wins_for_both(self, tmp_path):
        """HERMES_RUNTIME_DIR is how Nix and the desktop payload say "this
        one directory is the whole runtime" — it must move the bytes too,
        or a sealed install would look for tools in a store it cannot
        write and does not own."""
        result = run_bare(
            f"""
            from pathlib import Path
            from installation.paths import get_tool_store, get_runtime_dir, resolve_bases

            sealed = Path({str(tmp_path / "sealed")!r})
            assert get_runtime_dir() == sealed
            assert get_tool_store() == sealed
            assert resolve_bases() == (sealed, sealed)
            print("ok")
            """,
            env={
                "HOME": str(tmp_path),
                "HERMES_RUNTIME_DIR": str(tmp_path / "sealed"),
            },
        )
        assert result.returncode == 0, result.stderr

    def test_registry_reads_the_real_pin_table(self):
        result = run_bare(
            """
            from installation.registry import (
                current_target, pins_path, load_pins, install_order, path_order,
                pinned_file, facts_path, load_facts, load_path_order,
                save_facts, record_fact, tool_path, tool_bin_dir, RuntimeFact,
            )
            pins = load_pins()
            assert pins, "pin table is empty"
            assert pins_path().is_file(), pins_path()
            target = current_target()
            order = install_order(pins)
            assert "node" in order and order.index("node") < order.index("npm")
            path_order(pins)
            pin = pinned_file("node", target)
            assert pin.sha256 and pin.url.startswith("https://")
            print("ok")
            """
        )
        assert result.returncode == 0, result.stderr

    def test_facts_round_trip_on_disk(self, tmp_path):
        result = run_bare(
            f"""
            from pathlib import Path
            from installation.registry import (
                RuntimeFact, save_facts, load_facts, facts_path,
                record_fact, tool_path, tool_bin_dir, load_path_order,
            )
            rt = Path({str(tmp_path)!r})
            save_facts({{"node": RuntimeFact(version="26.7.0", path="node/bin/node")}},
                       rt, path_order=["node", "npm"])
            assert facts_path(rt).is_file()
            facts = load_facts(rt)
            assert facts["node"].version == "26.7.0"
            # The order is filtered to tools that are actually recorded:
            # npm was named but never provisioned, so it is not in the file.
            assert load_path_order(rt) == ["node"]
            # record_fact rewrites the table wholesale, so the order it was
            # not given is dropped — assert the fact, not the order, after.
            record_fact("uv", "1.2.3", "uv/uv", rt)
            assert load_facts(rt)["uv"].version == "1.2.3"
            # Recorded but not on disk reads as unprovisioned.
            assert tool_path("uv", rt) is None
            assert tool_bin_dir("uv", rt) is None
            print("ok")
            """
        )
        assert result.returncode == 0, result.stderr

    def test_env_assembly(self, tmp_path):
        result = run_bare(
            f"""
            from pathlib import Path
            from installation.registry import RuntimeFact, save_facts
            from installation.env import (
                runtime_cache_dir, managed_path_dirs, is_macos_xcode_shim,
                managed_tool_binary, managed_tool_env, with_managed_runtimes,
            )
            rt = Path({str(tmp_path)!r})
            (rt / "node" / "bin").mkdir(parents=True)
            (rt / "node" / "bin" / "node").write_text("#!/bin/sh\\n")
            save_facts({{"node": RuntimeFact(version="26.7.0", path="node/bin/node")}}, rt)
            dirs = managed_path_dirs(rt)
            assert any(d.name == "bin" for d in dirs), dirs
            env = with_managed_runtimes({{"PATH": "/usr/bin"}}, runtime_dir=rt)
            assert "node" in env["PATH"]
            managed_tool_env(rt)
            managed_tool_binary("node", rt)
            runtime_cache_dir(rt)
            is_macos_xcode_shim("/usr/bin/git")
            print("ok")
            """
        )
        assert result.returncode == 0, result.stderr

    def test_tree_classification(self, tmp_path):
        result = run_bare(
            f"""
            import json
            from pathlib import Path
            from installation.tree import (
                runtime_tree, GitCheckout, Sealed, read_build_info,
                steward_update_message, steward_uninstall_message,
                install_method,
            )
            from hermes_cli.update_channel import resolve_update_channel
            checkout = Path({str(tmp_path)!r}) / "checkout"
            (checkout / ".git").mkdir(parents=True)
            assert isinstance(runtime_tree(checkout), GitCheckout)

            sealed = Path({str(tmp_path)!r}) / "sealed"
            sealed.mkdir()
            (sealed / "install-stamp.json").write_text(
                json.dumps({{"commit": "a" * 40, "distribution": "docker", "updateMechanism": "external"}})
            )
            tree = runtime_tree(sealed)
            assert isinstance(tree, Sealed), tree
            assert "docker" in steward_update_message(tree.steward).lower()
            steward_uninstall_message(tree.steward)
            read_build_info(sealed)
            install_method(checkout)
            resolve_update_channel(None, checkout)
            print("ok")
            """
        )
        assert result.returncode == 0, result.stderr

    def test_provisioner_decision_paths(self, tmp_path):
        """Everything short of a download: the network is not available here."""
        result = run_bare(
            f"""
            from pathlib import Path
            from installation.provisioner import (
                stale_tools, provision_tool, require_current_runtimes,
                step_provision_runtimes, main, ToolResult, StaleManagedRuntimes,
            )
            rt = Path({str(tmp_path)!r})
            drift = stale_tools(runtime_dir=rt)
            assert "node" in drift, drift          # nothing provisioned yet
            # An unpinned tool fails without touching the network.
            result = provision_tool("not-a-real-tool", runtime_dir=rt)
            assert result.ok is False and "not pinned" in (result.detail or "")
            # A git checkout is allowed to drift; only sealed trees raise.
            checkout = rt / "checkout"
            (checkout / ".git").mkdir(parents=True)
            require_current_runtimes(project_root=checkout, runtime_dir=rt)
            # A sealed tree cannot self-heal, so the same drift raises there.
            sealed = rt / "sealed"
            sealed.mkdir()
            try:
                require_current_runtimes(project_root=sealed, runtime_dir=rt)
            except StaleManagedRuntimes:
                pass
            else:
                raise AssertionError("a sealed tree with drift must raise")
            # --target asserting a foreign host exits 2 before any download.
            assert main(["--runtime-dir", str(rt), "--target", "sunos-vax"]) == 2
            print("ok")
            """
        )
        assert result.returncode == 0, result.stderr


class TestTheGuardActuallyGuards:
    """The check must fail when the property is broken.

    Without this, a bug in ``run_bare`` (a path entry that leaks
    site-packages back in) would leave every test above passing while
    checking nothing.
    """

    def test_a_third_party_import_fails_under_the_bare_interpreter(self):
        result = run_bare("import requests\nprint('ok')")
        assert result.returncode != 0, (
            "the stripped interpreter could still import a third-party "
            "package, so these tests prove nothing"
        )
        assert "ModuleNotFoundError" in result.stderr

    def test_the_repo_itself_is_reachable(self):
        """The opposite failure: a path so stripped nothing imports."""
        result = run_bare("import hermes_constants\nprint('ok')")
        assert result.returncode == 0, result.stderr
