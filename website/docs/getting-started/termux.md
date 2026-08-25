---
sidebar_position: 3
title: "Android / Termux"
description: "Hermes Agent no longer supports Android or Termux"
---

# Android and Termux are not supported

Hermes Agent removed its Android and Termux support. The installer no
longer detects Termux, and the `termux` and `termux-all` dependency
profiles no longer exist.

If you run an older release on a phone, that install keeps working. It
receives no fixes. `hermes update` pulls a tree with no Termux lane, so
the update fails or produces an install that does not start.

## Why

Termux was a second install lane with its own rules. It used pip and a
stdlib venv while every other platform used uv. It needed a curated
extras profile, because several dependencies publish no Android wheel.
It patched the psutil source before the build. It also carried its own
terminal UI mode, its own startup paths, and its own service messages.

No test covered that lane. Every change to an install path had to be
reasoned about twice, and the Termux half was never executed. A lane
that nothing tests is a lane that is already broken.

## Supported platforms

| Platform | How to install |
|---|---|
| Linux (x86_64, aarch64) | `curl -fsSL https://hermes-agent.nousresearch.com/install.sh \| bash` |
| macOS (Apple Silicon) | Hermes Desktop, or the same `install.sh` |
| Windows (native) | `iex (irm https://hermes-agent.nousresearch.com/install.ps1)` |
| WSL2 | The Linux command above |

See [Platform support](./platform-support.md) for the full table.

## Run Hermes from a phone

Hermes still reaches you on a phone. Run the agent on a machine that
Hermes supports, then talk to it from the phone:

- Install the gateway on a Linux host or a server. Then use Telegram,
  Discord, Slack, WhatsApp, or SMS. See
  [Gateway](../user-guide/gateway.md).
- Use the web dashboard from the phone browser. See
  [Web dashboard](../user-guide/web-dashboard.md).
- Use `ssh` from a terminal app and run the CLI on the host.

This is a better setup than the phone install was. The agent keeps
running when the phone screen turns off, and Android cannot stop it.

## A future Android port

An Android package can return. It must be a real package with its own
tests, not a second lane inside the main installer. The parts it needs
are recorded in `docs/termux-removal-notes.md` in the repository.
