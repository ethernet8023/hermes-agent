"""Native-host smoke for the standalone PowerShell bootstrap downloader."""
from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import tempfile
import threading
from typing import cast


class FixtureHTTPServer(ThreadingHTTPServer):
    files: dict[str, bytes]
    requests: list[str]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        server = cast(FixtureHTTPServer, self.server)
        server.requests.append(self.path)
        body = server.files.get(self.path)
        self.send_response(200 if body is not None else 404)
        self.send_header("Content-Length", str(len(body or b"")))
        self.end_headers()
        self.wfile.write(body or b"")

    def log_message(self, format: str, *args: object) -> None:
        pass


def ps_quote(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def verify(server: FixtureHTTPServer, root: Path, mode: str) -> None:
    body = b"pinned bootstrap bytes"
    digest = hashlib.sha256(body).hexdigest()
    base = f"http://127.0.0.1:{server.server_port}"
    files = {"/mirror": body}
    if mode == "corrupt":
        files["/primary"] = b"wrong bytes"
    elif mode in ("release", "primary"):
        files["/primary"] = body
    if mode == "both-missing":
        files.clear()
    server.files = files
    server.requests.clear()
    destination = root / f"{mode}.bin"
    script = root / f"{mode}.ps1"
    release = f" -GitHubUrl {ps_quote(base + '/release')}" if mode == "release" else ""
    script.write_text(
        f". {ps_quote(Path.cwd() / 'scripts/install.ps1')} "
        f"-HermesHome {ps_quote(root / 'home')} -InstallDir {ps_quote(root / 'repo')}\n"
        f"Invoke-VerifiedDownload{release} -Url {ps_quote(base + '/primary')} "
        f"-MirrorUrl {ps_quote(base + '/mirror')} -Sha256 {ps_quote(digest)} "
        f"-OutFile {ps_quote(destination)}\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        capture_output=True, text=True, timeout=45,
    )
    expected = {
        "release": ["/release", "/mirror"],
        "primary": ["/primary"],
        "missing": ["/primary", "/mirror"],
        "corrupt": ["/primary"],
        "both-missing": ["/primary", "/mirror"],
    }[mode]
    assert server.requests == expected, (mode, server.requests, result.stderr)
    if mode in ("release", "primary", "missing"):
        assert result.returncode == 0, (mode, result.stdout, result.stderr)
        assert destination.read_bytes() == body
    else:
        assert result.returncode != 0, (mode, result.stdout, result.stderr)
        if mode == "corrupt":
            assert "digest mismatch" in result.stdout + result.stderr
            assert not destination.exists()
    print("NATIVE_BOOTSTRAP", mode, "OK")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="pm-native-bootstrap-") as temporary:
        root = Path(temporary)
        server = FixtureHTTPServer(("127.0.0.1", 0), Handler)
        server.files, server.requests = {}, []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for mode in ("release", "primary", "missing", "corrupt", "both-missing"):
                verify(server, root, mode)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
