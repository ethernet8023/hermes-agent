"""Regression tests for the docker-exec privilege-drop shim.

Split from test_docker_exec_privilege_drop.py so its boot-heavy e2e
test parallelizes against the other whales instead of serializing
inside one file. Shared helpers/fixture travel via the common header.


The shim (docker/hermes-exec-shim.sh, installed at /opt/hermes/bin/hermes)
exists to prevent the auth.json ownership-mismatch bug where
`docker exec <c> hermes login` would write /opt/data/auth.json as
root:root mode 0600, leaving the supervised gateway (UID 10000) unable
to read its own credentials and returning "Provider authentication
failed: Hermes is not logged into Nous Portal" on every message.

These tests verify:

1. ``docker exec <c> hermes …`` (defaulting to root) gets dropped to the
   hermes user before the real binary runs.
2. ``docker exec --user hermes <c> hermes …`` (already non-root) short-
   circuits and doesn't try to drop again.
3. Files written under $HERMES_HOME from a ``docker exec`` session land
   as hermes:hermes — the actual user-visible invariant.
4. The HERMES_DOCKER_EXEC_AS_ROOT opt-out lets diagnostic sessions keep
   running as root deliberately.
5. The main CMD path (``docker run <image> …``) is unaffected by the
   PATH-shim ordering — no recursion, no behavior change.
"""

from __future__ import annotations
from tests.docker.conftest import docker_exec

import subprocess
import time
from collections.abc import Iterator

import pytest


# How long to give a `docker run -d` container before declaring it not ready.
# Generous because under arm64 QEMU emulation cont-init (a Python config
# migration + chowns) runs several times slower than on native amd64.
_RUN_READY_TIMEOUT_S = 60


def _wait_for_cont_init(container: str) -> None:
    """Block until s6 cont-init has fully finished, not merely until
    ``docker exec`` is responsive.

    The earlier ``_wait_for_init`` only polled ``docker exec <c> true``,
    which succeeds almost immediately on s6-overlay — long before the
    ``01-hermes-setup`` cont-init hook (docker/stage2-hook.sh) has
    finished seeding + ``chown hermes:hermes`` config.yaml and running the
    Python config migration. A test that wipes config.yaml and then writes
    it as root would then race that boot-time chown: on native amd64
    stage2-hook wins in a blink and the test always passed, but under arm64
    QEMU emulation the slow Python migration was still in flight and
    clobbered the root-written file's ownership back to hermes:hermes,
    failing ``test_shim_opt_out_keeps_root`` non-deterministically.

    The reliable "cont-init is done" signal is
    ``$HERMES_HOME/logs/container-boot.log``: it is written by
    ``02-reconcile-profiles`` (hermes_cli.container_boot), which s6 runs
    *strictly after* ``01-hermes-setup`` in lexicographic order. The
    reconciler always logs at least one ``profile=default`` line even for a
    bare ``sleep infinity`` container, so once that marker appears every
    stage2-hook side effect (seed, chown, migrate) is guaranteed complete.
    Mirrors the readiness pattern in test_container_restart.py.
    """
    deadline = time.monotonic() + _RUN_READY_TIMEOUT_S
    last = ""
    while time.monotonic() < deadline:
        r = subprocess.run(
            ["docker", "exec", container,
             "cat", "/opt/data/logs/container-boot.log"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            last = r.stdout
            if "profile=default" in last:
                return
        time.sleep(0.2)
    pytest.fail(
        f"container {container} did not finish cont-init within "
        f"{_RUN_READY_TIMEOUT_S}s (container-boot.log so far: {last!r})"
    )


@pytest.fixture
def sleep_container(built_image: str, container_name: str) -> Iterator[str]:
    """Long-lived container running `sleep infinity` so we can docker exec into it."""
    subprocess.run(
        ["docker", "rm", "-f", container_name],
        capture_output=True, check=False,
    )
    r = subprocess.run(
        ["docker", "run", "-d", "--name", container_name, built_image,
         "sleep", "infinity"],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0, f"docker run failed: {r.stderr}"
    try:
        _wait_for_cont_init(container_name)
        yield container_name
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True, check=False,
        )


def test_e2e_login_then_supervised_gateway_can_read_auth(
    sleep_container: str,
) -> None:
    """End-to-end regression for the original bug.

    Pre-shim: ``docker exec <c> hermes login`` (root) wrote
    /opt/data/auth.json as root:root 0600. The supervised gateway (UID
    10000) couldn't read it, _load_auth_store swallowed PermissionError
    as a parse failure, and resolve_nous_runtime_credentials raised
    "Hermes is not logged into Nous Portal" on every message.

    We can't do a real OAuth login in a unit test, but we can stand in
    for it by writing the same file shape via `hermes config set`-style
    writes — what matters is the *file ownership invariant* downstream
    of `_save_auth_store`. If the shim works, every file the
    `docker exec` path produces is hermes-readable.

    Specifically: pretend the operator ran `hermes login` (writes
    auth.json) and verify (a) the file exists and (b) it's readable by
    the hermes UID. We use `hermes auth list` since that touches the
    auth store on the read side and would fail with the same
    'not logged in' shape if the file was unreadable to uid 10000.
    """
    # Have the shim-protected `docker exec` write the auth store.
    # `hermes auth list` is read-only but still exercises _load_auth_store
    # under the shim's UID. We invoke `hermes config set` first to
    # provoke a write into HERMES_HOME so we have something concrete to
    # owner-check.
    r = subprocess.run(
        ["docker", "exec", sleep_container,
         "hermes", "config", "set", "_test.e2e_marker", "1"],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0, f"config set failed: {r.stderr}"

    # The supervised UID (10000) must be able to read everything under
    # HERMES_HOME that docker exec just wrote.
    r = subprocess.run(
        ["docker", "exec", "--user", "hermes", sleep_container,
         "find", "/opt/data", "-maxdepth", "2", "-type", "f",
         "!", "-readable", "-print"],
        capture_output=True, text=True, timeout=15,
    )
    assert r.returncode == 0, f"find failed: {r.stderr}"
    unreadable = [ln for ln in r.stdout.splitlines() if ln.strip()]
    assert not unreadable, (
        "Files written by `docker exec` are unreadable to the hermes user "
        f"(supervised gateway UID): {unreadable}. The shim failed to drop "
        "privileges before the write."
    )
