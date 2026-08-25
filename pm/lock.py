"""The lockfile (versions + hashes, machine-written) and the installed-state
file (what is actually on this machine).

lock.json:  {"schema": 1, "packages": {name: {"version": ..., "sha256": {target: hash}}}}
facts.json: {"schema": 1, "packages": {name: {"entry": ..., "version": ..., "env": ..., "stamp": ...}}}

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
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if data.get("schema") == SCHEMA and isinstance(data.get("packages"), dict):
            return data
    except (OSError, ValueError):
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

    def sha256(self, name: str, target: str) -> str | None:
        hashes = (self._packages.get(name) or {}).get("sha256") or {}
        return hashes.get(target) or hashes.get("any")

    def names(self) -> list[str]:
        return sorted(self._packages)

    def set_pin(self, name: str, version: str, sha256: dict[str, str]) -> None:
        self._packages[name] = {"version": version, "sha256": sha256}

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
