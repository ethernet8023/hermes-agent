# Combined NSIS include for the Hermes desktop installers. Wired from
# electron-builder.config.cjs through nsis.include (which takes ONE file);
# electron-builder splices customInit / customInstall / customUnInstall
# into the generated installer script.
#
# Two concerns live here:
#   1. Arch guard (customInit): refuse to run on the wrong machine.
#   2. CLI PATH exposure (customInstall/customUnInstall): make the bundled
#      payload's CLI shims reachable from any terminal.

!include "x64.nsh"

# ── Arch guard ───────────────────────────────────────────────────────────────
#
# The stock identify_package macro
# (app-builder-lib templates/nsis/include/extractAppPackage.nsh) treats an
# arm64 machine as a valid x64 host. An x64 installer on arm64 Windows
# installs silently and the app then runs in emulation forever. The reverse
# direction is worse: an arm64 installer on an x64 machine matches no
# package macro, so the installer "succeeds" and writes an EMPTY install.
#
# The defines and the native-machine tests come from the surrounding
# electron-builder machinery: APP_64/APP_ARM64 exist per embedded payload,
# x64.nsh supplies IsNativeAMD64/IsNativeARM64, which see through WOW
# emulation. The !ifndef nesting keeps a future multi-arch installer
# permissive: it carries both payloads, so it must not be blocked on
# either machine.
#
# MessageBox carries /SD IDOK so a silent install (/S, the electron-updater
# path) does not hang on a dialog. SetErrorLevel 2 = "installation aborted
# by script", so a silent wrong-arch install reports failure instead of
# pretending success.

!macro customInit
  !ifdef APP_ARM64
    !ifndef APP_64
      ${IfNot} ${IsNativeARM64}
        MessageBox MB_OK|MB_ICONSTOP|MB_SETFOREGROUND \
          "This installer is for Windows on ARM (arm64).$\r$\n$\r$\nThis computer is not arm64. Download the x64 installer instead." \
          /SD IDOK
        SetErrorLevel 2
        Quit
      ${EndIf}
    !endif
  !endif
  !ifdef APP_64
    !ifndef APP_ARM64
      ${IfNot} ${IsNativeAMD64}
        MessageBox MB_OK|MB_ICONSTOP|MB_SETFOREGROUND \
          "This installer is for x64 Windows.$\r$\n$\r$\nThis computer is not x64. Download the arm64 installer instead." \
          /SD IDOK
        SetErrorLevel 2
        Quit
      ${EndIf}
    !endif
  !endif
!macroend

# ── CLI PATH exposure ────────────────────────────────────────────────────────
#
# A bundled install ships prebuilt, signed CLI shims (hermes.exe,
# hermes-agent.exe, hermes-acp.exe) inside the payload at
# resources\agent-payload\bin — staged by stage-agent-payloads.mjs, signed
# with the rest of the tree, self-relative via the shim-target.txt sidecar.
# The installer's ONLY job is to point the user PATH at that directory.
# Nothing is generated or copied at install time: post-install byte
# generation is exactly what a sealed, signed artifact cannot do.
#
# The dir added is the payload bin — NEVER $INSTDIR itself. $INSTDIR holds
# the GUI Hermes.exe, and NTFS name resolution is case-insensitive: with
# $INSTDIR on PATH, typing `hermes` in a terminal would launch the desktop
# app instead of the CLI.
#
# Gated on the shims actually existing so the external (non-bundled) NSIS
# artifact — which carries a stub payload with no bin/ — adds nothing.
#
# The edit targets the user scope (HKCU\Environment) because perMachine is
# false, and it must be idempotent in both directions: the oneClick updater
# runs the old uninstaller and then this installer on every update.
#
# It is written against advapi32 directly rather than against the EnVar
# plugin. EnVar is the usual answer to this problem, but electron-builder
# has never shipped it: neither the v27 default bundle (nsis@1.2.1,
# nsis-bundle-3.12) nor the legacy bundle (nsis-resources-3.4.1) carries
# EnVar.dll in Plugins\x86-unicode, so `EnVar::SetHKCU` fails the build at
# compile time with "Plugin not found". Vendoring the DLL would put a
# prebuilt binary we cannot rebuild or audit inside a signed installer, for
# work that is ~40 lines of script against the System plugin the bundle DOES
# ship.
#
# PATH is read with RegQueryValueEx, NOT with ReadRegStr. ReadRegStr returns
# an empty string both when the value is missing and when it is longer than
# ${NSIS_MAX_STRLEN}, so the naive version silently REPLACES a long user PATH
# with our one entry. RegQueryValueEx distinguishes the two
# (ERROR_MORE_DATA), which lets us refuse to touch a PATH we cannot read
# whole. The registry type is round-tripped for the same reason: rewriting a
# REG_SZ PATH as REG_EXPAND_SZ would change how a literal '%' in it behaves.
#
# EVERY System::Call here sits inside a !macro that only customInstall and
# customUnInstall insert. None may move into a top-level Function, because a
# Function body is compiled where the file is !include-d, and that is before
# electron-builder's own `!addplugindir` line.
#
# The generator emits that line and the !include of this file as two
# CONCURRENT AsyncTaskManager tasks (NsisTarget.computeCommonInstallerScriptHeader),
# each appending when it resolves: the plugin path awaits a toolset resolve,
# the include awaits a stat, so the include reliably lands first. A plugin
# used before !addplugindir binds to ${NSISDIR}'s default plugin directory;
# !addplugindir then registers the SAME plugin under a second path, and the
# next use of it — app-builder-lib's own getProcessInfo.nsh — aborts the
# compile with "Plugin command System::Call conflicts with a plugin in
# another directory!". Keeping the calls inside macros defers them past the
# whole generated header, so the order stops mattering.
#
# The macros take a LABEL because an inlined macro repeats its code at every
# insertion point, and NSIS labels must be unique per function.

