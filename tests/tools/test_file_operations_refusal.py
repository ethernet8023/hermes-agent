"""Execute generated read scripts, including failed producers in pipelines."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.file_operations import ShellFileOperations


class ScriptEnvironment:
    windows_path_form = "native" if os.name == "nt" else "posix"

    def __init__(self, cwd, prefix=""):
        self.cwd = str(cwd)
        self.prefix = prefix
        if os.name == "nt":
            shell = Path(os.environ["LOCALAPPDATA"]) / "hermes" / "bin" / "busybox-sh.exe"
            if not shell.is_file():
                pytest.skip("installed busybox-w32 required")
            self.argv = [str(shell), "sh", "-c"]
        else:
            self.argv = [shutil.which("bash") or "/bin/sh", "-c"]

    def execute(self, command, **kwargs):
        result = subprocess.run([*self.argv, self.prefix + command], cwd=self.cwd,
                                capture_output=True, text=True, timeout=10)
        return {"returncode": result.returncode, "output": result.stdout + result.stderr}


@pytest.mark.parametrize("sequential", [False, True])
def test_reader_failure_is_not_hidden_by_successful_clamp(tmp_path, sequential):
    target = tmp_path / "note.txt"
    target.write_text("actual fixture\n", encoding="utf-8")
    # A failed reader after a successful stat/sample, not canned compound output:
    # the production-generated shell pipeline must carry this producer's failure.
    env = ScriptEnvironment(tmp_path, "sed() { printf 'sed: Permission denied\\n' >&2; return 1; }; ")
    ops = ShellFileOperations(env)
    result = (ops._read_file_sequential(str(target), 1, 2000) if sequential
              else ops.read_file(str(target)))
    assert result.error and "refused" in result.error, result.to_dict()
    assert not result.content and "empty" not in (result.hint or "")


@pytest.mark.parametrize("stage", ["wc", "tail"])
@pytest.mark.parametrize("sequential", [False, True])
def test_metadata_reader_failure_is_not_a_successful_read(tmp_path, stage, sequential):
    target = tmp_path / "note.txt"
    target.write_text("actual fixture\n", encoding="utf-8")
    prefix = ("wc() { if [ \"$1\" = '-l' ]; then printf 'wc: Permission denied\\n' >&2; return 1; "
              "else command wc \"$@\"; fi; }; " if stage == "wc" else
              "tail() { printf 'tail: Permission denied\\n' >&2; return 1; }; ")
    ops = ShellFileOperations(ScriptEnvironment(tmp_path, prefix))
    result = (ops._read_file_sequential(str(target), 1, 2000) if sequential
              else ops.read_file(str(target)))
    assert result.error and "refused" in result.error, result.to_dict()
    assert not result.content


def test_visible_but_unreadable_size_is_not_an_empty_file(tmp_path):
    target = tmp_path / "note.txt"
    target.write_text("actual fixture\n", encoding="utf-8")
    env = ScriptEnvironment(tmp_path, "wc() { printf 'wc: Permission denied\\n' >&2; return 1; }; ")
    result = ShellFileOperations(env).read_file(str(target))
    assert result.error and "refused" in result.error, result.to_dict()
    assert not result.content and "empty" not in (result.hint or "")
