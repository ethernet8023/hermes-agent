"""Self-update for the sealed desktop payload (``hermes update`` on a
``desktop-app`` tree).

The sealed bundle updates through electron-updater when the GUI runs. When
the user is at a terminal instead, refusing with "open the app" is a
worse answer than doing what the in-app updater would do — so this module
drives the same motion from the CLI:

1. Read ``resources/app-update.yml`` (electron-builder writes it into every
   packaged app: provider, owner, repo, channel).
2. Fetch the channel manifest (``latest.yml``) from the GitHub releases
   feed and compare its version against the baked install stamp.
3. Download the NSIS installer for this artifact and verify its sha512
   against the manifest before anything is killed.
4. Hand off to a DETACHED helper script, then exit. The helper stops every
   process running from the app root — including the payload python that
   spawned it — waits, runs the installer silently, and relaunches the GUI
   only if it was running before the swap.

The handoff is load-bearing, not a flourish: this command RUNS ON the
payload interpreter, and Windows locks executing binaries. No process that
lives in ``resources/agent-payload`` can replace ``agent-payload`` — the
final step must belong to a process outside the app root (cmd/powershell
from System32). The helper breaks away from the parent's job object so an
Electron-spawned update survives its parent's death.

Windows/NSIS only for now. On other platforms (mac dmg/zip needs a
different swap dance under Gatekeeper) the caller falls back to the
steward refusal message.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Optional


class SealedUpdateUnavailable(Exception):
    """Raised when the CLI self-update path cannot serve this install —
    the caller falls back to the steward refusal message."""


def resolve_app_layout(project_root: Path) -> dict:
    """Locate the app root, resources dir, feed config, and app exe from
    the payload repo root (``<app>/resources/agent-payload/repo``).

    Raises SealedUpdateUnavailable when the tree does not look like a
    bundled desktop app (e.g. a docker/nix sealed tree).
    """
    repo = Path(project_root).resolve()
    payload = repo.parent
    resources = payload.parent
    app_root = resources.parent
    feed_file = resources / "app-update.yml"
    if payload.name != "agent-payload" or not feed_file.is_file():
        raise SealedUpdateUnavailable(
            f"no app-update.yml above {repo} — not a bundled desktop payload"
        )
    exe = _find_app_exe(app_root)
    return {"app_root": app_root, "resources": resources, "feed_file": feed_file, "exe": exe}


def _find_app_exe(app_root: Path) -> Optional[Path]:
    """The launcher exe: the one top-level .exe that is not the uninstaller."""
    candidates = [
        p
        for p in app_root.glob("*.exe")
        if not p.name.lower().startswith("uninstall")
    ]
    return candidates[0] if len(candidates) == 1 else None


def read_feed_config(feed_file: Path) -> dict:
    """Parse app-update.yml. Only the github provider is supported —
    that is what the desktop release workflow publishes."""
    import yaml

    cfg = yaml.safe_load(feed_file.read_text(encoding="utf-8")) or {}
    if cfg.get("provider") != "github" or not cfg.get("owner") or not cfg.get("repo"):
        raise SealedUpdateUnavailable(
            f"unsupported update feed in {feed_file}: provider={cfg.get('provider')!r}"
        )
    cfg.setdefault("channel", "latest")
    return cfg


def channel_manifest_url(cfg: dict) -> str:
    return (
        f"https://github.com/{cfg['owner']}/{cfg['repo']}"
        f"/releases/latest/download/{cfg['channel']}.yml"
    )


def fetch_channel_manifest(cfg: dict, timeout: float = 30.0) -> dict:
    import yaml

    with urllib.request.urlopen(channel_manifest_url(cfg), timeout=timeout) as resp:
        manifest = yaml.safe_load(resp.read().decode("utf-8")) or {}
    if not manifest.get("version") or not manifest.get("files"):
        raise SealedUpdateUnavailable("channel manifest has no version/files")
    return manifest


def pick_artifact(manifest: dict) -> dict:
    """The installer entry for THIS machine. latest.yml is already
    per-platform/arch (the release workflow publishes one manifest per
    (os, arch) lane), so with one file entry there is nothing to choose;
    with several, match the arch token in the name."""
    files = manifest["files"]
    if len(files) == 1:
        return files[0]
    arch = "arm64" if "ARM64" in os.environ.get("PROCESSOR_ARCHITECTURE", "").upper() else "x64"
    for entry in files:
        if arch in entry.get("url", ""):
            return entry
    raise SealedUpdateUnavailable(f"no artifact for arch {arch} in the channel manifest")


def parse_version(v: str) -> tuple:
    parts = re.findall(r"\d+", v or "")
    return tuple(int(p) for p in parts[:3]) or (0,)


def installed_version(repo_root: Path) -> str:
    import json

    stamp = json.loads((repo_root / "install-stamp.json").read_text(encoding="utf-8"))
    return stamp.get("baseVersion") or stamp.get("displayVersion") or "0"


def download_and_verify(cfg: dict, artifact: dict, dest_dir: Path) -> Path:
    """Stream the installer next to nothing that matters, verify sha512
    (base64, electron-builder convention) BEFORE returning. A mismatch
    deletes the file and raises — nothing has been killed yet, so a bad
    download costs nothing."""
    url = artifact["url"]
    if not re.match(r"^[\w.+-]+\.exe$", url):
        raise SealedUpdateUnavailable(f"unexpected artifact name shape: {url!r}")
    full_url = (
        f"https://github.com/{cfg['owner']}/{cfg['repo']}/releases/latest/download/{url}"
    )
    dest = dest_dir / url
    digest = hashlib.sha512()
    with urllib.request.urlopen(full_url, timeout=60) as resp, open(dest, "wb") as out:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            out.write(chunk)
    got = base64.b64encode(digest.digest()).decode("ascii")
    want = artifact.get("sha512", "")
    if got != want:
        dest.unlink(missing_ok=True)
        raise SealedUpdateUnavailable(
            f"sha512 mismatch for {url}: expected {want[:16]}…, got {got[:16]}…"
        )
    return dest


def render_apply_script(app_root: Path, installer: Path, exe: Optional[Path]) -> str:
    """The detached helper. Kill-by-path, not by name: every process whose
    image lives under the app root goes — the Electron shell, the payload
    python (including the parent that spawned this script), payload node —
    and nothing else on the machine. Relaunch only restores what the kill
    took away (GUI running before => GUI running after)."""
    relaunch = ""
    if exe is not None:
        relaunch = f"""