!include "LogicLib.nsh"
!include "WinMessages.nsh"

!define HERMES_PATH_KEY 'HKCU "Environment"'
!define HERMES_PATH_HKCU 0x80000001
!define HERMES_PATH_KEY_READ 0x20019    # STANDARD_RIGHTS_READ|KEY_QUERY_VALUE|KEY_ENUMERATE_SUB_KEYS|KEY_NOTIFY
!define HERMES_PATH_ERROR_FILE_NOT_FOUND 2
!define HERMES_PATH_REG_SZ 1
!define HERMES_PATH_REG_EXPAND_SZ 2

# cbData is a BYTE count; the buffer the System plugin hands out is one NSIS
# string (${NSIS_MAX_STRLEN} chars). Keep one char back for the terminator.
!define /math _HERMES_PATH_CHARS ${NSIS_MAX_STRLEN} - 1
!define /math HERMES_PATH_CB_MAX ${_HERMES_PATH_CHARS} * ${NSIS_CHAR_SIZE}

# Reads HKCU\Environment\PATH.
#   out $1 = current value ("" when the value does not exist)
#   out $2 = registry type (REG_EXPAND_SZ when the value does not exist)
#   out $3 = 0 when $1 is trustworthy, 1 when PATH must not be touched
# clobbers $4 $5 $6
!macro HermesReadUserPath
  StrCpy $1 ""
  StrCpy $2 ${HERMES_PATH_REG_EXPAND_SZ}
  StrCpy $3 1
  System::Call "advapi32::RegOpenKeyEx(i ${HERMES_PATH_HKCU}, t'Environment', i 0, i ${HERMES_PATH_KEY_READ}, *i.r4) i.r5"
  ${If} $5 = 0
    StrCpy $6 ${HERMES_PATH_CB_MAX}
    System::Call "advapi32::RegQueryValueEx(i $4, t'PATH', i 0, *i.r2, t.r1, *i r6r6) i.r5"
    System::Call "advapi32::RegCloseKey(i $4)"
    ${If} $5 = ${HERMES_PATH_ERROR_FILE_NOT_FOUND}
      StrCpy $1 ""
      StrCpy $2 ${HERMES_PATH_REG_EXPAND_SZ}
      StrCpy $3 0
    ${ElseIf} $5 = 0
    ${AndIf} $2 = ${HERMES_PATH_REG_SZ}
      StrCpy $3 0
    ${ElseIf} $5 = 0
    ${AndIf} $2 = ${HERMES_PATH_REG_EXPAND_SZ}
      StrCpy $3 0
    ${EndIf}
  ${EndIf}
!macroend

# Locates ";<dir>;" inside ";<path>;" — the sentinel semicolons make an entry
# match only on whole-entry boundaries, so "C:\a\bin" never matches inside
# "C:\a\bin2". StrCmp is case-insensitive, which is what Windows paths want.
#   in  $4 = haystack, $5 = needle
#   out $7 = match index, or -1
# clobbers $6
!macro HermesFindPathEntry LABEL
  StrLen $6 $5
  StrCpy $7 0
  hermes_scan_${LABEL}:
    StrCpy $8 $4 $6 $7
    StrCmp $8 "" 0 +3
      StrCpy $7 -1
      Goto hermes_scan_done_${LABEL}
    StrCmp $8 $5 hermes_scan_done_${LABEL}
    IntOp $7 $7 + 1
    Goto hermes_scan_${LABEL}
  hermes_scan_done_${LABEL}:
!macroend

