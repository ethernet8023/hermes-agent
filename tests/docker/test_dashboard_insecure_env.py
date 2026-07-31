"""Split from test_dashboard.py: each boot-heavy test lives in its own
file so the per-file parallel runner (scripts/run_tests_parallel.py)
can overlap container boots across workers instead of serializing
~110s boots inside one file. Shared docstring/context: see the
original header in test_dashboard.py.
"""
from __future__ import annotations

import json
import time

from tests.docker.conftest import docker_exec, docker_exec_sh, start_container, poll_container, _http_probe


def test_dashboard_insecure_env_var_no_longer_bypasses_gate(
    built_image: str, container_name: str,
) -> None:
    """``HERMES_DASHBOARD_INSECURE=1`` NO LONGER disables the auth gate
    (June 2026 hardening). With insecure set on a 0.0.0.0 bind and NO auth
    provider registered, start_server fails closed — the dashboard never
    binds, so ``/api/status`` is unreachable. This proves the unauthenticated
    public-dashboard escape hatch is gone: there is no env that serves the
    dashboard on a public bind without an auth provider.
    """
    start_container(
        built_image, container_name,
        "HERMES_DASHBOARD=1",
        "HERMES_DASHBOARD_HOST=0.0.0.0",
        "HERMES_DASHBOARD_INSECURE=1",
        cmd="sleep 120",
    )
    # Fail-closed: the dashboard process must NOT successfully serve. Probe
    # for a few seconds; /api/status should never become reachable because
    # start_server raised SystemExit before binding.
    ok, _ = poll_container(
        container_name,
        "curl -fsS -m 2 http://127.0.0.1:9119/api/status >/dev/null 2>&1",
        deadline_s=12.0,
    )
    assert not ok, (
        "Dashboard must NOT serve on a public bind with --insecure and no "
        "auth provider — the gate fails closed. /api/status became reachable, "
        "meaning the unauthenticated escape hatch is still open."
    )
