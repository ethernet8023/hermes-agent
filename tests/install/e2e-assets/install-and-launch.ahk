#Requires AutoHotkey v2.0
#SingleInstance Force

; Drive the REAL Hermes-Setup.exe (Tauri bootstrap installer) window:
; click Install, wait for the install to finish, click Launch, and wait for
; the real Hermes.exe (Electron desktop) window to appear.
;
; Adapted from @ethernet8023's e2e/windows/install-hermes-desktop.ahk
; (PR #68183) — same ImageSearch approach and button templates; this
; variant targets windows by process name (ahk_exe) so the installer
; window and the launched app window (both titled "Hermes") can't be
; confused, and it clicks Launch instead of closing the window, because
; the Launch hand-off is part of the flow under test.
;
; Robustness beyond the original:
;   * Log() survives a missing stdout (GUI-subsystem AHK started without a
;     console throws "(6) The handle is invalid" on FileAppend to '*' —
;     that single throw killed the whole first CI attempt).
;   * If a button template doesn't match (installer UI restyled), falls
;     back to clicking the button's known relative position, and the
;     install-finished signal falls back to "bootstrap complete" in
;     bootstrap-installer.log. Every fallback is logged loudly.
;
; Args: [1] log path  [2] setup exe name  [3] bootstrap-installer.log path

logPath := A_Args.Length >= 1 ? A_Args[1] : "ahk.log"
setupExe := A_Args.Length >= 2 ? A_Args[2] : "Hermes-Setup.exe"
bootstrapLog := A_Args.Length >= 3 ? A_Args[3] : ""

Log(text) {
    msg := Format("[autohotkey] {}`n", text)
    ToolTip(text)
    ; stdout only exists when AHK was launched from a console. Under
    ; Start-Process (no console) FileAppend to '*' throws "(6) The handle
    ; is invalid" — and a Log() that throws kills the whole script from
    ; inside OnError. The file log below is the real record.
    try FileAppend(msg, '*')
    FileAppend(msg, logPath)
}

OnError(LogError)

LogError(err, mode) {
    Log(Format("Unhandled error: {}", err.Message))
    ExitApp(1)
    return -1  ; suppress the standard error dialog
}

SetWorkingDir(A_ScriptDir)
CoordMode("Pixel", "Screen")
CoordMode("Mouse", "Screen")

ClickWithMarker(x, y, button := "Left") {
    Click(x, y, button)
    Sleep(10)
    MouseMove(30, 30)
    Log(Format("Clicked at {1}, {2}", x, y))
}

; Single-pass image search inside a window. Returns true + center coords.
TryFindImage(winTitle, imageFile, &outX, &outY) {
    try {
        WinGetPos(&wx, &wy, &ww, &wh, winTitle)
    } catch {
        return false
    }
    hBitmap := LoadPicture(imageFile)
    if !hBitmap {
        throw Error("LoadPicture failed: " imageFile)
    }
    bm := Buffer(32, 0) ; BITMAP structure on x64
    DllCall("GetObject", "Ptr", hBitmap, "Int", bm.Size, "Ptr", bm)
    width := NumGet(bm, 4, "Int")
    height := NumGet(bm, 8, "Int")
    if ImageSearch(&x, &y, wx, wy, wx + ww, wy + wh, Format("*10 {}", imageFile)) {
        outX := x + Floor(width / 2)
        outY := y + Floor(height / 2)
        return true
    }
    return false
}

; Fractional window position -> screen coords (fallback click target).
WindowRelPoint(winTitle, fx, fy, &outX, &outY) {
    WinGetPos(&wx, &wy, &ww, &wh, winTitle)
    outX := wx + Floor(ww * fx)
    outY := wy + Floor(wh * fy)
}

BootstrapLogContains(needle) {
    global bootstrapLog
    if (bootstrapLog = "" or !FileExist(bootstrapLog)) {
        return false
    }
    try {
        ; Read-share open: the installer still holds the file for writing.
        f := FileOpen(bootstrapLog, "r-d")
        if !f {
            return false
        }
        content := f.Read()
        f.Close()
        return InStr(content, needle) > 0
    } catch {
        return false
    }
}

installerWin := "ahk_exe " setupExe
appWin := "ahk_exe Hermes.exe"

; The Install/Launch button sits centered horizontally near the bottom of
; the installer window (measured from production screenshots; used only
; when the image template fails to match a restyled UI).
BTN_FX := 0.50
BTN_FY := 0.87

Log("Waiting for the installer window (" installerWin ") ...")
try {
    WinWait(installerWin, , 60)
} catch {
    throw Error("installer window did not appear within 60s")
}
WinGetPos(&x, &y, &w, &h, installerWin)
Log(Format("Window found at x={1} y={2} w={3} h={4}", x, y, w, h))

; ── Step 1: click Install (template first, relative-position fallback) ──
installClicked := false
deadline := A_TickCount + 60000
while (A_TickCount < deadline) {
    if TryFindImage(installerWin, A_ScriptDir "\install-button.png", &ix, &iy) {
        ClickWithMarker(ix, iy)
        Log("Install clicked (template match)")
        installClicked := true
        break
    }
    Sleep(500)
}
if !installClicked {
    WindowRelPoint(installerWin, BTN_FX, BTN_FY, &ix, &iy)
    ClickWithMarker(ix, iy)
    Log("FALLBACK: install template never matched; clicked relative position")
}

; ── Step 2: wait for the install to finish ──────────────────────────────
; Primary signal: the Launch button template appears. Secondary signal:
; "bootstrap complete" in bootstrap-installer.log (the installer's own
; completion line) — after which we give the template 2 more minutes and
; then fall back to the relative-position click.
launchX := 0, launchY := 0
launchFound := false
completeSince := 0
waitDeadline := A_TickCount + 1000 * 60 * 45
Log("Waiting for install to finish (Launch template or bootstrap log) ...")
while (A_TickCount < waitDeadline) {
    if TryFindImage(installerWin, A_ScriptDir "\launch-button.png", &launchX, &launchY) {
        launchFound := true
        Log("Install finished (Launch template visible)")
        break
    }
    if (completeSince = 0 and BootstrapLogContains("bootstrap complete")) {
        completeSince := A_TickCount
        Log("bootstrap-installer.log reports completion; giving the Launch template 120s")
    }
    if (completeSince > 0 and A_TickCount - completeSince > 120000) {
        Log("FALLBACK: log says complete but Launch template never matched")
        break
    }
    Sleep(2000)
}
if (!launchFound and completeSince = 0) {
    throw Error("install did not finish within 45 minutes (no Launch button, no completion log line)")
}

; ── Step 3: click Launch — the hand-off under test ──────────────────────
if launchFound {
    ClickWithMarker(launchX, launchY)
} else {
    WindowRelPoint(installerWin, BTN_FX, BTN_FY, &lx, &ly)
    ClickWithMarker(lx, ly)
    Log("FALLBACK: clicked Launch at relative position")
}
Log("Launch clicked; waiting for the Hermes desktop app window")

; The installer spawns Hermes.exe detached and exits itself.
try {
    WinWait(appWin, , 120)
} catch {
    throw Error("Hermes.exe window did not appear within 120s of clicking Launch")
}
WinGetPos(&ax, &ay, &aw, &ah, appWin)
Log(Format("App window appeared at x={1} y={2} w={3} h={4}", ax, ay, aw, ah))

; Give the renderer a few seconds on screen (recorded as proof), then hand
; control back to the PowerShell driver, which closes the app and re-launches
; it under Playwright for the update legs.
Sleep(8000)
Log("done")
ExitApp(0)
