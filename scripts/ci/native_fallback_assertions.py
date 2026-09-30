"""Read-only native acceptance for the PR's source and repair paths."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tomllib

from pm.downloader import HashError
from pm.operations import build_environment
from pm.wheel_sources import relocate_wheels


def verify_side_environment(root: Path) -> None:
    python = build_environment(
        source=Path.cwd(),
        out=root / "side-source-env",
        cache=root / "side-fresh-uv-cache",
        python=Path(os.environ["HERMES_PYTHON"]),
        no_install_project=True,
        explicit=True,
        timeout=3600,
    )
    subprocess.run(
        [str(python), "-c", "import cryptography,httptools; "
         "from cryptography.hazmat.bindings._rust import openssl; "
         "print('SIDE_ENV_NATIVE_SOURCE_BUILD_OK')"],
        check=True,
    )


def verify_repair_path(root: Path) -> None:
    import tomli_w

    recorded = root / "wheel-path-recorded"
    directory = recorded / "wheels"
    directory.mkdir(parents=True)
    filename = "smoke-1.0-cp314-abi3-win_arm64.whl"
    body = b"reviewed native path bytes"
    sha = hashlib.sha256(body).hexdigest()
    (directory / filename).write_bytes(body)
    (recorded / "pyproject.toml").write_text(
        f'[tool.hermes.win-arm64-wheels]\nsmoke = '
        f'{{ filename = "{filename}", sha256 = "{sha}" }}\n', encoding="utf-8")
    (recorded / "uv.lock").write_text(tomli_w.dumps({
        "version": 1,
        "package": [{"name": "smoke", "version": "1.0",
                     "source": {"registry": str(directory)},
                     "wheels": [{"path": str(directory / filename)}]}],
    }), encoding="utf-8")

    repaired = root / "wheel-path-repaired"
    shutil.copytree(recorded, repaired)
    assert relocate_wheels(repaired, recorded, target="win32-arm64") == ()
    rows = tomllib.loads((repaired / "uv.lock").read_text(encoding="utf-8"))["package"]
    assert rows[0]["source"]["registry"] == str(repaired / "wheels")
    assert rows[0]["wheels"][0]["path"] == str(repaired / "wheels" / filename)

    corrupt = root / "wheel-path-corrupt"
    shutil.copytree(recorded, corrupt)
    (corrupt / "wheels" / filename).write_bytes(b"tampered")
    try:
        relocate_wheels(corrupt, recorded, target="win32-arm64")
    except HashError:
        pass
    else:
        raise AssertionError("corrupt recorded wheel was accepted")
    print("NATIVE_WINDOWS_REPAIR_PATH_OK")


if __name__ == "__main__":
    home = Path(os.environ["RUNNER_TEMP"])
    verify_side_environment(home)
    verify_repair_path(home)
