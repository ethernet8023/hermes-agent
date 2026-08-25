---
sidebar_position: 3
title: "Android / Termux"
description: "Hermes Agent 不再支持 Android 和 Termux"
---

# 不再支持 Android 和 Termux

Hermes Agent 已移除 Android 和 Termux 支持。安装程序不再检测 Termux，
`termux` 和 `termux-all` 依赖配置也已删除。

如果你的手机上运行着旧版本，该安装仍可继续使用，但不会再收到任何修复。
`hermes update` 会拉取不含 Termux 通道的代码树，因此更新会失败，
或者产生一个无法启动的安装。

## 原因

Termux 是一条独立的安装通道，规则也自成一套。在其他平台都使用 uv 时，
它使用 pip 和标准库 venv。由于多个依赖没有发布 Android wheel，
它需要一份精选的扩展配置。它在构建前还要给 psutil 源码打补丁。
此外，它还带有自己的终端 UI 模式、自己的启动路径和自己的服务提示信息。

没有任何测试覆盖这条通道。每次修改安装路径都要做两遍推演，
而 Termux 这一半从未真正执行过。没有测试覆盖的通道，等同于已经损坏。

## 支持的平台

| 平台 | 安装方式 |
|---|---|
| Linux (x86_64, aarch64) | `curl -fsSL https://hermes-agent.nousresearch.com/install.sh \| bash` |
| macOS (Apple Silicon) | Hermes Desktop，或同样的 `install.sh` |
| Windows (原生) | `iex (irm https://hermes-agent.nousresearch.com/install.ps1)` |
| WSL2 | 使用上面的 Linux 命令 |

完整列表请参考[平台支持](./platform-support.md)。

## 在手机上使用 Hermes

Hermes 仍然可以在手机上使用。把 agent 运行在受支持的机器上，
然后从手机与它通信：

- 在 Linux 主机或服务器上安装 gateway，然后使用 Telegram、Discord、
  Slack、WhatsApp 或短信。请参考 [Gateway](../user-guide/gateway.md)。
- 用手机浏览器访问 Web 仪表盘。请参考
  [Web 仪表盘](../user-guide/web-dashboard.md)。
- 从终端 App 使用 `ssh`，在主机上运行 CLI。

这比在手机上直接安装更好：手机息屏后 agent 仍在运行，
Android 也无法把它停掉。

## 未来的 Android 移植

Android 包可以回归，但它必须是一个带有独立测试的真正的包，
而不是主安装程序里的第二条通道。所需的部分记录在仓库的
`docs/termux-removal-notes.md` 中。
