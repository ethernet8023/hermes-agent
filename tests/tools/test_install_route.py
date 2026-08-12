"""Tests for the shared pip-install routing decision.

``tools.lazy_deps.resolve_install_route`` is the single place that answers
"where may this install write?" for every install ladder in the tree: the
lazy-install pipeline (``ensure`` / ``install_specs``) and the post-setup
hook ladder (``hermes_cli.tools_config._pip_install``). Before it existed
the two ladders disagreed — the post-setup one had no writability probe and
no overflow-target awareness at all, so on a sealed dep store it attempted a
write that could only fail.

The shapes under test, all of which ship:

* writable dep store — source checkout / managed install
* sealed dep store WITH an overflow dir — the immutable Docker image
* sealed dep store WITHOUT one — a Nix build, any read-only distribution
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tools import lazy_deps as ld


@pytest.fixture
def sealed_store(monkeypatch):
    """The opposite shape: a read-only dep store (Nix, any sealed install).

    Paired with ``writable_store`` from ``tests/tools/conftest.py``.
    """
    monkeypatch.setattr(ld, "_site_packages_writable", lambda: False)


@pytest.fixture
def steward(monkeypatch):
    """Pin the detected steward so remediation text is deterministic.

    The real lookup reads the build stamp of the running tree, which in the
    test suite is a git checkout (never sealed).
    """

    def _set(name: str):
        from hermes_cli import runtime_tree as rt

        monkeypatch.setattr(
            rt, "runtime_tree", lambda root: rt.Sealed(root=Path(root), steward=name)
        )

    return _set


class TestResolveInstallRoute:
    def test_writable_store_installs_in_place(self, monkeypatch, writable_store):
        monkeypatch.delenv(ld._LAZY_TARGET_ENV, raising=False)
        route = ld.resolve_install_route()
        assert route.target is None
        assert route.blocked_reason is None

    def test_sealed_store_without_overflow_is_blocked(
        self, monkeypatch, sealed_store, steward
    ):
        steward("nix")
        monkeypatch.delenv(ld._LAZY_TARGET_ENV, raising=False)
        route = ld.resolve_install_route()
        assert route.target is None
        assert route.blocked_reason is not None
        # Remediation must name the declarative mechanism, never pip: a
        # store write is rejected, and would be discarded by the next
        # rebuild even if it were not.
        assert "extraDependencyGroups" in route.blocked_reason
        assert "pip install" not in route.blocked_reason

    def test_sealed_store_with_overflow_redirects(
        self, monkeypatch, tmp_path, sealed_store
    ):
        target = tmp_path / "lazy-packages"
        monkeypatch.setenv(ld._LAZY_TARGET_ENV, str(target))
        route = ld.resolve_install_route()
        assert route.blocked_reason is None
        assert route.target == target
        # Resolution also readies the dir, so callers never race on it.
        assert (target / ld._TARGET_STAMP_NAME).exists()

    def test_overflow_wins_over_writability(self, monkeypatch, tmp_path, writable_store):
        """A configured overflow dir is honoured even on a writable store.

        The deployment, not the probe, decides: Docker sets the target to
        keep installs off the image layer even though the layer would accept
        the write until the container is recreated.
        """
        target = tmp_path / "lazy-packages"
        monkeypatch.setenv(ld._LAZY_TARGET_ENV, str(target))
        assert ld.resolve_install_route().target == target

    def test_unusable_overflow_dir_is_blocked_not_silently_ignored(
        self, monkeypatch, tmp_path
    ):
        # A configured-but-broken target must not fall back to writing into
        # the sealed store; that is the failure mode the redirect prevents.
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("", encoding="utf-8")
        monkeypatch.setenv(ld._LAZY_TARGET_ENV, str(blocker))
        route = ld.resolve_install_route()
        assert route.target is None
        assert route.blocked_reason


class TestStewardRemediation:
    """Each steward gets its own real next step — never "run pip"."""

    def test_nix_points_at_the_declarative_options(self, sealed_store, steward):
        steward("nix")
        reason = ld._sealed_store_remediation("platform.matrix")
        assert "extraDependencyGroups" in reason
        assert "extraPythonPackages" in reason
        assert "platform.matrix" in reason
        assert "pip" not in reason

    def test_docker_points_at_the_writable_volume(self, sealed_store, steward):
        steward("docker")
        reason = ld._sealed_store_remediation("platform.matrix")
        assert "HERMES_LAZY_INSTALL_TARGET" in reason
        assert "pip" not in reason

    def test_desktop_points_at_the_app(self, sealed_store, steward):
        steward("desktop-app")
        reason = ld._sealed_store_remediation("platform.matrix")
        assert "desktop app" in reason
        assert "pip" not in reason

    def test_unknown_steward_still_avoids_pip_advice(self, sealed_store, steward):
        steward("unknown")
        reason = ld._sealed_store_remediation()
        assert "Rebuild the environment" in reason
        assert "pip" not in reason

    def test_every_remediation_key_is_a_real_steward(self):
        """The table is keyed by steward values ``runtime_tree`` can produce.

        ``lazy_deps`` stays free of CLI imports, so it restates the steward
        names as literals; this asserts the two modules agree instead of
        letting a rename silently route every sealed install to the generic
        fallback.
        """
        from hermes_cli import runtime_tree as rt

        known = {rt.STEWARD_NIX, rt.STEWARD_DOCKER, rt.STEWARD_DESKTOP}
        assert set(ld._SEALED_STORE_REMEDIATION) <= known

    def test_every_remediation_renders_its_placeholder(self):
        """Templates must format cleanly and leave no placeholder behind.

        Literal braces are legitimate here — the Nix text quotes a
        ``pkgs.hermes-agent.override { ... }`` snippet — so this asserts the
        named field is gone rather than banning ``{``, which would forbid
        showing the user real Nix syntax.
        """
        templates = [*ld._SEALED_STORE_REMEDIATION.values(), ld._SEALED_STORE_FALLBACK]
        for template in templates:
            rendered = template.format(what="the dependencies for 'x'")
            assert "{what}" not in rendered
            assert "pip" not in rendered
        # At least the templates that reference {what} must interpolate it.
        with_field = [t for t in templates if "{what}" in t]
        assert with_field, "no template names the missing dependencies"
        for template in with_field:
            assert "the dependencies for 'x'" in template.format(
                what="the dependencies for 'x'"
            )

    def test_nix_covers_both_flake_and_nixos_users(self):
        """Not everyone on Nix uses the NixOS module.

        A flake/home-manager user has no ``services.hermes-agent.*`` options,
        so naming only those leaves them with no next step.
        """
        rendered = ld._SEALED_STORE_REMEDIATION["nix"].format(what="these deps")
        assert "services.hermes-agent.extraDependencyGroups" in rendered
        assert "pkgs.hermes-agent.override" in rendered


class TestStewardDetection:
    """``runtime_tree`` must resolve the steward on every sealed shape.

    Nix is the one that needs help: the derivation output and the venv are
    separate store paths, so the stamp is NOT beside the code and only
    ``HERMES_BUILD_INFO`` (exported by the wrapper) can find it. Without
    that lookup a Nix install reports steward "unknown" and gets generic
    remediation instead of its actual config options.
    """

    def _sealed_venv_and_stamp(self, tmp_path, distribution):
        code_root = tmp_path / "hermes-agent-env/lib/python3.12/site-packages"
        code_root.mkdir(parents=True)
        stamp = tmp_path / "hermes-agent-out/share/hermes-agent/install-stamp.json"
        stamp.parent.mkdir(parents=True)
        stamp.write_text(
            json.dumps({"schemaVersion": 2, "commit": "abc",
                        "source": distribution, "distribution": distribution}),
            encoding="utf-8",
        )
        return code_root, stamp

    def test_stamp_found_via_env_when_not_beside_the_code(
        self, monkeypatch, tmp_path
    ):
        from hermes_cli.runtime_tree import Sealed, runtime_tree

        code_root, stamp = self._sealed_venv_and_stamp(tmp_path, "nix")
        monkeypatch.setenv("HERMES_BUILD_INFO", str(stamp))

        tree = runtime_tree(code_root)
        assert isinstance(tree, Sealed)
        assert tree.steward == "nix"

    def test_without_the_env_lookup_the_steward_is_unknown(
        self, monkeypatch, tmp_path
    ):
        # Documents WHY the env lookup exists: the stamp is simply not
        # reachable from the code root on this layout.
        from hermes_cli.runtime_tree import Sealed, runtime_tree

        code_root, _ = self._sealed_venv_and_stamp(tmp_path, "nix")
        monkeypatch.delenv("HERMES_BUILD_INFO", raising=False)

        tree = runtime_tree(code_root)
        assert isinstance(tree, Sealed)
        assert tree.steward == "unknown"

    def test_stamp_beside_the_code_still_wins_without_the_env(
        self, monkeypatch, tmp_path
    ):
        # Docker's layout: the stamp sits at the code root.
        from hermes_cli.runtime_tree import Sealed, runtime_tree

        monkeypatch.delenv("HERMES_BUILD_INFO", raising=False)
        root = tmp_path / "opt-hermes"
        root.mkdir()
        (root / "install-stamp.json").write_text(
            json.dumps({"schemaVersion": 2, "commit": "abc",
                        "source": "docker", "distribution": "docker"}),
            encoding="utf-8",
        )
        tree = runtime_tree(root)
        assert isinstance(tree, Sealed)
        assert tree.steward == "docker"


class TestNoPipAdviceWhenPipCannotWork:
    """The rendered exception, not just the reason string.

    ``FeatureUnavailable.__str__`` appends "To enable manually: uv pip
    install …". On a sealed store that tail contradicts the sentence above
    it and sends the user at a command that cannot succeed, so it must be
    suppressed — this asserts the final text the user actually sees.
    """

    def _raise_ensure(self, monkeypatch, specs=("mautrix[encryption]==0.21.0",)):
        monkeypatch.setattr(ld, "feature_missing", lambda f: specs)
        try:
            ld.ensure("platform.matrix", prompt=False)
        except ld.FeatureUnavailable as exc:
            return str(exc)
        raise AssertionError("ensure() did not raise")

    def test_sealed_store_message_has_no_pip_line(
        self, monkeypatch, sealed_store, steward
    ):
        steward("nix")
        monkeypatch.delenv(ld._LAZY_TARGET_ENV, raising=False)
        message = self._raise_ensure(monkeypatch)
        assert "pip install" not in message
        assert "extraDependencyGroups" in message
        # No ".." seam from concatenating an already-terminated reason.
        assert ".." not in message

    def test_platform_impossible_message_has_no_pip_line(self, monkeypatch):
        # Matrix on Windows: python-olm has no wheel and needs make+libolm.
        monkeypatch.setattr(
            ld, "_unsupported_feature_reason",
            lambda f: "unsupported on Windows: … Run Hermes under WSL.",
        )
        message = self._raise_ensure(monkeypatch)
        assert "pip install" not in message
        assert "WSL" in message

    def test_writable_install_keeps_the_pip_line(self, monkeypatch, writable_store):
        # Where a manual install genuinely works, keep telling the user how.
        monkeypatch.delenv(ld._LAZY_TARGET_ENV, raising=False)
        monkeypatch.setattr(ld, "_allow_lazy_installs", lambda: False)
        message = self._raise_ensure(monkeypatch, ("honcho-ai==2.2.0",))
        assert "uv pip install 'honcho-ai==2.2.0'" in message


class TestLadderAgreement:
    """Both install ladders must reach the same verdict on the same host."""

    def _pip_install(self):
        from hermes_cli.tools_config import _pip_install

        return _pip_install

    def test_post_setup_ladder_refuses_sealed_store(
        self, monkeypatch, sealed_store, steward
    ):
        steward("nix")
        monkeypatch.delenv(ld._LAZY_TARGET_ENV, raising=False)
        monkeypatch.setattr(
            ld.subprocess, "run",
            lambda *a, **k: pytest.fail("no install may be attempted"),
        )
        result = self._pip_install()(["--quiet", "somepkg"], timeout=5)
        assert result.returncode == 1
        assert "extraDependencyGroups" in result.stderr

    def test_lazy_ladder_refuses_sealed_store(
        self, monkeypatch, sealed_store, steward
    ):
        steward("nix")
        monkeypatch.delenv(ld._LAZY_TARGET_ENV, raising=False)
        monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
        monkeypatch.setattr(
            "hermes_cli.config.load_config", lambda: {}, raising=False
        )
        result = ld.install_specs(["somepkg==1.0"])
        # Blocked, not failed: nothing ran, and the fix is a rebuild.
        assert result.ok is False
        assert result.blocked is True
        assert "extraDependencyGroups" in result.reason

    def test_post_setup_ladder_redirects_to_overflow(
        self, monkeypatch, tmp_path, sealed_store
    ):
        target = tmp_path / "lazy-packages"
        monkeypatch.setenv(ld._LAZY_TARGET_ENV, str(target))
        seen = []

        def fake_run(cmd, **kwargs):
            seen.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("hermes_cli.tools_config.subprocess.run", fake_run)
        monkeypatch.setattr("hermes_cli.managed_uv.ensure_uv", lambda: "/fake/uv")

        self._pip_install()(["-U", "faster-whisper", "--quiet"])

        cmd = seen[0]
        assert "--target" in cmd and str(target) in cmd
        # Routing flags must precede the caller's arguments: a trailing
        # "--target" after a positional spec changes pip's parse.
        assert cmd[cmd.index("--target") - 1] == "install"

    def test_caller_supplied_target_is_not_overridden(
        self, monkeypatch, tmp_path, sealed_store
    ):
        """``agent/lsp/install.py`` installs into its own hermes-owned dir."""
        monkeypatch.setenv(ld._LAZY_TARGET_ENV, str(tmp_path / "lazy-packages"))
        seen = []

        def fake_run(cmd, **kwargs):
            seen.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("hermes_cli.tools_config.subprocess.run", fake_run)
        monkeypatch.setattr("hermes_cli.managed_uv.ensure_uv", lambda: "/fake/uv")

        own_target = str(tmp_path / "lsp-packages")
        self._pip_install()(["--target", own_target, "--quiet", "pyright"])

        cmd = seen[0]
        assert cmd.count("--target") == 1
        assert own_target in cmd

    def test_virtual_env_is_not_invented_without_a_venv(
        self, monkeypatch, tmp_path, writable_store
    ):
        """No venv → no ``VIRTUAL_ENV``.

        Deriving it from ``sys.executable``'s ancestry names a real directory
        that is not a venv on bundled and store-installed shapes, pointing uv
        at the wrong prefix.
        """
        monkeypatch.delenv(ld._LAZY_TARGET_ENV, raising=False)
        monkeypatch.setattr("sys.prefix", "/some/prefix")
        monkeypatch.setattr("sys.base_prefix", "/some/prefix")
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["env"] = kwargs.get("env") or {}
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("hermes_cli.tools_config.subprocess.run", fake_run)
        monkeypatch.setattr("hermes_cli.managed_uv.ensure_uv", lambda: "/fake/uv")

        self._pip_install()(["somepkg"])
        assert "VIRTUAL_ENV" not in seen["env"]

    def test_virtual_env_is_the_real_prefix_inside_a_venv(
        self, monkeypatch, writable_store
    ):
        monkeypatch.delenv(ld._LAZY_TARGET_ENV, raising=False)
        monkeypatch.setattr("sys.prefix", "/venvs/hermes")
        monkeypatch.setattr("sys.base_prefix", "/usr")
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["env"] = kwargs.get("env") or {}
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("hermes_cli.tools_config.subprocess.run", fake_run)
        monkeypatch.setattr("hermes_cli.managed_uv.ensure_uv", lambda: "/fake/uv")

        self._pip_install()(["somepkg"])
        assert seen["env"]["VIRTUAL_ENV"] == "/venvs/hermes"


class TestLazyLadderVirtualEnv:
    """The lazy ladder must apply the same VIRTUAL_ENV rule as the other one.

    ``tools.environments.local.hermes_subprocess_env`` deliberately strips
    VIRTUAL_ENV so a Hermes-side install cannot clobber another project's
    environment; the lazy ladder then re-adds it for uv. Re-adding a value
    derived from ``sys.executable``'s ancestry names a non-venv directory on
    the bundled and store-installed shapes.
    """

    def _capture_env(self, monkeypatch, tmp_path):
        target = tmp_path / "lazy-packages"
        monkeypatch.setenv(ld._LAZY_TARGET_ENV, str(target))
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["env"] = kwargs.get("env") or {}
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(ld.subprocess, "run", fake_run)
        monkeypatch.setattr("hermes_cli.managed_uv.resolve_uv", lambda: "/fake/uv")
        ld._venv_pip_install(("somepkg==1.0",))
        return seen["env"]

    def test_no_venv_means_no_virtual_env(self, monkeypatch, tmp_path):
        monkeypatch.setattr("sys.prefix", "/opt/payload/python")
        monkeypatch.setattr("sys.base_prefix", "/opt/payload/python")
        assert "VIRTUAL_ENV" not in self._capture_env(monkeypatch, tmp_path)

    def test_real_venv_is_reported_by_prefix(self, monkeypatch, tmp_path):
        monkeypatch.setattr("sys.prefix", "/venvs/hermes")
        monkeypatch.setattr("sys.base_prefix", "/usr")
        assert self._capture_env(monkeypatch, tmp_path)["VIRTUAL_ENV"] == "/venvs/hermes"
