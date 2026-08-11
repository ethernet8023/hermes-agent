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
; confused, and it actually clicks Launch instead of closing the window,
; because the Launch hand-off is part of the flow under test.
;
; Args: [1] log path  [2] setup exe name (default Hermes-Setup.exe)

logPath := A_Args.Length >= 1 ? A_Args[1] : "ahk.log"
setupExe := A_Args.Length >= 2 ? A_Args[2] : "Hermes-Setup.exe"

Log(text) {
    msg := Format("[autohotkey] {}`n", text)
    ToolTip(text)
    FileAppend(msg, '*')
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

FindImageInWindow(winTitle, imageFile, &outX, &outY, timeoutMs := 10000, intervalMs := 250)
{
    WinGetPos(&wx, &wy, &ww, &wh, winTitle)

    hBitmap := LoadPicture(imageFile)
    if !hBitmap {
        throw Error("LoadPicture failed: " imageFile)
    }
    bm := Buffer(32, 0) ; BITMAP structure on x64
    DllCall("GetObject", "Ptr", hBitmap, "Int", bm.Size, "Ptr", bm)
    width := NumGet(bm, 4, "Int")
    height := NumGet(bm, 8, "Int")

    startTime := A_TickCount
    timeLeft := 1
    Log(Format("Searching for {} in {} ...", imageFile, winTitle))
    searchImage := Format("*10 {}", imageFile)
    while (timeLeft > 0)
    {
        ; Refresh the window rect each pass — the installer window can move
        ; or resize between stages.
        try WinGetPos(&wx, &wy, &ww, &wh, winTitle)
        if ImageSearch(&x, &y, wx, wy, wx + ww, wy + wh, searchImage)
        {
            outX := x + Floor(width / 2)
            outY := y + Floor(height / 2)
            Log("Found " imageFile)
            return
        }
        Sleep intervalMs
        timeLeft := timeoutMs - (A_TickCount - startTime)
        ToolTip(Format("Searching {} in {} ... {}s left", imageFile, winTitle, Round(timeLeft / 1000, 2)))
    }
    throw Error(Format("Failed to find {} in window {}", imageFile, winTitle))
}

ClickCenterOfImageInWindow(winTitle, imageFile, timeoutMs := 10000, intervalMs := 250)
{
    FindImageInWindow(winTitle, imageFile, &x, &y, timeoutMs, intervalMs)
    ClickWithMarker(x, y)
}

installerWin := "ahk_exe " setupExe
appWin := "ahk_exe Hermes.exe"

Log("Waiting for the installer window (" installerWin ") ...")
try {
    WinWait(installerWin, , 60)
} catch {
    throw Error("installer window did not appear within 60s")
}
WinGetPos(&x, &y, &w, &h, installerWin)
Log(Format("Window found at x={1} y={2} w={3} h={4}", x, y, w, h))

; ── Step 1: click Install ───────────────────────────────────────────────
ClickCenterOfImageInWindow(installerWin, A_ScriptDir "\install-button.png", 60000)
Log("Install clicked; waiting for the Launch button (install can take a while)")

; ── Step 2: wait for install to finish (Launch button appears) ──────────
FindImageInWindow(installerWin, A_ScriptDir "\launch-button.png", &launchX, &launchY, 1000 * 60 * 45)
Log("Install finished (Launch button visible)")

; ── Step 3: click Launch — the hand-off under test ──────────────────────
ClickWithMarker(launchX, launchY)
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
