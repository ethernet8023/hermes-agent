"""Sandbox (Windows MXC) routes for the desktop's Safety > Sandbox panel.

One resolver owns the policy: every read goes through ``tools.environments.mxc_host.status``
and every write lands in ``config.yaml``'s ``terminal`` section, so the panel, the CLI and the
running backend agree about what is granted. Writes reconcile old execution before returning.
"""

from __future__ import annotations

import asyncio
import os
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from hermes_cli.web_deps import late
from hermes_cli.web_routers._common import config_write_scope, http_failure, scoped_to_thread

router = APIRouter()

load_config = late("load_config", "hermes_cli.config")
save_config = late("save_config", "hermes_cli.config")
reconcile_terminal_policy = late("reconcile_terminal_policy", "tools.terminal_policy_lifecycle")


class SandboxPolicyUpdate(BaseModel):
    enabled: Optional[bool] = None
    readwrite_paths: Optional[List[str]] = None
    readonly_paths: Optional[List[str]] = None
    network: Optional[bool] = None
    profile: Optional[str] = None


class SandboxGrant(BaseModel):
    path: str
    expected_target: Optional[str] = None
    mode: str = "read"  # "read" | "readwrite"
    profile: Optional[str] = None


class SandboxPrepare(BaseModel):
    path: Optional[str] = None
    profile: Optional[str] = None


def _terminal_section(config: dict) -> dict:
    section = config.setdefault("terminal", {})
    if not isinstance(section, dict):
        section = {}
        config["terminal"] = section
    return section


def _workspace_path(config: dict) -> str:
    """The folder a new session's sandbox treats as read/write: ``terminal.cwd`` when it names a
    real directory, else the process working directory."""
    raw = str((_terminal_section(config).get("cwd") or "").strip())
    if raw and raw not in (".", "auto", "cwd"):
        expanded = os.path.expandvars(os.path.expanduser(raw))
        if os.path.isdir(expanded):
            return os.path.normpath(expanded)
    return os.getcwd()


def _status_payload(*, provision_shell: bool = False, workspace: Optional[str] = None) -> dict:
    from tools.environments.mxc_host import sandbox_workspace_for, status

    record = status(provision_shell=provision_shell)
    config = load_config()
    resolved = os.path.normpath(os.path.expandvars(os.path.expanduser(workspace))) if workspace else _workspace_path(config)
    # Show the folder the sandbox will actually use: a session anchored at home reports the default workspace.
    record["workspace"] = sandbox_workspace_for(resolved)
    return record


def _validated_paths(paths: List[str], *, writable: bool) -> List[str]:
    from tools.environments.mxc_host import validate_grant_paths
    try:
        return validate_grant_paths(paths, writable=writable)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _grant_target(path: str, *, writable: bool = False) -> str:
    """Resolve the recursive folder scope identically for preview and commit."""
    raw = path.strip()
    if not raw or not os.path.isabs(raw):
        raise HTTPException(status_code=400, detail="path must be absolute")
    target = os.path.normpath(os.path.expandvars(os.path.expanduser(raw)))
    if os.path.isfile(target) or (not os.path.exists(target) and os.path.splitext(target)[1]):
        target = os.path.dirname(target)
    target = _validated_paths([target], writable=writable)[0]
    if not os.path.isdir(target):
        raise HTTPException(status_code=400, detail=f"folder does not exist: {target}")
    return target


@router.get("/api/sandbox/grant-target")
async def get_sandbox_grant_target(path: str, profile: Optional[str] = None):
    with http_failure("Failed to preview sandbox access", 500, "Sandbox grant preview failed"):
        return await scoped_to_thread(profile, lambda: {"target": _grant_target(path), "recursive": True})


@router.get("/api/sandbox/status")
async def get_sandbox_status(profile: Optional[str] = None, provision: bool = False, workspace: Optional[str] = None,
                             refresh: bool = False):
    """Availability, current policy and readiness for the desktop panel. ``provision=true``
    installs the sandbox shell when it is missing (a download), which the opt-in toggle uses;
    ``workspace`` names the folder to resolve (defaults to terminal.cwd);
    ``refresh=true`` re-runs the host probe instead of serving the cached verdict."""
    if refresh:
        from tools.environments.mxc_host import clear_probe_cache
        clear_probe_cache()
    with http_failure("Failed to read sandbox status", 500, "Sandbox status failed"):
        def _read():
            # A saved policy is not proof that retiring prior execution succeeded.
            reconcile_terminal_policy()
            return _status_payload(provision_shell=provision, workspace=workspace)
        return await scoped_to_thread(profile, _read)


def provision_sandbox_bins() -> None:
    """Install the kit and the shell into the writable pm store when missing.

    A sealed install carries both pins in its payload, so there is nothing to
    fetch and a missing copy is a rebuild, not a download. A source install
    downloads only the pin the store lacks. Raises ``SealedInstall`` or
    ``InstallError``; the caller leaves the toggle off and shows the message.
    """
    from pm.install import ensure, is_installed, sealed
    from pm.package import InstallError

    if sealed():
        missing = [name for name in ("mxc-kit", "busybox") if not is_installed(name)]
        if missing:
            raise SealedInstall(missing)
        return
    for name in ("mxc-kit", "busybox"):
        if not is_installed(name):
            ensure(name, explicit=True)


