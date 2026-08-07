"""Read-only-install guard in :func:`tools.lazy_deps.ensure` (#48628).

A read-only site-packages (any nix build — the venv lives in the
immutable store) cannot receive lazy pip installs: the uv -> pip ->
ensurepip ladder burns ~15s bootstrapping ensurepip only to fail.
``ensure()`` probes writability directly and must fail fast instead —
no install-method inference involved.
"""

import pytest

from tools import lazy_deps
from tools.lazy_deps import FeatureUnavailable


FEATURE = "provider.anthropic"


@pytest.fixture(autouse=True)
def _missing_and_installable(monkeypatch):
    """Reach the guard: deps missing, installs allowed, no durable target.

    ``_allow_lazy_installs`` is patched explicitly so the suite does not
    depend on the host's ~/.hermes/config.yaml (a local
    ``allow_lazy_installs: false`` otherwise short-circuits with a different
    rejection reason).
    """
    monkeypatch.setattr(lazy_deps, "feature_missing", lambda _f: ("some-pkg==1.0",))
    monkeypatch.setattr(lazy_deps, "_allow_lazy_installs", lambda: True)
    monkeypatch.setattr(lazy_deps, "_lazy_install_target", lambda: None)


def _no_installer(monkeypatch):
    """Fail loudly if the guard lets execution reach the install ladder."""
    def _boom(*_a, **_kw):
        raise AssertionError("guard let execution reach the install ladder")

    monkeypatch.setattr(lazy_deps.subprocess, "run", _boom)


def test_managed_install_fails_fast_without_touching_the_installer(monkeypatch):
    # get_managed_system() passes an unrecognised HERMES_MANAGED value
    # through as-is, so a distro package can name itself here. Homebrew
    # cannot: config.py maps "brew" and "homebrew" to None, because Hermes
    # does not support brew installs.
    monkeypatch.setattr(lazy_deps, "_managed_system", lambda: "apt")
    _no_installer(monkeypatch)

    with pytest.raises(FeatureUnavailable) as excinfo:
        lazy_deps.ensure(FEATURE, prompt=False)

    assert "apt" in excinfo.value.reason
    # refresh_active_features classifies by this prefix — anything else is
    # reported to the user as a hard failure instead of a skip.
    assert excinfo.value.reason.startswith("unsupported ")


def test_readonly_install_fails_fast_without_touching_the_installer(monkeypatch):
    # Read-only site-packages with NO recognized manager (raw nix profile,
    # unmarked distro package): the probe rung must still fail fast.
    monkeypatch.setattr(lazy_deps, "_managed_system", lambda: "")
    monkeypatch.setattr(lazy_deps, "_site_packages_writable", lambda: False)
    _no_installer(monkeypatch)

    with pytest.raises(FeatureUnavailable) as excinfo:
        lazy_deps.ensure(FEATURE, prompt=False)

    assert "read-only" in excinfo.value.reason
    # refresh_active_features classifies by this prefix — anything else is
    # reported to the user as a hard failure instead of a skip.
    assert excinfo.value.reason.startswith("unsupported ")


def test_nix_names_the_extra_and_both_ways_to_set_the_option(monkeypatch):
    """A Nix install gets a remedy it can act on, not a pip command.

    The /nix/store is read-only, so a `uv pip install` hint always fails.
    The message must name the extra to add and the option that adds it.

    get_managed_system() returns "NixOS" for each Nix install, including
    `nix profile install` and nix-darwin on a host that does not run NixOS.
    The message must therefore say Nix, and must give the override form as
    well as the NixOS module option.
    """
    monkeypatch.setattr("hermes_cli.config.get_managed_system", lambda: "NixOS")
    _no_installer(monkeypatch)

    with pytest.raises(FeatureUnavailable) as excinfo:
        lazy_deps.ensure(FEATURE, prompt=False)

    message = str(excinfo.value)
    assert f'"{lazy_deps.LAZY_DEPS[FEATURE]}"' in message, (
        "the message must name the extra to add, not just the feature"
    )
    assert "services.hermes-agent.extraDependencyGroups" in message
    assert "pkgs.hermes-agent.override" in message, (
        "a non-NixOS Nix user has no services.* option to set"
    )
    # A pip command cannot succeed against a read-only store.
    assert "uv pip install" not in message


