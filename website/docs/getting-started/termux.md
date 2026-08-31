---
sidebar_position: 3
title: "Android / Termux"
description: "Install Hermes Agent on Android via Termux using our signed APT package"
---

# Hermes on Android with Termux

Hermes Agent runs on Android through [Termux](https://termux.dev/) as a native
APT package, served from our own signed package repository.

:::info Supported configuration
Termux on **aarch64 (arm64-v8a)** devices is the supported configuration. Other
architectures are not packaged yet.
:::

## How the install works

The package is fully self-contained: it ships its own Python and its own Node,
and the entire dependency set is pre-validated at build time. Nothing is
compiled or downloaded on the device during install, and it does not depend on
Termux's `python` or `nodejs` packages.

The package is distributed through our signed APT repository (hosted on our
release storage). Installing means:

1. Adding the repository's `sources.list` entry for the channel you want
   (see [Channels](#channels) below).
2. Importing the repository's GPG public key so `pkg` can verify the packages
   (the key is published alongside the repository - fetch it from the release
   page and add it to `apt`'s trusted keys).
3. Installing the package:

   ```bash
   pkg install hermes-agent
   ```

The `hermes` command is a symlink in `$PREFIX/bin`, so it is available from any
Termux shell immediately after install.

## What gets installed

Everything lives in a single dpkg-owned directory, with one symlink outside
it:

| What                        | Where                          |
| --------------------------- | ------------------------------ |
| The app (Python, Node, venv) | `$PREFIX/lib/hermes-agent/`   |
| The `hermes` launcher       | `$PREFIX/bin/hermes` (symlink) |
| Your data                   | `~/.hermes/`                   |

`$PREFIX` is Termux's install prefix (typically `/data/data/com.termux/files/usr`).

Your data - configuration, sessions, skills, memories - lives in `~/.hermes/`
and is separate from the package.

## Channels

Two channels are published, mirroring the desktop release channels:

- **stable** - tagged releases.
- **nightly** - built from the latest development state.

Point the repository `sources.list` entry at the channel you want
(`hermes-stable` or `hermes-nightly` distribution). Version strings for
nightlies sort below stable, so switching back to stable always upgrades.

## Updating

Updates come through the package manager:

```bash
pkg upgrade hermes-agent
```

`hermes update` on a Termux install refuses to self-update and prints exactly
this command instead - the package manager owns the install.

## Running the gateway

Termux has no service manager, so the messaging gateway runs as a foreground
process in a Termux session:

```bash
hermes gateway run
```

Or in the background with `nohup`:

```bash
hermes gateway stop
nohup hermes gateway run >> ~/.hermes/logs/gateway.log 2>&1 &
```

:::warning Android phantom process killer
Android may suspend or kill background processes, including Termux jobs. To
keep a background gateway alive, exclude Termux from battery optimization
(Android Settings -> Apps -> Termux -> Battery -> Unrestricted) and keep the
Termux session alive (a wake lock via `termux-wake-lock` helps). Treat
background gateway persistence on a phone as best-effort.
:::

## Uninstalling

```bash
pkg uninstall hermes-agent
```

This removes the package and the `$PREFIX/bin/hermes` symlink. Your data in
`~/.hermes/` is left untouched.

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `Unable to locate package hermes-agent` | The repository `sources.list` entry is missing or the channel name is wrong - re-check it, then run `pkg update`. |
| Signature / GPG errors during `pkg update` | The repository public key isn't imported (or is stale) - re-fetch the current key from the release page. |
| `hermes: command not found` | Reinstall the package, or check that `$PREFIX/bin` is on your `PATH`. |
| Gateway dies when the screen turns off | See the [phantom process killer note](#running-the-gateway) - battery-optimization exemption plus `termux-wake-lock`. |

For general diagnostics, run `hermes doctor`.
