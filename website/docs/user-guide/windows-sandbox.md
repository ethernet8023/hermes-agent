---
sidebar_position: 8
title: "Windows Sandbox (MXC)"
description: "Run supported agent actions inside Windows process containers, with explicit host-service boundaries"
---

# Windows Sandbox (MXC)

On supported Windows builds, Hermes runs terminal commands and file tools inside Microsoft MXC
process containers. Windows enforces their filesystem and network policy. Each environment has
its own workspace authority and private scratch directory; a command cannot acquire another
conversation's workspace merely by changing its working directory.

Strict mode admits only reviewed action paths. Browser control, desktop automation, MCP,
connectors and other uncontained tool services are refused rather than executed on the host.
The tool list may still show them in an existing conversation; the check happens when a call
is attempted, so no new conversation is needed.

Hermes itself, the desktop, inference (including local inference), and fixed-purpose memory,
skills and session-history services remain trusted host software. This is not a virtual machine
around the whole application. Installed extensions and provider integrations are part of that
trusted host software; the sandbox does not defend against a malicious Hermes installation.

This is a different shape from the Docker backend. Docker gives the agent a separate Linux
filesystem; MXC keeps the agent on your real Windows filesystem with your real tools, and draws
the boundary with permissions instead of a virtual machine. Starting a container costs a fraction
of a second, so Hermes starts a new one for every single command.

## Requirements

- Windows with MXC's `base-container` tier and the required UI restrictions. Hermes probes
  capabilities rather than relying on a version label. Weaker AppContainer/DACL fallback is
  not accepted as equivalent, and requests explicitly disallow host-DACL mutation.
- The MXC kit and the sandbox shell ship with Hermes. Both are pinned pm tools, so a sealed
  install carries them and a source install downloads them the first time you turn the sandbox
  on. There is no path to set and no separate kit install.
- One elevated command, run once per machine, so containers can traverse the drive root:
  `wxc-host-prep.exe prepare-system-drive`, from the kit in the pm store.
- Windows ARM64. The kit exists for that architecture only; other platforms show the sandbox as
  unavailable.

## Turning it on

In Hermes Desktop, open **Settings → Safety** and find **Windows sandbox**. The panel shows
whether this machine can run MXC and, if not, why. Flip **Sandbox agent actions** on. That sets
`terminal.backend` to `mxc`, provisions the kit and shell if this install does not ship them, and
takes effect on the agent's next command in every session; nothing needs restarting. A download
that fails leaves the switch off and shows the reason. A conversation that is already under way is
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
- **Network**, off by default (`terminal.mxc_network`). It controls outbound access from
  sandboxed commands and the reviewed web-search/page-fetch services. Pasted URL expansion
  follows the same policy. It does not disconnect Hermes's model provider or the desktop.
  Local inference remains on the host.

Enabling network access does not authorize host execution. Browser Use, raw CDP, desktop
control, MCP, connectors, media-generation tools and unreviewed plugin tools remain refused.
`execute_code` is also refused until a compatible sandboxed implementation is available;
Python programs can instead be run through the sandboxed terminal. Skill text remains readable,
but new inline shell snippets and scheduled host-script launches are refused. Automatic
messaging delivery of model-selected local file paths is refused rather than reading those
files on the host; generated files remain in their authorized workspace. Model-selected remote
images follow the network policy. In the desktop, assistant-origin media, HTML and widgets stay
inert while MXC is enabled or their owning profile's policy has not been confirmed. User-uploaded
attachments remain usable. SVG rasterization is refused under MXC; use a raster screenshot
instead of invoking an uncontained converter.

The model cannot widen its workspace by calling the project tool. Choose a project yourself or
authorize an additional folder through the sandbox controls. An explicitly empty grant list
means no additional grants; it never restores old environment-variable grants. Invalid policy
is an error, not permission to fall back to the local backend.

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

The agent is instructed to stop and ask rather than route around the sandbox. The collapsed
tool card keeps the policy reason; the full grant callout stays behind its disclosure. Before
**Allow reading** or **Allow read & write** is enabled, the callout resolves and displays the
actual folder being authorized. A folder grant covers its descendants, not just the file that
triggered the suggestion. Protected code and credential roots cannot be added as user grants.

A command's error text supplies a suggested path, not authenticated evidence from the kernel.
Review the resolved folder before consenting. A successful grant prepares a retry in the
originating conversation's composer, preserving the existing draft.

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
command nobody recognizes is not a control. Hermes does not apply ancestor ACL changes through
that retired control or its former preparation endpoint. The manual recipes here preserve the
mechanism and known limitations for a future, separately verified setup flow.

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
- `execute_code` and uncontained browser/desktop/provider tools are unavailable in strict mode,
  even when network access is enabled. Terminal programs and file tools are the supported path.
- Assistant-generated media, interactive widgets and executable previews are withheld while the
  sandbox is enabled or their owning profile cannot be verified. Your own uploaded attachments
  remain usable. New model-selected file, media and title requests check current policy before IO.
- This window's settings changes invalidate preview permissions immediately. Changes made in
  another window or through the CLI withdraw existing previews on a four-second refresh cycle,
  plus connection latency; that withdrawal is not instantaneous.
- Policy changes retire or drain tracked terminal commands, code kernels and terminal background
  jobs for the edited profile. Wait for a successful transition before retrying work. This does
  not retroactively sandbox a host application or scheduler script that was already running;
  stop existing host automation before relying on containment. New host-script launches are refused.
- This is not a separate desktop or a screen-privacy boundary. Win32k remains enabled so ordinary
  console runtimes can start; clipboard, input injection and the supported UI restrictions are
  applied, but do not assume that every GUI or screen-capture operation is unavailable.
- The switch is binary on current Windows builds. Per-domain allow or deny lists for sandboxed
  commands need a host-side egress proxy the container can reach, and the container cannot reach
  the host on these builds; that arrives with the next MXC contract.
- Messaging gateways must set `terminal.cwd` to a project folder; the sandbox will not accept the
  gateway's home directory as a workspace.

## Configuration reference

```yaml
terminal:
  backend: mxc
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
5. Turn on the sandbox in **Settings → Safety**. A source install downloads the kit and shell
   then; a sealed install already has them.
6. Run one task that stays inside the workspace and one that reaches outside it, and grant the
   folder from the tool card, so every path has been exercised before the audience arrives.
