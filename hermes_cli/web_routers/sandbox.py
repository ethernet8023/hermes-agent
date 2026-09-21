"""Sandbox (Windows MXC) routes for the desktop's Safety > Sandbox panel.

One resolver owns the policy: every read goes through ``tools.environments.mxc_host.status``
and every write lands in ``config.yaml``'s ``terminal`` section, so the panel, the CLI and the
running backend can never disagree about what is granted. A policy edit applies to the next
sandboxed command; nothing is restarted.
"""

from __future__ import annotations

import asyncio
import os
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from hermes_cli.web_deps import late
from hermes_cli.web_routers._common import config_write_scope, http_failure, log, scoped_to_thread

router = APIRouter()

load_config = late("load_config", "hermes_cli.config")
save_config = late("save_config", "hermes_cli.config")


class SandboxPolicyUpdate(BaseModel):
    enabled: Optional[bool] = None
    readwrite_paths: Optional[List[str]] = None
    readonly_paths: Optional[List[str]] = None
    network: Optional[bool] = None
    profile: Optional[str] = None


class SandboxGrant(BaseModel):
    path: str
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
    from tools.environments.mxc_host import ancestor_readiness, sandbox_workspace_for, status

    record = status(provision_shell=provision_shell)
    config = load_config()
    resolved = os.path.normpath(os.path.expandvars(os.path.expanduser(workspace))) if workspace else _workspace_path(config)
    # Show the folder the sandbox will actually use: a session anchored at home reports the default workspace.
    record["workspace"] = sandbox_workspace_for(resolved)
    if record["platform_supported"]:
        record["workspace_ancestors"] = ancestor_readiness(record["workspace"])
    return record


def _clean_paths(paths: Optional[List[str]]) -> Optional[List[str]]:
    if paths is None:
        return None
    seen: set[str] = set()
    cleaned: List[str] = []
    for raw in paths:
        text = str(raw or "").strip()
        if not text:
            continue
        native = os.path.normpath(os.path.expandvars(os.path.expanduser(text)))
        if native.lower() in seen:
            continue
        seen.add(native.lower())
        cleaned.append(native)
    return cleaned


@router.get("/api/sandbox/status")
async def get_sandbox_status(profile: Optional[str] = None, provision: bool = False, workspace: Optional[str] = None,
                             refresh: bool = False):
    """Availability, current policy and readiness for the desktop panel. ``provision=true``
    installs the sandbox shell when it is missing (a download), which the opt-in toggle uses;
    ``workspace`` names the folder whose ancestor readiness to report (defaults to terminal.cwd);
    ``refresh=true`` re-runs the host probe instead of serving the cached verdict."""
    if refresh:
        from tools.environments.mxc_host import clear_probe_cache
        clear_probe_cache()
    with http_failure("Failed to read sandbox status", 500, "Sandbox status failed"):
        return await scoped_to_thread(profile, lambda: _status_payload(provision_shell=provision, workspace=workspace))


def _apply_backend_switch_to_this_process() -> None:
    """Make a terminal-backend change reach the running server without a restart.

    Outside multiplexed hosting the terminal tool reads its backend from the process env that was
    bridged at startup, and it caches one environment per task; both would keep serving the old
    backend. Re-bridging config.yaml and dropping the cached environments means the next command
    of every session is created against the new backend (multiplexed profiles already re-read the
    file per turn)."""
    from agent.secret_scope import is_multiplex_active
    from tools.terminal_tool import _active_environments
    from tools.terminal_tool_lifecycle import cleanup_vm

    if not is_multiplex_active():
        from hermes_cli.config import apply_terminal_config_to_env
        apply_terminal_config_to_env(env=None)
    for task_id in list(_active_environments.keys()):
        cleanup_vm(task_id)


@router.post("/api/sandbox/policy")
async def update_sandbox_policy(body: SandboxPolicyUpdate, profile: Optional[str] = None):
    """Persist policy fields and/or switch the terminal backend. Enabling refuses (400) when the
    host cannot run MXC, with the same plain-language reason the status reports."""
    from tools.environments.mxc_host import status

    def _run():
        backend_changed = False
        with config_write_scope(body.profile or profile):
            if body.enabled:
                record = status(provision_shell=True)
                if not record["available"]:
                    raise HTTPException(status_code=400, detail=record["reason"] or "MXC is not available on this host.")
            config = load_config()
            terminal = _terminal_section(config)
            before = str(terminal.get("backend") or "local")
            if body.enabled is True:
                terminal["backend"] = "mxc"
            elif body.enabled is False and before == "mxc":
                terminal["backend"] = "local"
            backend_changed = str(terminal.get("backend") or "local") != before
            readwrite = _clean_paths(body.readwrite_paths)
            readonly = _clean_paths(body.readonly_paths)
            if readwrite is not None:
                terminal["mxc_readwrite_paths"] = readwrite
            if readonly is not None:
                terminal["mxc_readonly_paths"] = readonly
            if body.network is not None:
                terminal["mxc_network"] = bool(body.network)
            save_config(config)
            if backend_changed:
                try:
                    _apply_backend_switch_to_this_process()
                except Exception:  # noqa: BLE001 — the file is saved; a restart still applies it
                    log.warning("sandbox: live backend switch did not fully apply", exc_info=True)
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
    target = os.path.normpath(os.path.expandvars(os.path.expanduser(body.path.strip())))
    if not os.path.isabs(target):
        raise HTTPException(status_code=400, detail="path must be absolute")
    if os.path.isfile(target) or (not os.path.exists(target) and os.path.splitext(target)[1]):
        target = os.path.dirname(target)
    if not os.path.isdir(target):
        raise HTTPException(status_code=400, detail=f"folder does not exist: {target}")

    def _run():
        with config_write_scope(body.profile or profile):
            config = load_config()
            terminal = _terminal_section(config)
            key = "mxc_readwrite_paths" if mode == "readwrite" else "mxc_readonly_paths"
            current = _clean_paths(list(terminal.get(key) or [])) or []
            if target.lower() not in {p.lower() for p in current}:
                current.append(target)
            terminal[key] = current
            if mode == "readwrite":
                terminal["mxc_readonly_paths"] = [
                    p for p in (_clean_paths(list(terminal.get("mxc_readonly_paths") or [])) or [])
                    if p.lower() != target.lower()]
            save_config(config)
        return {"granted": target, "mode": mode, **_status_payload()}

    with http_failure("Failed to grant sandbox access", 500, "Sandbox grant failed"):
        return await asyncio.to_thread(_run)


@router.post("/api/sandbox/prepare")
async def prepare_sandbox_workspace(body: SandboxPrepare, profile: Optional[str] = None):
    """Make the workspace's ancestor folders discoverable to the container (needed by git).
    Unelevated: user-owned folders are prepared here; the rest come back as ``needs_admin`` with
    the exact command for an administrator prompt."""
    from tools.environments.mxc_host import prepare_ancestors

    def _run():
        from hermes_cli.web_routers._common import _profile_scope
        with _profile_scope(body.profile or profile):
            workspace = body.path or _workspace_path(load_config())
            result = prepare_ancestors(workspace)
            return {**_status_payload(workspace=workspace), **result}

    with http_failure("Failed to prepare sandbox workspace", 500, "Sandbox prepare failed"):
        return await asyncio.to_thread(_run)
