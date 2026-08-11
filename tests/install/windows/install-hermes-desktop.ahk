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
; of the published installer's "[ INSTALL ]" button, cut from the LOSSLESS
; welcome-screen.png the driver captures in CI (run 31449192962) -- never
; from the ffmpeg recording, whose H.264/yuv420p chroma subsampling shifts
; glyph pixels enough that a video-sourced crop misses the live screen.
; Completion deliberately does NOT use a second button image: the marker
; file is the installer's own completion signal and cannot go stale with a
; UI restyle.

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
    ; *20: the reference is a lossless screen crop, so only minor rendering
    ; drift (ClearType phase, sub-shade rounding) needs absorbing.
    searchImage := Format("*20 {}", imageFile)
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

; Find the Install button via ImageSearch against the lossless screen crop.
; No PixelSearch fallback: color-hunting matched the blue HERMES AGENT title
; (run 31446691812) and the progress view's blue stage text (run 31447405319)
; before it ever matched the button.
FindInstallClickPoint(winTitle, &outX, &outY) {
    try {
        FindImageInWindow(winTitle, A_ScriptDir "\install-button.png", &outX, &outY, 3000, 250)
        Log("Install button located via ImageSearch")
        return true
    } catch {
        return false
    }
}

; Did the click land? The button vanishes when the UI flips to the progress
; view, so finding it again means the click did not take.
InstallButtonStillVisible(winTitle) {
    x := 0
    y := 0
    return FindInstallClickPoint(winTitle, &x, &y)
}

; Best-effort clicking: NEVER throw here. The authoritative signals are
; owned elsewhere -- the driver aborts on "bootstrap FAILED" in the
; installer log, and the marker wait below caps the run. Run 31447405319
; killed a healthy mid-install run because this loop threw on its own
; flawed UI heuristic; that class of failure must stay impossible.
attempts := 0
everClicked := false
while (attempts < 10) {
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
        if (everClicked) {
            ; Click already landed and this is the progress view.
            Log(Format("Attempt {}: button gone after click; proceeding to marker wait", attempts))
            break
        }
        ; Welcome screen may still be rendering.
        Log(Format("Attempt {}: no Install button in scan band yet; waiting", attempts))
        Sleep(2000)
        continue
    }
    ClickWithMarker(x, y)
    everClicked := true
    Sleep(3000)
    if (!InstallButtonStillVisible(winTitle)) {
        Log("Install click landed (button no longer on screen)")
        break
    }
    Log(Format("Install click attempt {}: button still visible; retrying", attempts))
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
