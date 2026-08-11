#Requires AutoHotkey v2.0
#SingleInstance Force

; Drives the Hermes bootstrap installer (Hermes-Setup.exe) through a real
; install: waits for the window, clicks Install, waits for the
; bootstrap-complete marker file (the installer writes it after every stage
; has finished, before the Launch screen renders), then closes the window
; WITHOUT launching the app -- the E2E driver verifies the install and runs
; the update route itself.
;
; Args:
;   1: log file path (default ahk.log in the working dir)
;   2: bootstrap-complete marker path to poll for (required)
;
; The Install button image lives next to this script. It is a literal crop
; of the published installer's button from a CI screen recording;
; ImageSearch runs with a generous shade tolerance because the reference
; passed through video compression. Completion deliberately does NOT use a
; second button image: the marker file is the installer's own completion
; signal and cannot go stale with a UI restyle.

logPath := A_Args.Length >= 1 ? A_Args[1] : "ahk.log"
markerPath := A_Args.Length >= 2 ? A_Args[2] : ""

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
    ; *60: the reference crop survived H.264 video compression, so per-channel
    ; drift up to ~60 shades must still count as a match.
    searchImage := Format("*60 {}", imageFile)
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

if (markerPath = "") {
    throw Error("marker path argument is required")
}

; The reference crop went through H.264 compression, so allow a wide shade
; tolerance; retry the click a few times in case the first lands during a
; window animation frame.
clicked := false
attempts := 0
while (!clicked && attempts < 5) {
    attempts += 1
    try {
        ; ImageSearch reads SCREEN pixels: anything covering the installer
        ; (the runner session keeps a maximized console in front) makes the
        ; button invisible even though WinWait's title match succeeded.
        ; Force the installer to the foreground before every attempt.
        WinActivate(winTitle)
        WinMoveTop(winTitle)
        Sleep(500)
        ClickCenterOfImageInWindow(winTitle, A_ScriptDir "\install-button.png", 20000, 250)
        clicked := true
    } catch as err {
        Log(Format("Install click attempt {} failed: {}", attempts, err.Message))
        Sleep(2000)
    }
}
if (!clicked) {
    throw Error("could not find/click the Install button after " attempts " attempts")
}

; Wait for the installer's own completion signal: the bootstrap-complete
; marker is written after the last stage succeeds. A real install takes many
; minutes (git, uv, Python, Node, venv, desktop build).
Log(Format("Waiting for bootstrap-complete marker: {}", markerPath))
deadline := A_TickCount + 1000 * 60 * 25
while (A_TickCount < deadline) {
    if FileExist(markerPath) {
        Log("Marker found -- install complete")
        ; Close instead of clicking Launch: the E2E driver owns everything
        ; after the install, and a launched desktop would hold the venv shim
        ; open and block the update route.
        try WinClose(winTitle)
        Sleep(2000)
        ExitApp(0)
    }
    Sleep(5000)
}
throw Error("bootstrap-complete marker never appeared within 25 minutes")
