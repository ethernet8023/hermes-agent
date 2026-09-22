"""Model-emitted delivery paths are not authority to read host files."""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway import media_fetch
from gateway.config import Platform, PlatformConfig
from gateway.platforms import base
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override
from tools.terminal_scope import (
    TerminalPolicyUnavailable, get_live_terminal_config, install_and_reset_profile_terminal_scope)


@pytest.mark.parametrize("policy", ["backend: mxc", "backend: mxc\n  mxc_network: true", "backend: ["])
@pytest.mark.parametrize("entry", ["validator", "media", "local"])
def test_delivery_refuses_even_allowlisted_files_without_host_resolution(tmp_path, monkeypatch, policy, entry):
    artifact = tmp_path / "outside" / "fixture.txt"
    artifact.parent.mkdir()
    artifact.write_text("synthetic ungranted bytes", encoding="utf-8")
    monkeypatch.setenv("HERMES_MEDIA_ALLOW_DIRS", str(artifact.parent))
    monkeypatch.setenv("HERMES_MEDIA_DELIVERY_STRICT", "1")
    config = get_hermes_home() / "config.yaml"
    config.write_text("terminal:\n  backend: local\n", encoding="utf-8")
    assert base.validate_media_delivery_path(str(artifact)) == str(artifact.resolve())
    config.write_text(f"terminal:\n  {policy}\n", encoding="utf-8")
    # A policy refusal must not fall through to the remote-export or host-stat
    # fallback. They are leaves, not policy substitutes.
    resolve = Mock(wraps=base._resolve_path)
    fetch = Mock(return_value=str(artifact))
    monkeypatch.setattr(base, "_resolve_path", resolve)
    monkeypatch.setattr(media_fetch, "fetch_remote_media", fetch)

    if entry == "validator":
        assert base.validate_media_delivery_path(str(artifact)) is None
    elif entry == "media":
        assert base.BasePlatformAdapter.filter_media_delivery_paths([(str(artifact), False)]) == []
    else:
        assert base.BasePlatformAdapter.filter_local_delivery_paths([str(artifact)]) == []

    assert resolve.call_count == 0
    assert fetch.call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["during_delay", "notice_failure"])
async def test_image_delivery_rechecks_after_delay_and_contains_notice_failure(monkeypatch, case):
    config = get_hermes_home() / "config.yaml"
    config.write_text("terminal:\n  backend: mxc\n  mxc_network: true\n", encoding="utf-8")
    adapter = _FixtureAdapter(PlatformConfig(enabled=True), Platform.TELEGRAM)
    adapter.send_image = AsyncMock(return_value=base.SendResult(success=True))
    adapter.send = AsyncMock(return_value=base.SendResult(success=True))
    async def revoke(_delay):
        config.write_text("terminal:\n  backend: mxc\n  mxc_network: false\n", encoding="utf-8")
    if case == "notice_failure":
        await revoke(0)
        adapter.send.side_effect = OSError("fixture transport unavailable")
    monkeypatch.setattr(base.asyncio, "sleep", revoke)
    event = MessageEvent(text="fixture", source=SessionSource(platform=Platform.TELEGRAM, chat_id="fixture"))
    receipts = []
    await adapter._send_image_batch(event, [("https://fixture.invalid/image.png", "")], {},
                                    1 if case == "during_delay" else 0, receipts.append)
    adapter.send_image.assert_not_called()
    assert receipts and all(not result.success for result in receipts)


@pytest.mark.asyncio
@pytest.mark.parametrize("next_policy", [None, "backend: mxc\n  mxc_network: false", "backend: ["])
async def test_zero_delay_images_recheck_policy_after_each_send(next_policy):
    config = get_hermes_home() / "config.yaml"
    config.write_text("terminal:\n  backend: mxc\n  mxc_network: true\n", encoding="utf-8")
    adapter = _FixtureAdapter(PlatformConfig(enabled=True), Platform.TELEGRAM)
    adapter.send = AsyncMock(return_value=base.SendResult(success=True))
    sent = []

    async def send_image(**kwargs):
        sent.append(kwargs["image_url"])
        if len(sent) == 1 and next_policy is not None:
            config.write_text(f"terminal:\n  {next_policy}\n", encoding="utf-8")
        await base.asyncio.sleep(0)
        return base.SendResult(success=True)

    adapter.send_image = send_image
    images = [("https://fixture.invalid/first.png", ""), ("https://fixture.invalid/second.png", "")]
    event = MessageEvent(text="fixture", source=SessionSource(platform=Platform.TELEGRAM, chat_id="fixture"))
    receipts = []
    await adapter._send_image_batch(event, images, {}, 0, receipts.append)

    assert sent == [url for url, _ in (images if next_policy is None else images[:1])]
    assert len(receipts) == 1 and receipts[0].success
    assert adapter.send.call_count == (0 if next_policy is None else 1)
    if next_policy is not None:
        reason = "network" if "false" in next_policy else "policy is unavailable"
        assert reason in adapter.send.call_args.kwargs["content"]


