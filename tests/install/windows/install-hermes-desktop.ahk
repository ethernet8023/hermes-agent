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

; Find the Install button and click it.
;
; ImageSearch against the reference crop is attempted first, but it is
; expected to fail on a real screen: the crop came from an H.264/yuv420p
; recording, whose chroma subsampling smears the glyph edges -- the same
; crop matches a RECORDING of this screen within 8 shades/channel while
; missing the live screen entirely. The reliable path is PixelSearch for
; the saturated blue of the "[ INSTALL ]" text. Scan only BELOW 55% of the
; window height: the HERMES AGENT title is blue too, and its bottom rows
; reach ~47% -- a boundary that clips them makes every attempt click the
; title (run 31446691812 clicked "blue text at 220, 330" ten times).
FindInstallClickPoint(winTitle, &outX, &outY) {
    WinGetPos(&wx, &wy, &ww, &wh, winTitle)
    try {
        FindImageInWindow(winTitle, A_ScriptDir "\install-button.png", &outX, &outY, 3000, 250)
        Log("Install button located via ImageSearch")
        return true
    } catch {
    }
    lowerY := wy + Floor(wh * 0.55)
    if PixelSearch(&px, &py, wx, lowerY, wx + ww, wy + wh, 0x3B82F6, 90) {
        outX := px
        outY := py
        Log(Format("Install button located via PixelSearch (blue text at {1}, {2})", px, py))
        return true
    }
    return false
}

; Did the click land? The button's blue text vanishes when the UI flips to
; the progress view, so lingering blue in the lower half means it did not.
InstallButtonStillVisible(winTitle) {
    WinGetPos(&wx, &wy, &ww, &wh, winTitle)
    lowerY := wy + Floor(wh * 0.55)
    return PixelSearch(&px, &py, wx, lowerY, wx + ww, wy + wh, 0x3B82F6, 90)
}

clicked := false
attempts := 0
while (!clicked && attempts < 10) {
    attempts += 1
    ; ImageSearch/PixelSearch read SCREEN pixels: anything covering the
    ; installer (the runner session keeps a maximized console in front)
    ; hides the button even though WinWait's title match succeeded. Force
    ; the installer to the foreground before every attempt.
    WinActivate(winTitle)
    WinMoveTop(winTitle)
    Sleep(500)
    x := 0
    y := 0
    if (!FindInstallClickPoint(winTitle, &x, &y)) {
        Log(Format("Install click attempt {}: button not found on screen", attempts))
        Sleep(2000)
        continue
    }
    ClickWithMarker(x, y)
    Sleep(3000)
    if (InstallButtonStillVisible(winTitle)) {
        Log(Format("Install click attempt {}: UI did not advance; retrying", attempts))
        continue
    }
    clicked := true
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
