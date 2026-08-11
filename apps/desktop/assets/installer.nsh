# Hermes Windows installer theme.
#
# electron-builder includes this file through `nsis.include` in
# apps/desktop/package.json. The include is prepended to the generated
# script BEFORE `!include MUI2.nsh` runs, so the top-level `!define MUI_*`
# interface settings here are read by Modern UI when it builds the pages.
#
# The documented default location is build/installer.nsh, but
# apps/desktop/build/ is gitignored (it holds the agent-payload build
# artifact), so the file lives in assets/ and is wired explicitly.
#
# Scope: the installer is oneClick, so there are no welcome / directory /
# finish pages — the visible surfaces are the SpiderBanner one-click flash,
# the instfiles details view, and the uninstaller dialogs. NSIS has no OS
# dark-mode awareness, so this follows the updater rule from the update
# hand-off window: light stays Nous light. Colors mirror
# apps/desktop/src/themes/presets.ts (nousTheme.lightColors).
#
# NSIS color format is RRGGBB hex with no leading '#'.

# Dialog background / foreground: nousTheme.lightColors.background +
# .foreground.
!define MUI_BGCOLOR "F8FAFF"
!define MUI_TEXTCOLOR "17171A"

# Instfiles details list: same dark-on-light pairing so the log pane does
# not fall back to the Windows system colors.
!define MUI_INSTFILESPAGE_COLORS "17171A F8FAFF"

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
