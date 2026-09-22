"""Vision source admission precedes host DNS, fetches and SVG conversion."""
import base64
import json
import socket
from unittest.mock import AsyncMock, Mock

import pytest

from hermes_constants import get_hermes_home
from tools import image_source


def _policy(backend="mxc", network=False):
    (get_hermes_home() / "config.yaml").write_text(json.dumps({"terminal": {
        "backend": backend, "mxc_network": network,
    }}), encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("backend,network,allowed", [
    ("mxc", False, False), ("mxc", True, True), ("local", False, True),
])
async def test_remote_image_policy_precedes_dns_and_download(monkeypatch, backend, network, allowed):
    _policy(backend, network)
    lookup = Mock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    download = AsyncMock(return_value=b"GIF89a" + b"\0" * 20)
    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    monkeypatch.setattr(image_source, "_download_to_bytes", download)
    source = "https://fixture.invalid/fixture.gif"

    if allowed:
        image = await image_source.resolve_image_source(source, image_source.ResolveContext())
        assert image.mime == "image/gif"
        assert lookup.call_count > 0
        download.assert_awaited_once_with(source)
    else:
        with pytest.raises(image_source.SourceUnsafe, match="network"):
            await image_source.resolve_image_source(source, image_source.ResolveContext())
        lookup.assert_not_called()
        download.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend,network", [("mxc", False), ("mxc", True), ("local", False)])
async def test_svg_refused_before_converter_but_raster_data_still_prepares(monkeypatch, backend, network):
    from tools import vision_tools, vision_tools_image_prep

    _policy(backend, network)
    # A recording converter is the only replaced leaf; never invoke an optional renderer.
    raster = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")

    def convert(source, target):
        target.write_bytes(raster)
        return True

    converter = Mock(side_effect=convert)
    monkeypatch.setattr(vision_tools_image_prep, "_rasterize_svg_to_png", converter)
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><image href="https://fixture.invalid/remote.png"/></svg>'
    data_url = "data:image/svg+xml;base64," + base64.b64encode(svg).decode()
    if backend == "mxc":
        with pytest.raises(vision_tools._ImagePrepError, match="SVG.*sandbox"):
            await vision_tools._prepare_image(data_url, "fixture", None, validate_decode=True)
        converter.assert_not_called()
    else:
        prepared = await vision_tools._prepare_image(data_url, "fixture", None, validate_decode=True)
        assert prepared.path.read_bytes() == raster
        prepared.path.unlink()
        converter.assert_called_once()

    raster_url = "data:image/png;base64," + base64.b64encode(raster).decode()
    upload = get_hermes_home() / "images" / "fixture.png"
    upload.parent.mkdir(exist_ok=True)
    upload.write_bytes(raster)
    for source in (raster_url, str(upload)):
        prepared = await vision_tools._prepare_image(source, "fixture", None, validate_decode=True)
        assert prepared.mime == "image/png"
        assert prepared.path.read_bytes() == raster
        prepared.path.unlink()
