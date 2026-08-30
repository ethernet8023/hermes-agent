"""Regression tests for issue #29866.

A brew Python upgrade (or an interrupted venv rebuild — see also the v0.19.0
report in the same issue) can leave ``certifi`` importable while its bundled
``cacert.pem`` is missing or a dangling symlink. Every TLS connection then
fails with an opaque ``Could not find a suitable TLS CA certificate bundle``
and the gateway is down on all platforms.

Behavior contracts pinned here:

1. The venv-repair import probes (early recovery + `hermes update`) must
   classify certifi as BROKEN when the module imports but ``cacert.pem`` is
   missing or corrupt — an attribute probe alone passes in that state.
2. ``hermes doctor`` must fail the certificate check in that state, and
   ``hermes doctor --fix`` must repair by force-reinstalling certifi and
   re-verifying.
"""

import sys
import types
from pathlib import Path

import pytest

import hermes_cli._early_recovery as er


def _fake_certifi(monkeypatch, bundle_path: Path):
    """Install a fake certifi module whose where() points at bundle_path."""
    fake = types.ModuleType("certifi")
    fake.contents = lambda: ""  # satisfies the ('certifi', 'contents') probe
    fake.where = lambda: str(bundle_path)
    monkeypatch.setitem(sys.modules, "certifi", fake)
    return fake


# =========================================================================
# 1. Import probes detect a missing/corrupt cacert.pem
# =========================================================================


class TestEarlyRecoveryCertifiBundleProbe:
    def test_missing_bundle_flags_certifi_broken(self, monkeypatch, tmp_path):
        _fake_certifi(monkeypatch, tmp_path / "nonexistent" / "cacert.pem")
        broken = er._probe_broken_packages()
        assert "certifi" in broken, (
            "certifi imports but cacert.pem is missing — the probe must flag "
            "it broken (#29866); the attribute check alone passes here"
        )

    def test_tiny_bundle_flags_certifi_broken(self, monkeypatch, tmp_path):
        bundle = tmp_path / "cacert.pem"
        bundle.write_text("truncated", encoding="utf-8")
        _fake_certifi(monkeypatch, bundle)
        broken = er._probe_broken_packages()
        assert "certifi" in broken


    def test_where_raising_flags_certifi_broken(self, monkeypatch):
        fake = types.ModuleType("certifi")
        fake.contents = lambda: ""

        def _boom():
            raise OSError("simulated broken installation")

        fake.where = _boom
        monkeypatch.setitem(sys.modules, "certifi", fake)
        broken = er._probe_broken_packages()
        assert "certifi" in broken


class TestDoctorCertificates:
    def test_broken_bundle_fails_without_fix(self, monkeypatch, capsys, tmp_path):
        from hermes_cli import doctor as doctor_mod

        monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing.pem"))
        issues = []
        doctor_mod.check_certificates(should_fix=False, issues=issues)
        out = capsys.readouterr().out
        assert "broken" in out.lower()
        assert issues, "a broken bundle must be funneled into the action list"
        assert any("doctor --fix" in i for i in issues)

    def test_fix_reinstalls_certifi_and_reverifies(self, monkeypatch, capsys, tmp_path):
        from hermes_cli import doctor as doctor_mod

        # First verification fails, post-reinstall verification succeeds.
        calls = {"verify": 0, "pip": []}

        def fake_verify():
            calls["verify"] += 1
            if calls["verify"] == 1:
                from agent.errors import SSLConfigurationError

                raise SSLConfigurationError("certifi points to a missing CA bundle")

        def fake_run(cmd, **kwargs):
            calls["pip"].append(cmd)

            class _R:
                returncode = 0
                stdout = ""
                stderr = ""

            return _R()

        monkeypatch.setattr(
            "agent.ssl_guard.verify_ca_bundle_with_fallback", fake_verify
        )
        monkeypatch.setattr(doctor_mod.subprocess, "run", fake_run)

        issues = []
        doctor_mod.check_certificates(should_fix=True, issues=issues)
        out = capsys.readouterr().out

        assert calls["pip"], "--fix must run a pip force-reinstall of certifi"
        pip_cmd = calls["pip"][0]
        assert "--force-reinstall" in pip_cmd and "certifi" in pip_cmd
        assert calls["verify"] == 2, "must re-verify after the reinstall"
        assert "repaired" in out.lower()
        assert not issues


    def test_healthy_bundle_never_touches_pip(self, monkeypatch, capsys):
        from hermes_cli import doctor as doctor_mod

        def _fail_run(*a, **k):
            raise AssertionError("healthy bundle must not trigger a reinstall")

        monkeypatch.setattr(doctor_mod.subprocess, "run", _fail_run)
        doctor_mod.check_certificates(should_fix=True, issues=[])
        out = capsys.readouterr().out
        assert "valid" in out.lower()


# =========================================================================
# 3. Startup error message stays actionable
# =========================================================================


class TestSslGuardRepairHint:
    def test_missing_bundle_error_mentions_doctor_fix(self, monkeypatch, tmp_path):
        import certifi

        from agent.errors import SSLConfigurationError
        from agent.ssl_guard import verify_ca_bundle

        monkeypatch.setattr(certifi, "where", lambda: str(tmp_path / "gone.pem"))
        with pytest.raises(SSLConfigurationError) as excinfo:
            verify_ca_bundle()
        message = str(excinfo.value)
        assert "hermes doctor --fix" in message
        assert "certifi" in message
