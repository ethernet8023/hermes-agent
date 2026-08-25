"""installation.pip_ladder: one managed-uv install path, no pip tier.

Driven with fake uv binaries so every decision is observable without
network. The stdlib-only contract rides the same run-bare audit as the
rest of the installation package.
"""

from __future__ import annotations

import ast
import stat
import subprocess
from pathlib import Path

import pytest

from installation import pip_ladder
from tests.test_installation_stdlib_only import run_bare


def _fake_bin(path: Path, exit_code: int = 0, marker: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"sys.stderr.write({marker!r})\n"
        f"raise SystemExit({exit_code})\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class TestStdlibOnly:
    def test_imports_bare(self):
        result = run_bare(
            """
            from installation import pip_ladder
            out = pip_ladder.pip_install(())
            assert out.ok and out.tier == "none", out
            print("ok")
            """
        )
        assert result.returncode == 0, result.stderr


class TestUvOnly:
    def test_uv_success(self, tmp_path, monkeypatch):
        uv = _fake_bin(tmp_path / "uv", exit_code=0)
        calls: list[list[str]] = []
        real_run = subprocess.run

        def spy(cmd, **kw):
            calls.append([str(c) for c in cmd])
            return real_run(cmd, **kw)

        monkeypatch.setattr(pip_ladder.subprocess, "run", spy)

        out = pip_ladder.pip_install(["somepkg"], uv_bin=str(uv))

        assert out.ok and out.tier == "uv"
        assert len(calls) == 1
        assert calls[0][:3] == [str(uv), "pip", "install"]

    def test_uv_failure_is_final(self, tmp_path, monkeypatch):
        """uv saw the requirements and said no.

        There is no second opinion: pip would resolve the same
        requirements without uv policy (exclude-newer, the [tool.uv]
        overrides) and can install a quarantined release.
        """
        uv = _fake_bin(tmp_path / "uv", exit_code=1, marker="no solution")
        calls: list[list[str]] = []
        real_run = subprocess.run

        def spy(cmd, **kw):
            calls.append([str(c) for c in cmd])
            return real_run(cmd, **kw)

        monkeypatch.setattr(pip_ladder.subprocess, "run", spy)

        out = pip_ladder.pip_install(["somepkg"], uv_bin=str(uv))

        assert not out.ok and out.tier == "uv"
        assert "no solution" in out.stderr
        assert len(calls) == 1, f"a second install tier ran: {calls}"
        assert not any("pip" in c and "-m" in c for c in calls)

    def test_no_managed_uv_names_the_provisioner(self, monkeypatch):
        """An unprovisioned tree is a provisioning fault, not an install
        that quietly takes a weaker path."""
        monkeypatch.setattr(pip_ladder, "default_uv", lambda: None)
        calls: list[list[str]] = []
        monkeypatch.setattr(
            pip_ladder.subprocess,
            "run",
            lambda cmd, **kw: calls.append([str(c) for c in cmd]),
        )

        out = pip_ladder.pip_install(["somepkg"])

        assert not out.ok and out.tier == "none"
        assert "installation.provisioner" in out.stderr
        assert calls == [], f"an install ran with no managed uv: {calls}"

    def test_vanished_uv_reports_the_missing_binary(self, tmp_path, monkeypatch):
        def spy(cmd, **kw):
            raise FileNotFoundError(cmd[0])

        monkeypatch.setattr(pip_ladder.subprocess, "run", spy)

        out = pip_ladder.pip_install(
            ["somepkg"], uv_bin=str(tmp_path / "uv-gone")
        )

        assert not out.ok and out.tier == "none"
        assert "uv-gone" in out.stderr

    def test_target_constraints_and_overrides_reach_uv(self, tmp_path, monkeypatch):
        uv = _fake_bin(tmp_path / "uv", exit_code=0)
        seen: list[list[str]] = []

        def spy(cmd, **kw):
            seen.append([str(c) for c in cmd])
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(pip_ladder.subprocess, "run", spy)

        pip_ladder.pip_install(
            ["pkg"],
            uv_bin=str(uv),
            target=tmp_path / "overlay",
            constraints=tmp_path / "cons.txt",
            overrides=tmp_path / "over.txt",
        )

        assert len(seen) == 1
        cmd = seen[0]
        assert "--target" in cmd and "--constraint" in cmd and "--overrides" in cmd

    def test_never_raises(self, tmp_path, monkeypatch):
        def explode(cmd, **kw):
            raise OSError("everything is broken")

        monkeypatch.setattr(pip_ladder.subprocess, "run", explode)

        out = pip_ladder.pip_install(["pkg"], uv_bin=str(tmp_path / "uv"))

        assert not out.ok
        assert "broken" in out.stderr or "could not run" in out.stderr


class TestNoBuildAndWheelGap:
    """--no-build, and reading uv's refusal back.

    A user machine is not a build machine, so a runtime install forbids
    source builds. The refusal has to be told apart from every other
    resolution failure, because only this one means "this host cannot
    have the feature" — which lazy_deps re-raises as UnsupportedFeature.
    """

    def test_no_build_is_off_unless_asked(self, tmp_path, monkeypatch):
        # The bundled build lane compiles on purpose (native runner, real
        # toolchains), so the flag must never arrive uninvited.
        uv = _fake_bin(tmp_path / "uv", exit_code=0)
        calls: list[list[str]] = []
        real_run = subprocess.run

        def spy(cmd, **kw):
            calls.append([str(c) for c in cmd])
            return real_run(cmd, **kw)

        monkeypatch.setattr(pip_ladder.subprocess, "run", spy)

        pip_ladder.pip_install(["pkg"], uv_bin=str(uv))
        assert "--no-build" not in calls[0]

        calls.clear()
        pip_ladder.pip_install(["pkg"], uv_bin=str(uv), no_build=True)
        assert "--no-build" in calls[0]

    def test_a_wheel_gap_is_recognised_and_names_the_package(self):
        # uv's real words, verbatim from `uv pip install --no-build` on a
        # package that publishes an sdist only.
        result = pip_ladder.LadderResult(
            False,
            "",
            "  x No solution found when resolving dependencies:\n"
            "  `-> Because alibabacloud-credentials-api==1.0.0 has no usable wheels and\n"
            "      you require alibabacloud-credentials-api==1.0.0, we can conclude that\n"
            "      your requirements are unsatisfiable.\n"
            "\n"
            "hint: Wheels are required for `alibabacloud-credentials-api` because "
            "building from source is disabled for all packages (i.e., with `--no-build`)\n",
            "uv",
        )
        assert pip_ladder.wheel_gap(result) == "alibabacloud-credentials-api"

    def test_other_failures_are_not_mistaken_for_a_wheel_gap(self):
        # Every unsatisfiable resolution shares the "No solution found"
        # header, so matching THAT would turn a typo or an offline run
        # into a permanent "unsupported on this machine" verdict. Only
        # the hint means a wheel is missing.
        no_such_version = pip_ladder.LadderResult(
            False,
            "",
            "  x No solution found when resolving dependencies:\n"
            "  `-> Because there is no version of somepkg==99.0.0 and you require\n"
            "      somepkg==99.0.0, we can conclude that your requirements are\n"
            "      unsatisfiable.\n",
            "uv",
        )
        offline = pip_ladder.LadderResult(
            False,
            "",
            "  x No solution found when resolving dependencies:\n"
            "  `-> Because certifi was not found in the cache and you require\n"
            "      certifi==2026.7.22, we can conclude that your requirements are\n"
            "      unsatisfiable.\n"
            "\n"
            "hint: Packages were unavailable because the network was disabled. When "
            "the network is disabled, registry packages may only be read from the cache.\n",
            "uv",
        )
        assert pip_ladder.wheel_gap(no_such_version) is None
        assert pip_ladder.wheel_gap(offline) is None

    def test_a_successful_install_reports_no_gap(self):
        ok = pip_ladder.LadderResult(True, "Installed 1 package", "", "uv")
        assert pip_ladder.wheel_gap(ok) is None


class TestNoPipTier:
    """The pip tier must not come back.

    It was removed because pip resolves without uv policy: no
    exclude-newer, no [tool.uv] overrides, and no --overrides flag at
    all (which forced a second --no-deps repair pass against a tree pip
    had already changed). A reintroduced pip tier restores every one of
    those faults silently.
    """

    def test_module_spawns_no_pip_and_no_ensurepip(self):
        """No `python -m pip` and no ensurepip spawn in the module.

        `uv pip install` is the uv subcommand, not pip, so the check
        looks for the `-m` module-invocation shape and for ensurepip by
        name.
        """
        source = Path(pip_ladder.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)

        offenders: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name) else None
            )
            if name not in {"run", "Popen", "check_call", "check_output"}:
                continue
            for arg in node.args:
                literals = {
                    n.value
                    for n in ast.walk(arg)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)
                }
                if "ensurepip" in literals or ("-m" in literals and "pip" in literals):
                    offenders.append(f"line {node.lineno}: {ast.unparse(arg)[:80]}")

        assert not offenders, (
            "installation/pip_ladder.py spawns pip or ensurepip again:\n  "
            + "\n  ".join(offenders)
        )

    def test_result_tiers_are_uv_or_none(self):
        source = Path(pip_ladder.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)

        tiers: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "LadderResult" and len(node.args) == 4:
                    last = node.args[3]
                    if isinstance(last, ast.Constant) and isinstance(last.value, str):
                        tiers.add(last.value)

        assert tiers, "no LadderResult constructions found — did the shape change?"
        assert tiers <= {"uv", "none"}, f"a non-uv tier is back: {sorted(tiers)}"


class TestConsumersRideTheLadder:
    """The former copies must actually delegate — a revert to a private
    ladder would silently restore the drift this killed."""

    @pytest.mark.parametrize(
        "module_name,function_name",
        [
            ("hermes_cli.tools_config", "_pip_install"),
            ("tools.lazy_deps", "_venv_pip_install"),
        ],
    )
    def test_the_copy_is_gone(self, module_name, function_name):
        import importlib

        module = importlib.import_module(module_name)
        module_file = module.__file__
        assert module_file is not None
        source = Path(module_file).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == function_name
            ):
                # Judge the CODE, not the docstring — prose is allowed to
                # mention ensurepip when explaining what was removed.
                body = list(node.body)
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                ):
                    body = body[1:]
                code_src = "\n".join(
                    ast.get_source_segment(source, stmt) or "" for stmt in body
                )
                assert "pip_ladder" in code_src, (
                    f"{module_name}.{function_name} no longer delegates to "
                    f"installation.pip_ladder — the copy is back"
                )
                assert "ensurepip" not in code_src, (
                    f"{module_name}.{function_name} grew its own ladder again"
                )
                return
        pytest.fail(f"{module_name}.{function_name} not found")
