---
sidebar_position: 8
title: "Windows Sandbox (MXC)"
description: "Run every agent command inside a kernel-enforced Windows process container, with a folder policy you control"
---

# Windows Sandbox (MXC)

On Windows, Hermes can run every command and file operation the agent performs inside a fresh
Microsoft MXC process container. The container is enforced by the Windows kernel: the process
inside it can reach the session's workspace folder and the folders you have granted, and nothing
else. A write to your Documents folder, a read of Hermes's own credentials, or a network request
is refused by the operating system before it happens, no matter what the agent tries. Everything
else about Hermes stays the same. The agent, its model (including local inference on your GPU),
and the desktop app all run normally on the host; only the agent's actions are boxed.

This is a different shape from the Docker backend. Docker gives the agent a separate Linux
filesystem; MXC keeps the agent on your real Windows filesystem with your real tools, and draws
the boundary with permissions instead of a virtual machine. Starting a container costs a fraction
of a second, so Hermes starts a new one for every single command.

## Requirements

- Windows 11 with the MXC process-container support (Insider builds from 26300 onward at the time
  of writing). Hermes checks this for you and tells you plainly when a machine cannot run it.
- The MXC kit, specifically `wxc-exec.exe`. Hermes looks in `C:\mxc-kit\bin` and `C:\mxc\bin` and
  on `PATH`; if it lives elsewhere, set `terminal.mxc_wxc_exec_path`.
- One elevated command, run once per machine, so containers can traverse the drive root:
  `wxc-host-prep.exe prepare-system-drive` (from the same kit).
- A POSIX shell for the container. Git for Windows' bash cannot start inside an AppContainer, so
  Hermes uses a pinned, checksum-verified `busybox-w32` build and downloads it into
  `%LOCALAPPDATA%\hermes\bin` the first time you turn the sandbox on. To use your own copy, set
  `terminal.mxc_shell_path`.

## Turning it on

In Hermes Desktop, open **Settings → Safety** and find **Windows sandbox**. The panel shows
whether this machine can run MXC and, if not, why. Flip **Sandbox agent actions** on. That sets
`terminal.backend` to `mxc`, provisions the shell if needed, and takes effect on the agent's next
command in every session; nothing needs restarting. A conversation that is already under way is
told about the change on its next command: the note carries the sandbox rules, the POSIX shell,
and how to handle a refusal, and the same happens in reverse when you turn the sandbox off.

From the command line the equivalent is:

```bash
hermes config set terminal.backend mxc
```

`hermes doctor` and the terminal-backend picker report the same availability check, using the
same words, so you never have to guess why an option is greyed out.

## The policy

The policy is small on purpose, and the panel shows all of it:

- **Workspace.** The folder a session works in is always readable and writable. In the desktop
  that is the session's project folder; in the CLI it is the folder you launched `hermes` from.
  A grant covers everything beneath it, so Hermes never uses your home folder, a drive root, a
  folder that contains its own data directory, or a folder that contains its own program files as
  a workspace: a desktop session with no project folder works in `C:\Users\<you>\Hermes` instead
  (created on first use), and picking one of those folders explicitly is reported as an error.
  The program-files rule means a sandboxed agent cannot rewrite Hermes itself; to work on the
  Hermes source under the sandbox, use a separate clone. In the CLI, launch `hermes` from a
  project folder. Each conversation has its own workspace; the Sandbox panel shows the policy
  that applies to all of them, and the composer shows the folder of the conversation in front
  of you.
- **Additional folders**, each read-only or read & write. These are `terminal.mxc_readonly_paths`
  and `terminal.mxc_readwrite_paths` in `config.yaml`.
- **Network**, off by default (`terminal.mxc_network`). Off means the agent is offline: sandboxed
  commands cannot reach the internet or services on your own machine, and Hermes's own web search,
  page-fetch and browser tools are refused with that reason until you turn it on (they run in the
  Hermes process, outside the container, so the switch has to cover them too or it would mean
  little). Local inference is unaffected because the model runs outside the sandbox.

