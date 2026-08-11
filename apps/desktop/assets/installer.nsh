# Hermes Windows installer: assisted flow with Nous branding.
#
# electron-builder includes this file through `nsis.include` in
# apps/desktop/package.json. The include lands in the generated script's
# shared header, BEFORE `!include MUI2.nsh` runs, so two rules shape this
# file:
#   - Top-level `!define MUI_*` interface settings are read later by
#     Modern UI when it builds the pages — that part is free.
#   - Top level must not COMPILE anything that needs MUI2 / nsDialogs /
#     WinMessages macros (they are not included yet). Functions are
#     therefore defined inside the custom*Page macros, which the
#     assisted template expands after all of those includes.
#
# Flow (oneClick=false): welcome page whose Next button is relabeled
# Install -> instfiles page with the progress bar, an animated mascot
# (SysAnimate32 playing a palette RLE AVI built from
# public/hermes-frames), and the status line -> finish page with a
# "Launch Hermes Agent" checkbox. The all-users/current-user radio page
# is skipped: customInstallMode forces the per-user install the oneClick
# build always used.
#
# The documented default include location is build/installer.nsh, but
# apps/desktop/build/ is gitignored (it holds the agent-payload build
# artifact), so the file lives in assets/ and is wired explicitly.
#
# NSIS color format is RRGGBB hex with no leading '#'. Colors mirror
# apps/desktop/src/themes/presets.ts (nousTheme.lightColors) — NSIS has
# no dark-mode awareness, so light is the one palette (same rule as the
# update hand-off window).

!define MUI_BGCOLOR "F8FAFF"
!define MUI_TEXTCOLOR "17171A"
!define MUI_INSTFILESPAGE_COLORS "17171A F8FAFF"

# SysAnimate32 constants (commctrl.h). ACM_OPENW = WM_USER+103 (the
# installer build is Unicode, so SendMessage STR: sends a wide string).
# Style = WS_CHILD|WS_VISIBLE|ACS_CENTER|ACS_AUTOPLAY. No
# ACS_TRANSPARENT: the AVI bakes in the dialog background color, which
# avoids the flicker that style causes on themed dialogs.
!define HERMES_ACM_OPENW 0x467
!define HERMES_ACS_STYLE 0x50000005
!define HERMES_ICC_ANIMATE_CLASS 0x80

Var hermesSpinner

!macro customInstallMode
  # Skip the all-users/current-user radio page: this installer has
  # always been per-user (oneClick era), and the welcome page promises
  # Install starts the install, not another choice.
  StrCpy $isForceCurrentInstall "1"
!macroend

!macro customWelcomePage
  # On an auto-update relaunch the user already consented — skip
  # straight to the progress page (same treatment the template gives
  # the license page).
  !insertmacro skipPageIfUpdated
  !define MUI_WELCOMEPAGE_TITLE "Hermes Agent"
  !define MUI_WELCOMEPAGE_TEXT "This will install Hermes Agent on your computer.$\r$\n$\r$\nClick Install to begin."
  !define MUI_PAGE_CUSTOMFUNCTION_SHOW hermesWelcomeShow
  !insertmacro MUI_PAGE_WELCOME

  Function hermesWelcomeShow
    # The welcome page's Next button IS the install trigger here (the
    # install-mode page aborts itself and the next stop is instfiles),
    # so give it the standard Install label.
    GetDlgItem $0 $HWNDPARENT 1
    SendMessage $0 ${WM_SETTEXT} 0 "STR:$(^InstallBtn)"
  FunctionEnd
!macroend

# The assisted template expands this directly before MUI_PAGE_INSTFILES
# — the one hook point where a SHOW define reliably lands on instfiles.
!macro customPageAfterChangeDir
  !define MUI_PAGE_CUSTOMFUNCTION_SHOW hermesInstFilesShow

  Function hermesInstFilesShow
    File /oname=$PLUGINSDIR\hermes-spinner.avi "${PROJECT_DIR}\assets\hermes-spinner.avi"

    # MUI2 exposes no HWND var for instfiles; the inner page dialog is
    # the #32770 child of the wizard frame.
    FindWindow $1 "#32770" "" $HWNDPARENT

    # Hide the details toggle — expanding the log list would cover the
    # mascot, and the generated section prints nothing into it anyway.
    GetDlgItem $0 $1 1027
    ShowWindow $0 ${SW_HIDE}

    # The generated install section runs `SetDetailsPrint none`, so the
    # status line (1006) receives no text from the engine; seed it
    # directly. Nsis7z's own per-file updates during payload extraction
    # go straight to this control and are not muted by that setting.
    GetDlgItem $0 $1 1006
    SendMessage $0 ${WM_SETTEXT} 0 "STR:Installing Hermes Agent…"

    # SysAnimate32 needs an explicit InitCommonControlsEx — the animate
    # class is not among the ones the NSIS dialog already pulls in.
    System::Call '*(i 8, i ${HERMES_ICC_ANIMATE_CLASS}) p .r2'
    System::Call 'comctl32::InitCommonControlsEx(p r2)'
    System::Free $2

    # Center the mascot in the empty area under the progress bar.
    System::Call '*(i, i, i, i) p .r2'
    System::Call 'user32::GetClientRect(p r1, p r2)'
    System::Call '*$2(i, i, i .r3, i .r4)'
    System::Free $2
    IntOp $3 $3 - 112
    IntOp $3 $3 / 2
    IntOp $4 $4 - 110
    System::Call 'user32::CreateWindowEx(i 0, t "SysAnimate32", t "", i ${HERMES_ACS_STYLE}, i r3, i r4, i 112, i 100, p r1, i 0, p 0, p 0) p .s'
    Pop $hermesSpinner
    SendMessage $hermesSpinner ${HERMES_ACM_OPENW} 0 "STR:$PLUGINSDIR\hermes-spinner.avi"
  FunctionEnd
!macroend

!macro customFinishPage
  !define MUI_FINISHPAGE_TITLE "Hermes Agent is installed"
  !define MUI_FINISHPAGE_TEXT "Setup is complete."
  !define MUI_FINISHPAGE_RUN
  !define MUI_FINISHPAGE_RUN_TEXT "Launch Hermes Agent"
  !define MUI_FINISHPAGE_RUN_FUNCTION hermesLaunch
  !insertmacro MUI_PAGE_FINISH

  Function hermesLaunch
    # Same launch shape as the template's StartApp: through the shell as
    # the unelevated user, via the shortcut so the working directory and
    # app-model id come from the link.
    ${StdUtils.ExecShellAsUser} $0 "$launchLink" "open" ""
  FunctionEnd
!macroend

!macro customInstall
  # Runs after files are extracted, on install AND update. Guard
  # first-install-only work with the updated flag electron-builder sets
  # when the app relaunches the installer for an auto-update.
  ${ifNot} ${isUpdated}
    # first-install-only steps go here
  ${endIf}
!macroend

!macro customUnInstall
  # Manual uninstall only: an update runs the uninstaller too, and
  # user-data cleanup during an update would destroy the installation it
  # is refreshing.
  ${ifNot} ${isUpdated}
    # manual-uninstall-only steps go here
  ${endIf}
!macroend
