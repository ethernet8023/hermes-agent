#Requires AutoHotkey v2.0
#SingleInstance Force

; Drives the Hermes bootstrap installer (Hermes-Setup.exe) through a real
; install: waits for the window, clicks Install, waits for the Launch button
; to appear (install finished), then closes the window WITHOUT launching the
; app -- the E2E driver verifies the install and runs the update route itself.
;
; Args:
;   1: log file path (default ahk.log in the working dir)
;
; Button images live next to this script. They are literal screenshots of the
; installer's buttons; ImageSearch runs with *10 shade tolerance so minor
; rendering differences (ClearType, DPI rounding) still match.

logPath := A_Args.Length >= 1 ? A_Args[1] : "ahk.log"

Log(text) {
    msg := Format("[autohotkey] {}`n", text)
    ToolTip(text)
    ; stdout only exists when the launcher attached a console (Start-Process
    ; -NoNewWindow). AutoHotkey64 is a GUI-subsystem exe, so a bare spawn has
    ; an invalid stdout handle and FileAppend('*') throws "(6) The handle is
    ; invalid" -- recursively, from inside OnError's own Log call, which
    ; wedges the script instead of exiting. The log FILE is the record;
    ; stdout is best-effort.
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
    Log(Format("Clicking at {1}, {2}", x, y))
    ; Draw a short-lived red dot where we clicked so the screen recording
    ; shows WHERE the automation acted, not just what happened after.
    size := 20
    g := Gui("-Caption +AlwaysOnTop +ToolWindow")
    g.BackColor := "Red"
    g.Show(Format(
        "x{} y{} w{} h{} NoActivate"
        , x - size // 2
        , y - size // 2
        , size
        , size
    ))
    hRegion := DllCall(
        "CreateEllipticRgn"
        , "Int", 0
        , "Int", 0
        , "Int", size
        , "Int", size
        , "Ptr"
    )
    DllCall("SetWindowRgn", "Ptr", g.Hwnd, "Ptr", hRegion, "Int", true)
    WinSetTransparent(255, g.Hwnd)
    SetTimer(() => g.Destroy(), -500)
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

    Log(Format("Searching for button file {} in window {}...", imageFile, winTitle))
    searchImage := Format("*10 {}", imageFile)
    while (timeLeft > 0)
    {
        if ImageSearch(&x, &y, wx, wy, wx + ww, wy + wh, searchImage)
        {
            outX := x + Floor(width / 2)
            outY := y + Floor(height / 2)
            Log("Found button!")
            return
        }

        Sleep intervalMs
        timeLeft := timeoutMs - (A_TickCount - startTime)
        ToolTip(Format("Searching for button {} in window {}...  {}s left", imageFile, winTitle, Round(timeLeft / 1000, 2)))
    }

    throw Error(Format("Failed to find button {} in window {}", imageFile, winTitle))
}

ClickCenterOfImageInWindow(winTitle, imageFile, timeoutMs := 10000, intervalMs := 250)
{
    FindImageInWindow(winTitle, imageFile, &x, &y, timeoutMs, intervalMs)
    ClickWithMarker(x, y)
}

Log("Waiting for the installer window to appear...")
winTitle := "Hermes"
try {
    WinWait(winTitle, , 30)
} catch {
    throw Error("Hermes installer window did not appear within 30s")
}
WinGetPos(&x, &y, &w, &h, winTitle)
Log(Format("Window found at x={1} y={2} w={3} h={4}", x, y, w, h))

ClickCenterOfImageInWindow(winTitle, A_ScriptDir "\install-button.png")

; Wait for the install to finish. The Launch button only renders when every
; stage (git, uv, Python, Node, venv, desktop build) has completed, so its
; appearance IS the success signal. A real install takes many minutes.
FindImageInWindow(winTitle, A_ScriptDir "\launch-button.png", &launchX, &launchY, 1000 * 60 * 25)

; Close instead of clicking Launch: the E2E driver owns everything after the
; install, and a launched desktop would hold the venv shim open and block the
; update route.
WinClose(winTitle)

Sleep(2000)

ExitApp(0)
