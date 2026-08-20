# Combined NSIS include for Hermes bundled installs.
#
# electron-builder's nsis.include takes a SINGLE file, so all custom macros
# go here. Two concerns:
#
# 1. Arch guard (from nsis-arch-guard.nsh): a wrong-arch NSIS installer must
#    refuse to run instead of installing an emulated or empty app.
#
# 2. PATH management: add $INSTDIR to user PATH on install, remove on
#    uninstall. Uses the EnVar plugin (shipped by electron-builder) which
#    handles REG_EXPAND_SZ properly and is idempotent. This is the
#    AddToPath-safe approach (nsis.sourceforge.io/AddToPath_safe).
#
# Wired from electron-builder.config.cjs nsis.include, which splices
# customInit/customInstall/customUnInstall into the installer script.

!include "EnVar.nsh"

# ── Arch guard ──────────────────────────────────────────────────────────────
#
# The stock identify_package macro treats an arm64 machine as a valid x64
# host. An x64 installer on arm64 Windows installs silently and the app runs
# in emulation forever. The reverse is worse: arm64 on x64 matches no
# package macro, so the installer "succeeds" and writes an EMPTY install.
#
# MessageBox carries /SD IDOK so a silent install (/S, the electron-updater
# path) does not hang on a dialog. SetErrorLevel 2 = "installation aborted
# by script".

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

# ── PATH management ────────────────────────────────────────────────────────
#
# Add the install directory to user PATH so `hermes` works from any terminal
# immediately after install. The EnVar plugin handles REG_EXPAND_SZ properly
# and is idempotent (checks before adding, never duplicates).
#
# On uninstall, remove the directory from PATH. EnVar::DeleteValue is also
# idempotent — no error if the value wasn't present.

!macro customInstall
  EnVar::AddValue "PATH" "$INSTDIR"
  Pop $0
  ${If} $0 == 0
    DetailPrint "Added $INSTDIR to user PATH"
  ${ElseIf} $0 == 1
    DetailPrint "$INSTDIR already on user PATH"
  ${Else}
    DetailPrint "Warning: could not add $INSTDIR to user PATH (error $0)"
  ${EndIf}
!macroend

!macro customUnInstall
  EnVar::DeleteValue "PATH" "$INSTDIR"
  Pop $0
  ${If} $0 == 0
    DetailPrint "Removed $INSTDIR from user PATH"
  ${Else}
    DetailPrint "Note: $INSTDIR was not on user PATH (error $0)"
  ${EndIf}
!macroend