# Writes PATH back as $1 with type $2 and tells the shell about it, so a
# terminal opened after the install sees the change without a logout.
!macro HermesWriteUserPath
  ClearErrors
  ${If} $2 = ${HERMES_PATH_REG_SZ}
    WriteRegStr ${HERMES_PATH_KEY} "PATH" "$1"
  ${Else}
    WriteRegExpandStr ${HERMES_PATH_KEY} "PATH" "$1"
  ${EndIf}
  ${If} ${Errors}
    DetailPrint "Could not write PATH to HKCU\Environment"
  ${Else}
    SendMessage ${HWND_BROADCAST} ${WM_SETTINGCHANGE} 0 "STR:Environment" /TIMEOUT=5000
  ${EndIf}
!macroend

!macro HermesAddPathEntry LABEL
  Exch $0
  Push $1
  Push $2
  Push $3
  Push $4
  Push $5
  Push $6
  Push $7
  Push $8

  !insertmacro HermesReadUserPath
  ${If} $3 <> 0
    DetailPrint "Left PATH alone: it is longer than this installer can read safely"
    Goto hermes_add_done_${LABEL}
  ${EndIf}

  StrCpy $4 ";$1;"
  StrCpy $5 ";$0;"
  !insertmacro HermesFindPathEntry add_${LABEL}
  ${If} $7 <> -1
    Goto hermes_add_done_${LABEL}
  ${EndIf}

  # +1 for the joining semicolon.
  StrLen $4 $0
  StrLen $5 $1
  IntOp $4 $4 + $5
  IntOp $4 $4 + 1
  ${If} $4 >= ${NSIS_MAX_STRLEN}
    DetailPrint "Left PATH alone: adding the Hermes CLI would overflow it"
    Goto hermes_add_done_${LABEL}
  ${EndIf}

  ${If} $1 == ""
    StrCpy $1 "$0"
  ${Else}
    StrCpy $4 $1 1 -1
    ${If} $4 == ";"
      StrCpy $1 $1 -1
    ${EndIf}
    StrCpy $1 "$1;$0"
  ${EndIf}
  DetailPrint "Adding the Hermes CLI to PATH: $0"
  !insertmacro HermesWriteUserPath

  hermes_add_done_${LABEL}:
  Pop $8
  Pop $7
  Pop $6
  Pop $5
  Pop $4
  Pop $3
  Pop $2
  Pop $1
  Pop $0
!macroend

!macro HermesRemovePathEntry LABEL
  Exch $0
  Push $1
  Push $2
  Push $3
  Push $4
  Push $5
  Push $6
  Push $7
  Push $8
  Push $9

  !insertmacro HermesReadUserPath
  ${If} $3 <> 0
    Goto hermes_del_done_${LABEL}
  ${EndIf}
  ${If} $1 == ""
    Goto hermes_del_done_${LABEL}
  ${EndIf}

  StrCpy $4 ";$1;"
  StrCpy $5 ";$0;"
  StrCpy $9 0

  # Splice out every occurrence, keeping the needle's trailing semicolon as
  # the joiner between the surrounding entries.
  hermes_del_next_${LABEL}:
    !insertmacro HermesFindPathEntry del_${LABEL}
    ${If} $7 <> -1
      StrCpy $9 1
      StrCpy $8 $4 $7
      IntOp $7 $7 + $6
      IntOp $7 $7 - 1
      StrCpy $4 $4 "" $7
      StrCpy $4 "$8$4"
      Goto hermes_del_next_${LABEL}
    ${EndIf}

  ${If} $9 = 0
    Goto hermes_del_done_${LABEL}
  ${EndIf}

  # Drop the sentinel semicolons this macro added.
  hermes_del_lstrip_${LABEL}:
    StrCpy $5 $4 1
    ${If} $5 == ";"
      StrCpy $4 $4 "" 1
      Goto hermes_del_lstrip_${LABEL}
    ${EndIf}
  hermes_del_rstrip_${LABEL}:
    StrCpy $5 $4 1 -1
    ${If} $5 == ";"
      StrCpy $4 $4 -1
      Goto hermes_del_rstrip_${LABEL}
    ${EndIf}

  StrCpy $1 $4
  DetailPrint "Removing the Hermes CLI from PATH: $0"
  !insertmacro HermesWriteUserPath

  hermes_del_done_${LABEL}:
  Pop $9
  Pop $8
  Pop $7
  Pop $6
  Pop $5
  Pop $4
  Pop $3
  Pop $2
  Pop $1
  Pop $0
!macroend

!macro customInstall
  ${If} ${FileExists} "$INSTDIR\resources\agent-payload\bin\hermes.exe"
    Push "$INSTDIR\resources\agent-payload\bin"
    !insertmacro HermesAddPathEntry install
  ${EndIf}
!macroend

!macro customUnInstall
  Push "$INSTDIR\resources\agent-payload\bin"
  !insertmacro HermesRemovePathEntry uninstall
!macroend
