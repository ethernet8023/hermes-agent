"""The lockfile (versions + hashes, machine-written) and the installed-state
file (what is actually on this machine).

lock.json:  {"schema": 1, "packages": {name: {"version": ..., "artifacts": {target: {"url": ..., "sha256": ...}}}}}
facts.json: {"schema": 1, "packages": {name: {"entry": ..., "version": ..., "env": ..., "stamp": ...}}}

lock.json artifacts carry the RESOLVED url beside the hash: the lockfile is
the complete machine interface (nix reads it as pure data), and the python
url templates are consulted only at `pm lock --bump` time. The "any" target
key covers target-independent artifacts (npm's tarball).

facts.json env values hold {{store}} templates so a CI-built file adopts onto
any machine by substitution.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

SCHEMA = 1
STORE_TOKEN = "{{store}}"


def _read(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return {"schema": SCHEMA, "packages": {}}
    try:
        data = json.loads(text)
        if data.get("schema") == SCHEMA and isinstance(data.get("packages"), dict):
            return data
    except ValueError:
        pass
    if text.strip():
        # An unparsable-but-nonempty state file is evidence, not garbage:
        # the next _write would silently discard every installed-state
        # record. Keep the bytes for post-mortem.
        try:
            path.with_suffix(".corrupt").write_text(text, encoding="utf-8")
        except OSError:
            pass
    return {"schema": SCHEMA, "packages": {}}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


class Lockfile:
    """Read side of lock.json. Written only by `pm lock --bump` (cli)."""

    def __init__(self, path: Path):
        self.path = path
        self._packages = _read(path)["packages"]

    def version(self, name: str) -> str | None:
        return (self._packages.get(name) or {}).get("version")

    def artifact(self, name: str, target: str) -> dict | None:
        artifacts = (self._packages.get(name) or {}).get("artifacts") or {}
        return artifacts.get(target) or artifacts.get("any")

    def sha256(self, name: str, target: str) -> str | None:
        return (self.artifact(name, target) or {}).get("sha256")

    def url(self, name: str, target: str) -> str | None:
        return (self.artifact(name, target) or {}).get("url")

    def names(self) -> list[str]:
        return sorted(self._packages)

    def set_pin(self, name: str, version: str, artifacts: dict[str, dict]) -> None:
        self._packages[name] = {"version": version, "artifacts": artifacts}

    def save(self) -> None:
        _write(self.path, {"schema": SCHEMA, "packages": self._packages})


class Facts:
    """The installed-state file. Written only by pm."""

    def __init__(self, path: Path):
        self.path = path
        self._packages = _read(path)["packages"]

    def reload(self) -> None:
        self._packages = _read(self.path)["packages"]

    def get(self, name: str) -> dict | None:
        return self._packages.get(name)

    def installed(self, name: str, expected_version: str | None, store_root: Path) -> bool:
        fact = self._packages.get(name)
        if not fact:
            return False
        if expected_version is not None and fact.get("version") != expected_version:
            return False
        return (store_root / fact["entry"]).exists()

    def env_for(self, name: str, store_root: Path) -> dict:
        fact = self._packages.get(name) or {}
        return _resolve(fact.get("env", {}), store_root)

    def _merge_and_write(self, name: str, fact: dict) -> None:
        """Read-modify-write against disk so concurrent installs of
        different packages never clobber each other."""
        on_disk = _read(self.path)["packages"]
        for key, value in self._packages.items():
            on_disk.setdefault(key, value)
        self._packages = on_disk
        self._packages[name] = fact
        _write(self.path, {"schema": SCHEMA, "packages": self._packages})

    def record(
        self, name: str, version: str, entry: str, env: dict, store_root: Path
    ) -> None:
        self._merge_and_write(
            name,
            {"entry": entry, "version": version, "env": _templatize(env, store_root)},
        )

    def record_state(self, name: str, stamp: str, extras: list[str]) -> None:
        """State packages (the venv) have a stamp and extras, no entry."""
        self._merge_and_write(name, {"stamp": stamp, "extras": extras})

    def entries_in_use(self) -> set[str]:
        return {f["entry"] for f in self._packages.values() if "entry" in f}

    def drop(self, name: str) -> bool:
        """Forget one package. Used when a leftover cache entry must not
        ship in a sealed payload."""
        on_disk = _read(self.path)["packages"]
        if name not in on_disk and name not in self._packages:
            return False
        on_disk.pop(name, None)
        self._packages = on_disk
        _write(self.path, {"schema": SCHEMA, "packages": self._packages})
        return True


def _templatize(env: dict, store_root: Path) -> dict:
    root = str(store_root)
    out = {}
    for key, value in env.items():
        if isinstance(value, list):
            out[key] = [str(v).replace(root, STORE_TOKEN) for v in value]
        else:
            out[key] = str(value).replace(root, STORE_TOKEN)
    return out


def _resolve(env: dict, store_root: Path) -> dict:
    root = str(store_root)
    out = {}
    for key, value in env.items():
        if isinstance(value, list):
            out[key] = [str(v).replace(STORE_TOKEN, root) for v in value]
        else:
            out[key] = str(value).replace(STORE_TOKEN, root)
    return out