if ($guiWasRunning) {{
  Start-Process -FilePath '{exe}'
}}
"""
    return f"""$ErrorActionPreference = 'Continue'
$appRoot = '{app_root}'
$guiWasRunning = $false
$mine = $PID
foreach ($p in Get-Process) {{
  try {{
    if ($p.Id -ne $mine -and $p.Path -and $p.Path.StartsWith($appRoot, [System.StringComparison]::OrdinalIgnoreCase)) {{
      if ($p.Path -like '*\\Hermes*.exe') {{ $guiWasRunning = $true }}
      Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
    }}
  }} catch {{}}
}}
Start-Sleep -Seconds 2
$inst = Start-Process -FilePath '{installer}' -ArgumentList '/S' -PassThru -Wait
{relaunch}
Remove-Item -Path $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue
exit $inst.ExitCode
"""


def spawn_detached_apply(script_text: str) -> None:
    """Write the helper and launch it OUTSIDE our process tree: new process
    group, breakaway from any job object (Electron wraps children in one),
    no inherited handles into the payload. From this moment the helper owns
    the machine-side swap; we just print and exit."""
    from hermes_cli._subprocess_compat import windows_detach_popen_kwargs

    fd, path = tempfile.mkstemp(suffix=".ps1", prefix="hermes-sealed-update-")
    with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
        f.write(script_text)
    subprocess.Popen(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-WindowStyle",
            "Hidden",
            "-File",
            path,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **windows_detach_popen_kwargs(),
    )


def cmd_update_sealed_desktop(args, project_root: Path) -> int:
    """``hermes update`` on the bundled desktop payload: check the feed,
    download+verify, hand off, exit. Returns a process exit code.

    Raises SealedUpdateUnavailable for every shape this path cannot serve
    (non-Windows, no feed, offline, weird artifacts) — the caller keeps the
    steward refusal as the fallback answer for those.
    """
    if sys.platform != "win32":
        raise SealedUpdateUnavailable("CLI self-update of the sealed bundle is Windows/NSIS only")

    layout = resolve_app_layout(project_root)
    cfg = read_feed_config(layout["feed_file"])
    current = installed_version(Path(project_root))
    manifest = fetch_channel_manifest(cfg)
    latest = str(manifest["version"])

    if parse_version(latest) <= parse_version(current):
        print(f"✓ Hermes Desktop is up to date (v{current}).")
        return 0

    if getattr(args, "check", False):
        print(f"⬆ v{latest} is available (installed: v{current}).")
        print("  Run `hermes update` to install it, or update from the app.")
        return 0

    artifact = pick_artifact(manifest)
    size_mb = int(artifact.get("size", 0)) / (1024 * 1024)
    print(f"⚕ Updating Hermes Desktop v{current} → v{latest}")
    print(f"→ Downloading {artifact['url']} ({size_mb:.0f} MB)...")
    cache_dir = Path(tempfile.gettempdir()) / "hermes-sealed-update"
    cache_dir.mkdir(parents=True, exist_ok=True)
    installer = download_and_verify(cfg, artifact, cache_dir)
    print("→ sha512 verified.")

    script = render_apply_script(layout["app_root"], installer, layout["exe"])
    spawn_detached_apply(script)
    print("→ Handing off to the installer. Every Hermes process (this one")
    print("  included) stops now; the app relaunches if it was running.")
    return 0