A few read-only grants are added automatically so the agent's tools work: the Hermes install and
its Python, the bundled Node and Git, the sandbox shell, and the desktop's composer staging
folders (the images and text you paste or attach), so a pasted screenshot can be analysed without
a grant. Hermes's data directory, with your configuration and credentials, is never granted, and
neither is the rest of the desktop's user-data folder.

Edits take effect on the agent's next command. Hermes reads the policy fresh for every container
it starts, which is what makes the grant-and-retry flow below possible without a restart.

## When something is refused

A refused command comes back with the operating system's own error and a short note from Hermes
that names what was refused and what is currently allowed, for example:

```
sh: can't create C:/Users/you/Documents/report.txt: Permission denied

[Sandbox] Windows MXC denied access outside the sandbox policy:
  denied: C:\Users\you\Documents\report.txt
  read/write: C:\Demo
  read-only: (none)
  network: off
The user controls this policy (Hermes desktop: Settings > Safety > Sandbox). If the task needs
that location, stop and ask the user to grant access; a grant applies to your next command.
Do not try to work around the sandbox.
```

The agent is instructed to stop and ask rather than route around the sandbox. In the desktop the
tool card is marked **Blocked by sandbox policy** and offers **Allow reading** and
**Allow read & write** for the folder in question. Granting writes the folder into the policy and
drafts a short "please try again" message into the composer, so one Enter resumes the task with
the new permission in force.

## Git inside the sandbox

Git is the one common tool that fails inside a container when the workspace sits under a profile
folder. This section explains why, what to do about it today, and what the permanent fix looks
like; it is written for both the person hitting the error and whoever picks the work back up.

**The symptom.** Any `git` command in a workspace such as `C:\Users\you\Hermes\project` fails with
`fatal: Unable to read current working directory: Permission denied`, and the `[Sandbox]` note on
the result names the workspace's parent folders as what was denied. Python, PowerShell, `cmd`,
Node and the Hermes file tools work in the same folder. A workspace directly under the drive root
(`C:\Demo`) works with git too.

