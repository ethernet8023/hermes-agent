"""Runtime install progress: the slow legs must tick, never hang silently.

The incident: 'Installing runtime…' sat frozen for minutes on a slow
line — the old bespoke downloader streamed no progress, so both the
quickstart hero and the pane's install row showed a dead bar. The engine
now installs through pm, which streams download -> unpack per artifact;
this test pins the router hook that translates that stream into live job
fields. (The pm-side streaming itself is covered in tests/pm/test_pm_core.py.)
"""

from __future__ import annotations

from hermes_cli.web_routers.local_models import _job, _runtime_progress_hook


def test_progress_hook_translates_stages_to_job_fields():
    job = _job("quickstart", "Test Model")
    hook = _runtime_progress_hook(job)

    hook("download", 5 << 20, 100 << 20, "1/2")
    assert job["phase"] == "downloading-runtime"
    assert "1/2" in job["detail"]
    assert job["done_bytes"] == 5 << 20
    assert job["total_bytes"] == 100 << 20

    # Rapid second tick inside the throttle window is dropped...
    hook("download", 6 << 20, 100 << 20, "1/2")
    assert job["done_bytes"] == 5 << 20
    # ...but a terminal tick (done == total) always lands.
    hook("download", 100 << 20, 100 << 20, "1/2")
    assert job["done_bytes"] == 100 << 20

    # pm's unpack stage maps to the unpacking phase. (Terminal tick: the
    # throttle drops non-terminal updates inside its window.)
    hook("unpack", 100, 100, "2/2")
    assert job["phase"] == "unpacking-runtime"
    assert "2/2" in job["detail"]

    # An indeterminate download (no Content-Length -> total 0) must not
    # read as a stuck 0% bar.
    job2 = _job("runtime-install", "x")
    hook2 = _runtime_progress_hook(job2)
    hook2("download", 4 << 20, 0, "")
    assert job2["phase"] == "downloading-runtime"
    assert job2["total_bytes"] is None
    assert job2["done_bytes"] == 4 << 20