class _FixtureAdapter(base.BasePlatformAdapter):
    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        pass

    async def send(self, *args, **kwargs):
        return base.SendResult(success=True)

    async def get_chat_info(self, *args):
        return {}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["media", "local", "image"])
async def test_prevalidated_attachment_rechecks_live_policy_at_delivery(tmp_path, monkeypatch, kind):
    artifact = tmp_path / "fixture.txt"
    artifact.write_text("synthetic ungranted bytes", encoding="utf-8")
    config = get_hermes_home() / "config.yaml"
    config.write_text("terminal:\n  backend: local\n", encoding="utf-8")
    # These dispatch methods receive an already-selected path list. A queued
    # path must be checked again after the active policy changes.
    accepted = str(artifact)
    adapter = _FixtureAdapter(PlatformConfig(enabled=True), Platform.TELEGRAM)
    adapter.send = AsyncMock(return_value=base.SendResult(success=True))
    adapter.send_document = AsyncMock(return_value=base.SendResult(success=True))
    adapter.send_multiple_images = AsyncMock(return_value=base.SendResult(success=True))
    event = MessageEvent(text="fixture", source=SessionSource(platform=Platform.TELEGRAM, chat_id="fixture"))
    recorded = []
    config.write_text("terminal:\n  backend: mxc\n", encoding="utf-8")

    if kind == "image":
        await adapter._send_image_batch(event, [(artifact.as_uri(), "")], {}, 0, recorded.append)
    else:
        await adapter._deliver_media_attachments(
            event, [(accepted, False)] if kind == "media" else [],
            [accepted] if kind == "local" else [], force_document_attachments=False,
            human_delay=0, metadata={}, record_delivery=recorded.append)

    assert adapter.send_document.call_count == 0
    assert adapter.send_multiple_images.call_count == 0
    assert recorded and recorded[0].success is False
    assert "sandbox-aware exporter" in recorded[0].error
    assert "sandbox-aware exporter" in adapter.send.call_args.kwargs["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["MEDIA", "bare", "unknown-extension"])
@pytest.mark.parametrize("policy", ["backend: mxc", "backend: mxc\n  mxc_network: true", "backend: ["])
async def test_response_extraction_explains_refusal_without_probing_model_path(tmp_path, monkeypatch, kind, policy):
    artifact = tmp_path / ("fixture.xyz" if kind == "unknown-extension" else "fixture.txt")
    artifact.write_text("synthetic ungranted bytes", encoding="utf-8")
    (get_hermes_home() / "config.yaml").write_text(f"terminal:\n  {policy}\n", encoding="utf-8")
    path_checks = []
    real_isfile = base.os.path.isfile

    def tracked_isfile(path):
        if str(path) == str(artifact):
            path_checks.append(path)
        return real_isfile(path)

    monkeypatch.setattr(base.os.path, "isfile", tracked_isfile)
    adapter = _FixtureAdapter(PlatformConfig(enabled=True), Platform.TELEGRAM)
    event = MessageEvent(text="fixture", source=SessionSource(platform=Platform.TELEGRAM, chat_id="fixture"))
    content = f"Keep this text.\n{'' if kind == 'bare' else 'MEDIA:'}{artifact}"
    extracted = await adapter._extract_response_content(content, event, "fixture", is_ephemeral_response=False)

    assert not path_checks
    assert extracted.media_files == extracted.local_files == []
    assert "Keep this text." in extracted.text_content
    assert "sandbox-aware exporter" in extracted.text_content


@pytest.mark.asyncio
async def test_post_turn_delivery_uses_owning_profile_a_b_a(tmp_path):
    homes = {}
    for name, backend in (("A", "mxc"), ("B", "local")):
        home = tmp_path / name
        home.mkdir()
        (home / "config.yaml").write_text(f"terminal:\n  backend: {backend}\n", encoding="utf-8")
        homes[name] = home
    # The process/launch profile is local. A's delivery must still refuse.
    (get_hermes_home() / "config.yaml").write_text("terminal:\n  backend: local\n", encoding="utf-8")

    @contextmanager
    def profile_scope(source):
        token = set_hermes_home_override(homes[source.chat_id])
        try:
            with install_and_reset_profile_terminal_scope(homes[source.chat_id]):
                yield
        finally:
            reset_hermes_home_override(token)

    adapter = _FixtureAdapter(PlatformConfig(enabled=True), Platform.TELEGRAM)
    adapter.gateway_runner = SimpleNamespace(_media_delivery_scope_for_source=profile_scope)
    adapter.send = AsyncMock(return_value=base.SendResult(success=True))
    adapter.send_document = AsyncMock(return_value=base.SendResult(success=True))
    artifact = tmp_path / "fixture.txt"
    artifact.write_text("synthetic artifact", encoding="utf-8")
    results = []
    for name in ("A", "B", "A"):
        event = MessageEvent(text="fixture", source=SessionSource(platform=Platform.TELEGRAM, chat_id=name))
        await adapter._deliver_media_attachments(
            event, [(str(artifact), False)], [], force_document_attachments=False,
            human_delay=0, metadata={}, record_delivery=results.append)

    assert [result.success for result in results] == [False, True, False]
    assert adapter.send_document.call_count == 1
    assert adapter.send_document.call_args.kwargs["chat_id"] == "B"


def test_failed_delivery_scope_does_not_fall_back_to_launch_profile():
    (get_hermes_home() / "config.yaml").write_text("terminal:\n  backend: local\n", encoding="utf-8")
    adapter = _FixtureAdapter(PlatformConfig(enabled=True), Platform.TELEGRAM)
    adapter.gateway_runner = SimpleNamespace(
        _media_delivery_scope_for_source=Mock(side_effect=RuntimeError("fixture unavailable")))
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="fixture")

    with adapter._media_delivery_scope(source):
        with pytest.raises(TerminalPolicyUnavailable):
            get_live_terminal_config()
        assert base.validate_media_delivery_path("/ungranted/fixture.txt") is None

    assert get_live_terminal_config()["backend"] == "local"


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed_policy", ["backend: local", "backend: mxc\n  mxc_network: true"])
@pytest.mark.parametrize("denied_policy,reason", [
    ("backend: mxc\n  mxc_network: false", "network"),
    ("backend: [", "policy is unavailable"),
])
async def test_remote_images_recheck_owning_policy_a_b_a(
        tmp_path, allowed_policy, denied_policy, reason):
    homes = {}
    for name in ("A", "B"):
        home = tmp_path / name
        home.mkdir()
        (home / "config.yaml").write_text(f"terminal:\n  {allowed_policy}\n", encoding="utf-8")
        homes[name] = home
    (get_hermes_home() / "config.yaml").write_text("terminal:\n  backend: local\n", encoding="utf-8")

    @contextmanager
    def profile_scope(source):
        token = set_hermes_home_override(homes[source.chat_id])
        try:
            with install_and_reset_profile_terminal_scope(homes[source.chat_id]):
                yield
        finally:
            reset_hermes_home_override(token)

    adapter = _FixtureAdapter(PlatformConfig(enabled=True), Platform.TELEGRAM)
    adapter.gateway_runner = SimpleNamespace(_media_delivery_scope_for_source=profile_scope)
    adapter.send = AsyncMock(return_value=base.SendResult(success=True))
    adapter.send_image = AsyncMock(return_value=base.SendResult(success=True))
    response = 'Keep this text.\n![fixture](https://fixture.invalid/a.png)\n<img src="https://fixture.invalid/b.gif">'
    event_a = MessageEvent(text="fixture", source=SessionSource(platform=Platform.TELEGRAM, chat_id="A"))
    extracted = await adapter._extract_response_content(response, event_a, "fixture", is_ephemeral_response=False)
    assert len(extracted.images) == 2
    assert extracted.text_content == "Keep this text."
    # A queued attachment is not authorized by the policy at extraction time.
    (homes["A"] / "config.yaml").write_text(f"terminal:\n  {denied_policy}\n", encoding="utf-8")
    receipts = []
    for name in ("A", "B", "A"):
        event = MessageEvent(text="fixture", source=SessionSource(platform=Platform.TELEGRAM, chat_id=name))
        await adapter._send_image_batch(event, extracted.images, {}, 0, receipts.append)

    assert [result.success for result in receipts] == [False, True, False]
    assert reason in receipts[0].error
    assert adapter.send_image.call_count == 2
    assert all(call.kwargs["chat_id"] == "B" for call in adapter.send_image.call_args_list)
    assert adapter.send.call_count == 2
    assert all(reason in call.kwargs["content"] for call in adapter.send.call_args_list)
    assert all("fixture.invalid" not in call.kwargs["content"] for call in adapter.send.call_args_list)
