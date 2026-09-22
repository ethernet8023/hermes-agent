"""Web extraction treats live policy refusals as final, never provider/cache misses."""
import json
from unittest.mock import Mock

import pytest

from hermes_constants import get_hermes_home
from tools import web_tools_extract, website_policy


def _policy(backend="mxc", network=True, domains=()):
    (get_hermes_home() / "config.yaml").write_text(json.dumps({
        "terminal": {"backend": backend, "mxc_network": network},
        "security": {"website_blocklist": {"enabled": True, "domains": list(domains)}},
    }), encoding="utf-8")
    website_policy._cached_policy = None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "timeout", "batch_errors"])
async def test_rescue_rechecks_network_after_provider_failure(monkeypatch, failure):
    import asyncio
    from plugins.web import keyless_mcp
    _policy()
    class FailingProvider:
        name = "recording"
        async def extract(self, urls, format=None):
            _policy(network=False)
            if failure == "timeout":
                raise asyncio.TimeoutError()
            if failure == "exception":
                raise RuntimeError("fixture provider failed")
            return [{"url": url, "content": "", "error": "provider failed"} for url in urls]
    rescue = Mock(return_value=[])
    monkeypatch.setattr(keyless_mcp, "extract_with_failover", rescue)
    monkeypatch.setattr(web_tools_extract, "_rescue_eligible", lambda _: True)
    results = await web_tools_extract._dispatch_extract(FailingProvider(), ["https://fixture.invalid/a"], None)
    rescue.assert_not_called()
    assert "network" in results[0]["error"].lower()


@pytest.mark.asyncio
async def test_each_dns_lookup_renews_admission(monkeypatch):
    from tools import web_tools
    _policy()
    lookups = []
    async def lookup(url):
        lookups.append(url)
        _policy(network=False)
        return True
    monkeypatch.setattr(web_tools, "async_is_safe_url", lookup)
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    provider = _RecordingProvider()
    monkeypatch.setattr(web_tools, "_resolve_extract_provider", lambda _: (provider, None))
    urls = ["https://one.invalid/a", "https://two.invalid/b"]
    result = json.loads(await web_tools.web_extract_tool(urls))
    assert lookups == urls[:1]
    assert provider.calls == []
    assert all("network" in entry["error"].lower() for entry in result["results"])


class _RecordingProvider:
    name = "recording"

    def __init__(self):
        self.calls = []

    async def extract(self, urls, format=None):
        self.calls.append(list(urls))
        return [{"url": url, "content": "fixture content", "error": None} for url in urls]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["offline", "error", "site", "local", "online"])
async def test_final_policy_refusal_never_reaches_cache_provider_or_rescue(monkeypatch, mode):
    _policy()
    provider = _RecordingProvider()
    urls = ["https://denied.invalid/cached", "https://allowed.invalid/new", "https://denied.invalid/new"]
    seeded = await web_tools_extract._extract_safe_urls(provider, urls[:1], None)
    assert seeded[0]["content"] == "fixture content"
    assert provider.calls == [urls[:1]]
    provider.calls.clear()
    _policy("local" if mode == "local" else "mxc", network=mode != "offline",
            domains=["denied.invalid"] if mode == "site" else [])
    if mode == "error":
        monkeypatch.setattr(website_policy, "check_website_access",
                            Mock(side_effect=RuntimeError("DO_NOT_EXPOSE_POLICY_FIXTURE")))
    rescue = Mock(return_value=[])
    monkeypatch.setattr(web_tools_extract, "_rescue_extract", rescue)

    results = await web_tools_extract._extract_safe_urls(provider, urls, None)
    assert [entry["url"] for entry in results] == urls
    blocked = set(range(len(urls))) if mode in ("offline", "error") else ({0, 2} if mode == "site" else set())
    expected_reason = {"offline": "network", "error": "policy is unavailable", "site": "website policy"}
    for index, entry in enumerate(results):
        if index in blocked:
            assert entry["error"]
            assert expected_reason[mode] in entry["error"]
            assert entry["content"] == ""
        else:
            assert entry["content"] == "fixture content"
    expected_fetch = [url for index, url in enumerate(urls) if index not in blocked and index != 0]
    assert provider.calls == ([expected_fetch] if expected_fetch else [])
    assert "DO_NOT_EXPOSE" not in json.dumps(results)
    rescue.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["before_dns", "before_fetch"])
async def test_direct_extract_rechecks_offline_policy_before_host_io(monkeypatch, transition):
    import socket
    from tools import web_tools
    from tools.environments.mxc_policy import tool_refusal

    urls = ["https://direct-fixture.invalid/a", "https://direct-fixture.invalid/b"]
    _policy()
    assert tool_refusal("web_extract", {"urls": urls}) is None
    provider = _RecordingProvider()
    lookup = Mock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)

    def resolve(backend):
        if transition == "before_fetch":
            _policy(network=False)
        return provider, None

    monkeypatch.setattr(web_tools, "_resolve_extract_provider", resolve)
    if transition == "before_dns":
        _policy(network=False)
    result = json.loads(await web_tools.web_extract_tool(urls))
    assert [entry["url"] for entry in result["results"]] == urls
    assert all("network" in entry["error"] for entry in result["results"])
    assert provider.calls == []
    if transition == "before_dns":
        lookup.assert_not_called()
