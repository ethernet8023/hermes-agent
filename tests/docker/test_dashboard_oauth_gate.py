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


def test_dashboard_oauth_gate_engages_on_non_loopback_bind(
    built_image: str, container_name: str,
) -> None:
    """The s6 dashboard run script must NOT auto-add ``--insecure`` when the
    dashboard binds to ``0.0.0.0``. The OAuth auth gate engages on its own
    when a ``DashboardAuthProvider`` is registered (the bundled nous
    provider activates whenever ``HERMES_DASHBOARD_OAUTH_CLIENT_ID`` is
    set).

    Regression guard for the wildcard-subdomain rollout where every
    portal-provisioned agent binds ``0.0.0.0`` and relies on the OAuth
    gate to authenticate browser callers. Before this fix, the run script
    flipped ``--insecure`` on for any non-loopback bind, which routed
    ``start_server`` straight back into the legacy ``allow_public=True``
    branch and disabled the gate every time.

    We verify two independent observable consequences of the gate being
    on:

    1. ``/api/auth/providers`` (publicly reachable through the gate so
       the login page can bootstrap) returns 200 with ``nous`` in the
       provider list — proves the bundled provider registered.
    2. ``/api/sessions`` (a gated route under both the legacy
       ``_SESSION_TOKEN`` middleware and the OAuth gate) returns 401
       to an unauthenticated caller — proves the OAuth gate is actively
       intercepting browser traffic. We deliberately probe a gated route
       here rather than ``/api/status``: status sits in the shared
       ``PUBLIC_API_PATHS`` allowlist (portal liveness probe target) and
       responds 200 without a cookie under both gates, so it cannot
       distinguish "gate on" from "gate off".
    """
    start_container(
        built_image, container_name,
        "HERMES_DASHBOARD=1",
        "HERMES_DASHBOARD_HOST=0.0.0.0",
        "HERMES_DASHBOARD_OAUTH_CLIENT_ID=agent:test-instance",
        cmd="sleep 120",
    )

    # (1) Provider registry visible via the public bootstrap endpoint.
    status_code, body = _http_probe(container_name, "/api/auth/providers")
    assert status_code == 200, (
        f"/api/auth/providers should return 200 when a provider is "
        f"registered; got {status_code} body={body!r}"
    )
    payload = json.loads(body)
    provider_names = [p.get("name") for p in payload.get("providers", [])]
    assert "nous" in provider_names, (
        "Bundled dashboard_auth/nous provider should register when "
        f"HERMES_DASHBOARD_OAUTH_CLIENT_ID is set. Got: {payload!r}"
    )

    # (2) A gated route (``/api/sessions``) returns 401 to an
    #     unauthenticated caller — the OAuth gate is intercepting.
    status_code, body = _http_probe(container_name, "/api/sessions")
    assert status_code == 401, (
        "OAuth gate must intercept gated /api/* routes on 0.0.0.0 bind "
        "when a provider is registered and HERMES_DASHBOARD_INSECURE "
        f"is unset. Got: status={status_code} body={body!r}"
    )

    # (3) ``/api/status`` remains 200 under the gate — it's in the shared
    #     ``PUBLIC_API_PATHS`` allowlist so NAS's wildcard-subdomain
    #     liveness probe (``fly-provider.ts`` ``getInstanceRuntimeStatus``)
    #     can reach it without a cookie. Regression guard: this allowlist
    #     drifted once already and surfaced every healthy agent as
    #     STARTING/down in the portal UI.
    status_code, body = _http_probe(container_name, "/api/status")
    assert status_code == 200, (
        "/api/status must remain publicly reachable under the OAuth gate "
        "— the portal uses it as the wildcard-subdomain liveness probe. "
        f"Got: status={status_code} body={body!r}"
    )
    status = json.loads(body)
    assert status.get("auth_required") is True, (
        "/api/status must report auth_required=True when the OAuth gate "
        f"is engaged so the SPA/portal can distinguish modes. Got: {status!r}"
    )