**Why git is different.** Git for Windows does not trust the process's current directory string;
it canonicalizes the path by reading the attributes of every folder from the drive root down to
the workspace. An AppContainer process may open a folder only if the folder's DACL grants the
right both to the user's ordinary identity and to a SID the container carries, so `Everyone` or
`Users` entries do not help. `wxc-host-prep prepare-system-drive` handles the drive root (it adds
object-only `Rc,S,REA,RA` entries for `ALL APPLICATION PACKAGES` and `ALL RESTRICTED APPLICATION
PACKAGES` on `C:\`, deliberately without `RD` so containers cannot list the root). Nothing does
the same for `C:\Users` or for the user's profile folder, and the traverse-only capability entries
Windows places there are one right short: traversing a folder is not reading its attributes.
Inheriting the drive-root grant downward is not an option, because an inheritable entry on `C:\`
would make every folder and file on the disk visible to every container.

**What to do today.** The missing rights are needed on each ancestor of the workspace, and only
`C:\Users` is administrator-owned, so it is one elevated command per machine:

```
icacls "C:\Users" /grant "*S-1-15-2-1:(RA,REA,RC,S)" "*S-1-15-2-2:(RA,REA,RC,S)"
```

This adds a non-inherited, attributes-only entry to the `C:\Users` folder object; nothing beneath
it changes, other users' profiles stay closed to containers, and `icacls "C:\Users" /remove
"*S-1-15-2-1" "*S-1-15-2-2"` undoes it. Then give the same rights to your own profile folder and
to the folders between it and the workspace (`C:\Users\you`, `C:\Users\you\Hermes`); you own
these, so no elevation is needed. On the current Windows build `icacls` against the profile root
itself hangs without applying anything; the .NET path works instead, from a normal PowerShell:

```powershell
$dir = Get-Item -LiteralPath "C:\Users\you"
$acl = $dir.GetAccessControl("Access")
foreach ($sid in "S-1-15-2-1", "S-1-15-2-2") {
  $id = New-Object System.Security.Principal.SecurityIdentifier($sid)
  $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
    $id, "ReadAttributes, ReadExtendedAttributes, ReadPermissions, Synchronize", "None", "None", "Allow")))
}
$dir.SetAccessControl($acl)
```

`Set-Acl` fails here with a `SeSecurityPrivilege` error because it also tries to write the audit
list; `SetAccessControl` on the `DirectoryInfo` writes only the DACL. The attributes-only right
set mirrors what `prepare-system-drive` grants on `C:\`, which git traverses without trouble, so
it is the expected minimum; the `C:\Users` half has not been exercised end to end yet because it
needs elevation, so verify with `git status` in a profile-folder workspace after applying it.
Adding `ListDirectory` (`RD`) is the fallback if attributes alone turn out not to suffice; it
also lets containers list the folder's child names, which on `C:\Users` means the account names
on the machine.

**Why there is no button for this.** An earlier build showed a "Prepare workspace for git" card
in the Sandbox panel with the command to copy. It was removed: the in-place half hung on the
profile folder, the elevated half is a machine setup step rather than a per-workspace one, and a
command nobody recognizes is not a control. The status route still reports the ancestors
(`workspace_ancestors` with `missing`, `needs_admin` and `admin_command`) and
`tools/environments/mxc_host.py` still has `ancestor_readiness` and `prepare_ancestors`, so a
future control can be built without re-deriving any of this.

**The permanent fix belongs in the kit.** `wxc-exec` knows every granted path when it creates a
container and could ensure attribute rights on the ancestors itself; alternatively `wxc-host-prep`
could extend what it does for `C:\` to `C:\Users` and the invoking user's profile root. Either
removes the administrator step for everyone. If Hermes ends up owning it instead, the shape that
fits is one elevated "finish setup" step that runs `prepare-system-drive` and the `C:\Users` grant
together behind a single UAC prompt, plus silent preparation of user-owned ancestors when a
workspace is created, using the .NET path rather than `icacls`.

## Limitations

- Windows only, and only on builds with the MXC process-container support.
- Each command runs in its own container, so a foreground command cannot leave a server running
  after it exits. Use `terminal(background=true)` for long-lived processes; Hermes keeps that
  container alive for the life of the process.
- The `execute_code` kernel does not persist between calls under this backend; commands and the
  file tools are the supported path.
- Browser automation and desktop control run on the host, outside the sandbox. The network
  switch refuses the web and browser tools when it is off; turn the `computer_use` toolset
  off yourself when the point is containment.
- The switch is binary on current Windows builds. Per-domain allow or deny lists for sandboxed
  commands need a host-side egress proxy the container can reach, and the container cannot reach
  the host on these builds; that arrives with the next MXC contract.
- Messaging gateways must set `terminal.cwd` to a project folder; the sandbox will not accept the
  gateway's home directory as a workspace.

## Configuration reference

```yaml
terminal:
  backend: mxc
  mxc_wxc_exec_path: ""         # Path to wxc-exec.exe; empty = C:\mxc-kit\bin, C:\mxc\bin, PATH
  mxc_shell_path: ""            # POSIX shell for the container; empty = managed busybox-w32
  mxc_readwrite_paths: []       # Extra folders the agent may read and write
  mxc_readonly_paths: []        # Extra folders the agent may read
  mxc_network: false            # Allow outbound network from sandboxed commands
  mxc_debug: false              # Log each container's launcher configuration
```

Every key is also bridged to a `TERMINAL_MXC_*` environment variable for processes started with
only the environment bridge, the same way the other terminal keys are.

## Preparing a demonstration machine

For a machine that will show the sandbox to an audience, the following order avoids surprises:

1. Confirm the Windows build supports process containers: `wxc-exec.exe --probe` should report a
   `base-container` tier with no warnings.
2. Run `wxc-host-prep.exe prepare-system-drive` once from an elevated prompt. If the demonstration
   will use git in a workspace under a profile folder, also run the `C:\Users` grant from "Git
   inside the sandbox" above, and prepare your own profile folder as described there.
3. Install Hermes Desktop and a local model, and confirm a normal conversation works.
4. Create the demonstration workspace directly under the drive root, for example `C:\Demo`, and
   open it as the session's project folder; git works there with no further preparation.
5. Turn on the sandbox in **Settings → Safety** while online, so the shell downloads.
6. Run one task that stays inside the workspace and one that reaches outside it, and grant the
   folder from the tool card, so every path has been exercised before the audience arrives.
