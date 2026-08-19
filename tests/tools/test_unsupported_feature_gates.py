"""Tests for _unsupported_feature_reason platform gates.

Ensures platform.matrix and stt.silk fail fast on platforms where
their dependencies have no wheels, instead of hanging for 300s on a
doomed sdist build.
"""

import sys

import pytest

from tools.lazy_deps import _unsupported_feature_reason


class TestPlatformMatrixGate:
    def test_blocked_on_macos(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        reason = _unsupported_feature_reason("platform.matrix")
        assert reason is not None
        assert "python-olm" in reason
        assert reason.startswith("unsupported ")

    def test_blocked_on_windows(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        reason = _unsupported_feature_reason("platform.matrix")
        assert reason is not None
        assert "python-olm" in reason

    def test_allowed_on_linux(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        reason = _unsupported_feature_reason("platform.matrix")
        assert reason is None, "Matrix should be installable on Linux"

    def test_other_features_unaffected(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        for feature in ["provider.anthropic", "platform.discord", "tts.edge"]:
            assert _unsupported_feature_reason(feature) is None


class TestSttSilkGate:
    def test_blocked_on_macos(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        reason = _unsupported_feature_reason("stt.silk")
        assert reason is not None
        assert "pilk" in reason.lower() or "silk" in reason.lower()
        assert reason.startswith("unsupported ")

    def test_blocked_on_linux(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        reason = _unsupported_feature_reason("stt.silk")
        assert reason is not None
        assert "pilk" in reason.lower() or "silk" in reason.lower()

    def test_allowed_on_windows(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        reason = _unsupported_feature_reason("stt.silk")
        assert reason is None, "pilk should be installable on Windows (has wheels)"