def test_docker_says_the_image_is_probably_at_fault(monkeypatch):
    """A sealed image contains each runnable extra, so this is a build bug."""
    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "1")
    monkeypatch.setattr("hermes_cli.config.get_managed_system", lambda: None)

    message = lazy_deps.managed_install_reason(FEATURE, "some-extra")
    assert "HERMES_DISABLE_LAZY_INSTALLS" in message
    assert "bug in the image build" in message
    assert "uv pip install" not in message


def test_package_manager_wins_over_the_sealed_flag(monkeypatch):
    """A NixOS host can also set the sealed flag; NixOS is the useful remedy.

    Reporting "this is probably a bug in the image build" to someone who is
    not running the image sends them to the wrong place.
    """
    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "1")
    monkeypatch.setattr("hermes_cli.config.get_managed_system", lambda: "NixOS")

    message = lazy_deps.managed_install_reason(FEATURE, "some-extra")
    assert "services.hermes-agent.extraDependencyGroups" in message
    assert "image build" not in message


def test_reason_is_classified_as_skipped_not_failed(monkeypatch):
    """The wording contract with refresh_active_features, pinned directly."""
    monkeypatch.setattr(lazy_deps, "_site_packages_writable", lambda: False)

    with pytest.raises(FeatureUnavailable) as excinfo:
        lazy_deps.ensure(FEATURE, prompt=False)

    assert excinfo.value.reason.startswith("unsupported "), (
        "refresh_active_features would report this as failed: rather than skipped:"
    )


def test_writable_install_is_not_blocked_by_the_guard(monkeypatch):
    """On a normal writable venv the guard must be transparent."""
    monkeypatch.setattr(lazy_deps, "_site_packages_writable", lambda: True)

    with pytest.raises(FeatureUnavailable) as excinfo:
        lazy_deps.ensure(FEATURE, prompt=False)

    # Whatever stops the install here, it must NOT be the read-only guard.
    assert "read-only installs" not in excinfo.value.reason


def test_durable_install_target_overrides_the_guard(monkeypatch, tmp_path):
    """A configured writable target means lazy installs legitimately work.

    The NixOS container module passes HERMES_MANAGED=true, and the Dockerfile
    sets HERMES_LAZY_INSTALL_TARGET. The managed guard must not stop an
    install that has somewhere to write. On a sealed tree the target now
    also derives from the state folder with no env var at all (doc4 §B).

    The sealed flag is a separate gate, and tests/conftest.py sets it for
    each test, so clear it here to leave the managed guard on its own.
"""
    monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
    monkeypatch.setattr(lazy_deps, "_site_packages_writable", lambda: False)
    monkeypatch.setattr(lazy_deps, "_lazy_install_target", lambda: tmp_path)

    with pytest.raises(FeatureUnavailable) as excinfo:
        lazy_deps.ensure(FEATURE, prompt=False)

    assert "read-only installs" not in excinfo.value.reason, (
        "durable-target installs must not be blocked by the read-only guard"
    )


def test_platform_unsupported_takes_precedence(monkeypatch):
    """A platform-specific reason is more actionable than 'read-only install'.

    Also required for consistency: refresh_active_features pre-checks
    _unsupported_feature_reason before calling ensure().
    """
    monkeypatch.setattr(lazy_deps, "_site_packages_writable", lambda: False)
    monkeypatch.setattr(
        lazy_deps, "_unsupported_feature_reason", lambda _f: "unsupported on win32"
    )

    with pytest.raises(FeatureUnavailable) as excinfo:
        lazy_deps.ensure(FEATURE, prompt=False)

    assert excinfo.value.reason == "unsupported on win32"


def test_probe_errs_toward_writable(monkeypatch):
    """A broken probe must not block installs on a normal venv.

    _site_packages_writable itself returns True when sysconfig/os.access
    misbehave; the install ladder reports real write failures with context.
    """
    import sysconfig

    def _raise(*_a, **_kw):
        raise OSError("probe broke")

    monkeypatch.setattr(sysconfig, "get_paths", _raise)

    assert lazy_deps._site_packages_writable() is True
