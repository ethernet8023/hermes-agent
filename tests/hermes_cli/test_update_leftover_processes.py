"""The update's exit lines tell the truth about who still runs old code.

Two halves of doc 3 §D, both about the processes an update leaves behind:

* POSIX: mutating a venv under a live process is SAFE (inode semantics),
  so there is no guard — but the processes keep pre-update behavior
  until restarted. The completion line must SAY so, and must never turn
  into a gate.
* Windows: the venv-holder guard refuses on any unclassified holder. A
  cron job's data-collection script is venv python running a file under
  ~/.hermes/scripts — short-lived and supervisor-less — so the right
  move is a bounded wait, not a dead-end refusal and not a kill.
"""

from __future__ import annotations

from hermes_cli import update_cmd


def _holder(pid: int, name: str, cmdline: str) -> tuple[int, str, str]:
    return (pid, name, cmdline)


class TestThePosixCompletionNotice:
    """_print_posix_stale_process_notice — report-only, POSIX-only."""

    def test_names_the_holders_with_restart_hints(self, monkeypatch, capsys):
        monkeypatch.setattr(update_cmd.os, "name", "posix")
        fake_main = type(
            "M",
            (),
            {
                "_detect_venv_python_processes": staticmethod(
                    lambda **kw: [
                        _holder(101, "python", "python -m hermes_cli.main gateway run"),
                        _holder(102, "python", "python -m hermes_cli.main serve"),
                        _holder(103, "python", "python some_agent_work.py"),
                    ]
                )
            },
        )
        monkeypatch.setattr(update_cmd, "_m", lambda: fake_main)
        update_cmd._print_posix_stale_process_notice()
        out = capsys.readouterr().out
        assert "3 running Hermes process(es) still use pre-update code" in out
        assert "PID 101" in out and "hermes gateway restart" in out
        assert "PID 102" in out and "Desktop app" in out
        assert "PID 103" in out

    def test_detection_is_asked_for_posix_holders(self, monkeypatch, capsys):
        """The notice must opt in to POSIX detection explicitly.

        _detect_venv_python_processes returns [] off-Windows by default —
        the refusal callers rely on that. If the notice forgets
        include_posix=True it prints nothing forever and this whole
        feature is a no-op that LOOKS wired up.
        """
        monkeypatch.setattr(update_cmd.os, "name", "posix")
        seen_kwargs: dict = {}

        def probe(**kwargs):
            seen_kwargs.update(kwargs)
            return []

        fake_main = type(
            "M", (), {"_detect_venv_python_processes": staticmethod(probe)}
        )
        monkeypatch.setattr(update_cmd, "_m", lambda: fake_main)
        update_cmd._print_posix_stale_process_notice()
        assert seen_kwargs.get("include_posix") is True

    def test_silent_when_nothing_runs(self, monkeypatch, capsys):
        monkeypatch.setattr(update_cmd.os, "name", "posix")
        fake_main = type(
            "M",
            (),
            {"_detect_venv_python_processes": staticmethod(lambda **kw: [])},
        )
        monkeypatch.setattr(update_cmd, "_m", lambda: fake_main)
        update_cmd._print_posix_stale_process_notice()
        assert capsys.readouterr().out == ""

    def test_never_breaks_completion(self, monkeypatch, capsys):
        """A notice that raises turns 'update succeeded' into a stack trace."""
        monkeypatch.setattr(update_cmd.os, "name", "posix")

        def explode(**kw):
            raise RuntimeError("psutil went sideways")

        fake_main = type(
            "M", (), {"_detect_venv_python_processes": staticmethod(explode)}
        )
        monkeypatch.setattr(update_cmd, "_m", lambda: fake_main)
        update_cmd._print_posix_stale_process_notice()  # must not raise
        assert capsys.readouterr().out == ""

    def test_windows_prints_nothing_here(self, monkeypatch, capsys):
        """Windows has the up-front guard; a second voice would conflict."""
        monkeypatch.setattr(update_cmd.os, "name", "nt")
        update_cmd._print_posix_stale_process_notice()
        assert capsys.readouterr().out == ""

    def test_completion_line_carries_the_notice(self, monkeypatch, capsys):
        monkeypatch.setattr(update_cmd.os, "name", "posix")
        fake_main = type(
            "M",
            (),
            {
                "_detect_venv_python_processes": staticmethod(
                    lambda **kw: [_holder(7, "python", "gateway run")]
                )
            },
        )
        monkeypatch.setattr(update_cmd, "_m", lambda: fake_main)
        update_cmd._print_update_completion("✓ Update complete!")
        out = capsys.readouterr().out
        assert out.index("Update complete") < out.index("pre-update code")


