# Termux removal notes

Hermes Agent no longer supports Termux (Android). This document records
what the removal took out, what it kept, and what a future Termux port
must rebuild.

## Why

Termux was a second install lane with its own rules. It used pip and a
stdlib venv while every other platform used uv. It needed a curated
`termux-all` extras profile because several dependencies have no Android
wheel. It patched the psutil source before the build. It carried its own
TUI mode, its own CLI fast paths, and its own gateway and doctor
messages.

Nothing in CI tested that lane. Every install-path change had to be
reasoned about twice, and the Termux half was never executed. The lane
grew a special case in each subsystem it touched.

## What the removal took out

### Install

- The Termux lane in `scripts/install.sh`: the `DISTRO=termux`
  detection, the stdlib venv and pip path, the `pkg install` calls, the
  psutil prebuild, the `.[termux-all]` fallback chain, the
  `$PREFIX/bin` command link, and the gateway and PATH special cases.
- The Termux lane in `setup-hermes.sh`.
- `constraints-termux.txt`.
- The `termux` and `termux-all` extras in `pyproject.toml`.
- `scripts/install_psutil_android.py`.
- `_ensure_uv_for_termux`, `_install_psutil_android_compat`, and
  `_is_android_python` in the update flow.

### Runtime

- `hermes_constants.is_termux`.
- The Termux CLI and TUI fast-launch paths, and the deferred-startup
  contract they set up (`HERMES_DEFER_AGENT_STARTUP`,
  `HERMES_FAST_STARTUP_BANNER`).
- The Termux bundled-skill sync stamp.
- The Termux npm workspace scoping for the TUI and the web UI, and the
  mtime rebuild check.
- `ui-tui/src/lib/termux.ts` and the `TERMUX_TUI_MODE` flag.
- The `TermuxAudioRecorder` voice backend and the Termux:API probes.
- The Termux browser carve-out that demanded a real `agent-browser`
  install instead of an `npx` shim.
- The Termux arms in the gateway service commands, the gateway status
  view, `hermes status`, `hermes doctor`, and the uninstaller.
- The Termux widening in skill platform gating. A skill tagged
  `platforms: [linux]` now matches `sys.platform == "linux"` only.

## What the removal kept

Three behaviours stay. Each one is correct for reasons that do not
depend on Termux.

1. **The nemo-relay environment markers in `pyproject.toml`.** The
   markers exclude Android from the wheel-only dependency. Hermes does
   not support Android, but a base install must not fail resolution
   there. Without the markers, uv reports an unsatisfiable requirement
   instead of falling back to the no-op Relay host.

2. **`LocalEnvironment.get_temp_dir` prefers `TMPDIR`.** The rule is
   "a host can have no `/tmp`". That is true beyond Android.

3. **The Android to Linux mapping in `tools/tirith_security.py`.**
   Android is ABI-compatible with Linux, so the Linux binaries are the
   correct artifacts. The mapping is a fact about the ABI, not a Termux
   feature.

`hermes_cli/psutil_android.py` also stays, but for a different reason.
The frozen old-updater surface
(`tests/compat/old_updater_surface.json`) lists `PSUTIL_URL` and
`prepare_patched_psutil_sdist` as bare imports. A released updater loads
them from the new tree during an update, so a delete bricks that update
on a half-new tree. Nothing in the current tree calls them. Delete the
module when the frozen surface is next regenerated.

## What a future port must rebuild

A Termux package is possible again. It must not return as a second
install lane inside this repository. These are the parts that a port
needs. Tracking issue: ethernet8023/hermes-agent#7 (fork-local until
this branch merges upstream).

| Part | What it needs |
|---|---|
| Dependency install | An Android wheel source, or an extras profile that holds only packages with Android wheels. |
| psutil | Upstream psutil must accept `sys.platform == "android"`. Track https://github.com/giampaolo/psutil/pull/2762. |
| uv | uv publishes no bionic build. The port must supply uv from the Termux package repository. |
| Voice capture | A microphone backend. The removed one shelled out to `termux-microphone-record` from Termux:API. |
| Browser tools | Chromium for Android, or a remote browser backend. |
| Gateway | Android stops background processes. The port needs a foreground service or a scheduler. |

## Verification

The removal is complete when this command reports no results:

```
grep -ri termux . --exclude-dir=.git --exclude-dir=node_modules
```

Two directories are expected to keep results until the port exists: the
website archive of the old Termux guide, and the frozen updater surface
described above.