class SealedInstall(Exception):
    """The payload ships its own kit, and this one does not have it."""

    def __init__(self, missing: list[str]):
        self.missing = missing
        super().__init__(
            "this install is sealed and does not ship: "
            + ", ".join(missing)
            + " — rebuild the bundle to get the sandbox kit")


@router.post("/api/sandbox/policy")
async def update_sandbox_policy(body: SandboxPolicyUpdate, profile: Optional[str] = None):
    """Persist policy fields and/or switch the terminal backend. Enabling refuses (400) when the
    host cannot run MXC, with the same plain-language reason the status reports."""
    from tools.environments.mxc_host import status

    def _run():
        with config_write_scope(body.profile or profile):
            if body.enabled:
                from pm.package import InstallError
                try:
                    provision_sandbox_bins()
                except (SealedInstall, InstallError) as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                record = status(provision_shell=False)
                if not record["available"]:
                    raise HTTPException(status_code=400, detail=record["reason"] or "MXC is not available on this host.")
            config = load_config()
            terminal = _terminal_section(config)
            before = str(terminal.get("backend") or "local")
            if body.enabled is True:
                terminal["backend"] = "mxc"
            elif body.enabled is False and before == "mxc":
                terminal["backend"] = "local"
            readwrite = _validated_paths(body.readwrite_paths, writable=True) if body.readwrite_paths is not None else None
            readonly = _validated_paths(body.readonly_paths, writable=False) if body.readonly_paths is not None else None
            if readwrite is not None:
                terminal["mxc_readwrite_paths"] = readwrite
            if readonly is not None:
                terminal["mxc_readonly_paths"] = readonly
            if body.network is not None:
                terminal["mxc_network"] = bool(body.network)
            save_config(config)
            reconcile_terminal_policy()
            return _status_payload()

    with http_failure("Failed to update sandbox policy", 500, "Sandbox policy update failed"):
        return await asyncio.to_thread(_run)


@router.post("/api/sandbox/grant")
async def grant_sandbox_path(body: SandboxGrant, profile: Optional[str] = None):
    """Add one folder to the policy (from a denied tool result's "Grant access" action). A file
    path grants its parent folder; a read/write grant supersedes a read-only one."""
    mode = (body.mode or "read").strip().lower()
    if mode not in ("read", "readwrite"):
        raise HTTPException(status_code=400, detail="mode must be 'read' or 'readwrite'")
    def _run():
        with config_write_scope(body.profile or profile):
            target = _grant_target(body.path, writable=mode == "readwrite")
            if body.expected_target is not None and os.path.normcase(target) != os.path.normcase(body.expected_target):
                raise HTTPException(status_code=409, detail="Grant target changed. Preview the folder again before granting access.")
            config = load_config()
            terminal = _terminal_section(config)
            key = "mxc_readwrite_paths" if mode == "readwrite" else "mxc_readonly_paths"
            current = _validated_paths(list(terminal.get(key) or []), writable=mode == "readwrite")
            if target.lower() not in {p.lower() for p in current}:
                current.append(target)
            terminal[key] = current
            if mode == "readwrite":
                terminal["mxc_readonly_paths"] = [
                    p for p in _validated_paths(list(terminal.get("mxc_readonly_paths") or []), writable=False)
                    if p.lower() != target.lower()]
            save_config(config)
            reconcile_terminal_policy()
            return {"granted": target, "mode": mode, **_status_payload()}

    with http_failure("Failed to grant sandbox access", 500, "Sandbox grant failed"):
        return await asyncio.to_thread(_run)


@router.delete("/api/sandbox/grant")
async def revoke_sandbox_path(body: SandboxGrant, profile: Optional[str] = None):
    # Revocation must remain possible even if a saved directory is gone or is now protected.
    def _run():
        with config_write_scope(body.profile or profile):
            config = load_config()
            terminal = _terminal_section(config)
            target = os.path.normcase(os.path.normpath(body.path))
            for key in ("mxc_readonly_paths", "mxc_readwrite_paths"):
                terminal[key] = [p for p in terminal.get(key, [])
                                 if os.path.normcase(os.path.normpath(p)) != target]
            save_config(config)
            reconcile_terminal_policy()
            return _status_payload()
    with http_failure("Failed to revoke sandbox access", 500, "Sandbox revoke failed"):
        return await asyncio.to_thread(_run)


@router.post("/api/sandbox/prepare")
async def prepare_sandbox_workspace(body: SandboxPrepare, profile: Optional[str] = None):
    raise HTTPException(status_code=410, detail="Workspace ACL preparation is retired. Hermes never changes ancestor ACLs.")