class TestCronScriptHolderClassification:
    """_cron_script_holder_pids — narrow signature, None on any stranger."""

    def test_recognizes_a_scripts_dir_invocation(self):
        matches = [
            _holder(
                201,
                "python.exe",
                r"C:\hermes\venv\Scripts\python.exe C:\Users\x\.hermes\scripts\watchdog.py",
            )
        ]
        assert update_cmd._cron_script_holder_pids(matches) == [201]

    def test_posix_paths_match_too(self):
        matches = [
            _holder(202, "python", "/opt/hermes/venv/bin/python /home/x/.hermes/scripts/collect.py")
        ]
        assert update_cmd._cron_script_holder_pids(matches) == [202]

    def test_any_non_script_holder_refuses_the_whole_set(self):
        matches = [
            _holder(203, "python", "/opt/hermes/venv/bin/python /home/x/.hermes/scripts/a.py"),
            _holder(204, "python", "python -m hermes_cli.main serve"),
        ]
        assert update_cmd._cron_script_holder_pids(matches) is None

    def test_a_backend_claiming_scripts_in_its_path_refuses(self):
        """hermes_cli.main anywhere in argv disqualifies — a backend whose
        cwd or arg happens to contain /scripts/ must not be waited on."""
        matches = [
            _holder(
                205,
                "python",
                "python -m hermes_cli.main serve --root /home/x/.hermes/scripts/",
            )
        ]
        assert update_cmd._cron_script_holder_pids(matches) is None

    def test_an_operator_repl_refuses(self):
        matches = [_holder(206, "python", "/opt/hermes/venv/bin/python")]
        assert update_cmd._cron_script_holder_pids(matches) is None


class TestTheBoundedWait:
    """_wait_for_cron_script_holders — waits, bounded, tells the truth."""

    def test_returns_true_when_processes_end(self, monkeypatch):
        alive = {301: 2}  # pid -> polls until gone

        def pid_exists(pid):
            alive[pid] -= 1
            return alive[pid] > 0

        fake_psutil = type("P", (), {"pid_exists": staticmethod(pid_exists)})
        monkeypatch.setitem(
            __import__("sys").modules, "psutil", fake_psutil
        )
        monkeypatch.setattr(update_cmd._time, "sleep", lambda s: None)
        assert update_cmd._wait_for_cron_script_holders([301], budget_seconds=10) is True

    def test_returns_false_on_budget_exhaustion(self, monkeypatch):
        fake_psutil = type("P", (), {"pid_exists": staticmethod(lambda pid: True)})
        monkeypatch.setitem(__import__("sys").modules, "psutil", fake_psutil)
        monkeypatch.setattr(update_cmd._time, "sleep", lambda s: None)
        clock = {"now": 0.0}

        def monotonic():
            clock["now"] += 1.0
            return clock["now"]

        monkeypatch.setattr(update_cmd._time, "monotonic", monotonic)
        assert (
            update_cmd._wait_for_cron_script_holders([302], budget_seconds=5) is False
        )

    def test_no_psutil_means_no_wait(self, monkeypatch):
        import sys as _sys

        monkeypatch.setitem(_sys.modules, "psutil", None)
        # import psutil raises TypeError on None-module; the helper must
        # answer False (can't verify → keep the refusal) instead of raising.
        assert update_cmd._wait_for_cron_script_holders([303]) is False
