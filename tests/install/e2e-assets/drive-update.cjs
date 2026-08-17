// drive-update.cjs — launch the INSTALLED Hermes.exe (real Electron desktop
// app) under Playwright's Electron driver and perform the update the way a
// user does: Settings -> About -> "Update now". Screenshots at every step.
//
// Run from the installed checkout's apps/desktop directory (so
// @playwright/test resolves from ITS node_modules — the same deps the
// installed app was built with):
//
//   node <this file> <path-to-Hermes.exe> <proof-dir>
//
// Exit codes: 0 = update hand-off started and the app quit (the detached
// updater takes it from there — the PowerShell driver polls for the result);
// 1 = any step failed. The driver treats nonzero as leg failure.
//
// This intentionally does NOT call any store/bridge function directly: only
// real clicks on the real UI, so a regression in the button, the About
// panel, the overlay, or the renderer->main bridge fails the test.

const path = require('node:path')
const fs = require('node:fs')

const { _electron } = require('@playwright/test')

const exePath = process.argv[2]
const proofDir = process.argv[3]

if (!exePath || !proofDir) {
  console.error('usage: node drive-update.cjs <Hermes.exe> <proof-dir>')
  process.exit(1)
}

fs.mkdirSync(proofDir, { recursive: true })

function log(msg) {
  console.log(`[drive-update] ${new Date().toISOString()} ${msg}`)
}

async function shot(page, name) {
  const file = path.join(proofDir, `${name}.png`)

  try {
    await page.screenshot({ path: file })
    log(`screenshot: ${file}`)
  } catch (err) {
    log(`screenshot ${name} failed: ${err.message}`)
  }
}

// Hard ceiling so a hung renderer can't wedge the CI job; the driver's own
// step timeout is the real guard, this is belt-and-braces.
const KILL_AFTER_MS = 15 * 60 * 1000
const killer = setTimeout(() => {
  console.error('[drive-update] global timeout — aborting')
  process.exit(1)
}, KILL_AFTER_MS)
killer.unref()

async function clickFirstVisible(page, locators, description, timeoutMs) {
  const deadline = Date.now() + timeoutMs

  for (;;) {
    for (const make of locators) {
      const locator = make(page).first()

      try {
        if (await locator.isVisible()) {
          await locator.click()
          log(`clicked: ${description}`)

          return true
        }
      } catch {
        // locator invalid in this state; try the next
      }
    }
    if (Date.now() > deadline) {
      return false
    }
    await page.waitForTimeout(500)
  }
}

async function main() {
  log(`launching ${exePath}`)

  const app = await _electron.launch({
    executablePath: exePath,
    args: ['--disable-gpu', '--no-sandbox'],
    // Inherit the driver's env: HERMES_HOME (isolated install) and
    // GIT_CONFIG_GLOBAL (URL redirect to the staged serve repo) MUST reach
    // the main process so its update check fetches from the staged repo.
    env: { ...process.env },
    timeout: 120_000
  })

  const page = await app.firstWindow({ timeout: 120_000 })
  log('first window acquired')

  // Boot: wait for the composer to exist — the shell is mounted by then.
  // The real backend (`hermes serve`) is booting underneath; give it time.
  await page.waitForSelector('textarea, [contenteditable="true"]', {
    state: 'attached',
    timeout: 300_000
  })
  log('renderer booted (composer attached)')
  await page.waitForTimeout(3000)
  await shot(page, '01-app-booted')

  // ── Reach Settings through the onboarding overlay ─────────────────────
  // A fresh install with no configured provider shows the onboarding card
  // ("Let's get you setup..."). It mounts LATE and in phases: first a
  // buttonless boot-progress card ("Starting Hermes... 86%"), then the
  // provider picker with "I'll choose a provider later" (proof:
  // 01-app-booted.png vs ERROR-no-settings-button.png in run 31839406540).
  // Two traps: a one-shot dismiss probe fires before the picker's button
  // exists, and isVisible() on the settings gear reports true while the
  // gear sits UNDER the fullscreen overlay that intercepts every click.
  // So: alternate short-timeout dismiss clicks with short-timeout settings
  // clicks until a settings click actually LANDS - Playwright's click
  // checks the hit target, so a landed click is proof the overlay is gone.
  // Harmless when onboarding never shows: the first settings click lands.
  const laterLocators = [
    p => p.getByRole('button', { name: /choose a provider later/i }),
    p => p.getByText(/choose a provider later/i),
    p => p.getByRole('button', { name: /skip/i })
  ]
  const settingsLocators = [
    p => p.getByLabel('Open settings'),
    p => p.locator('[aria-label="Open settings"]'),
    p => p.locator('[title="Open settings"]'),
    p => p.getByRole('button', { name: 'Open settings' })
  ]

  const overlayDeadline = Date.now() + 180_000
  let openedSettings = false
  const brief = e => String((e && e.message) || e).split('\n').slice(0, 3).join(' | ')
  let iter = 0

  while (!openedSettings) {
    iter++
    for (const make of laterLocators) {
      try {
        await make(page).first().click({ timeout: 1_500 })
        log('dismissed onboarding overlay')
        await page.waitForTimeout(2500)
        await shot(page, '01b-onboarding-dismissed')
        break
      } catch (e) {
        log(`[overlay] iter ${iter} dismiss click failed: ${brief(e)}`)
      }
    }
    for (const make of settingsLocators) {
      try {
        await make(page).first().click({ timeout: 2_500 })
        openedSettings = true
        log('clicked: Open settings')
        break
      } catch (e) {
        log(`[overlay] iter ${iter} settings click failed: ${brief(e)}`)
      }
    }
    if (!openedSettings && Date.now() > overlayDeadline) break
  }

  if (!openedSettings) {
    await shot(page, 'ERROR-no-settings-button')
    throw new Error('could not find the Open settings control')
  }
  await page.waitForTimeout(1500)
  await shot(page, '02-settings-open')

  // ── Go to the About section ───────────────────────────────────────────
  const openedAbout = await clickFirstVisible(
    page,
    [
      p => p.getByRole('tab', { name: 'About' }),
      p => p.getByRole('button', { name: 'About' }),
      p => p.getByText('About', { exact: true })
    ],
    'About section',
    30_000
  )

  if (!openedAbout) {
    await shot(page, 'ERROR-no-about-tab')
    throw new Error('could not find the About section in Settings')
  }
  await page.waitForTimeout(1500)
  await shot(page, '03-about-panel')

  // ── Wait for "Update now" (appears when behind > 0) ──────────────────
  // checkUpdates() runs at boot; if its result hasn't landed yet, press
  // "Check now" like an impatient user would.
  const updateNow = page.getByRole('button', { name: 'Update now' }).first()
  let visible = await updateNow.isVisible().catch(() => false)

  if (!visible) {
    log('Update now not visible yet — clicking Check now')
    await clickFirstVisible(page, [p => p.getByRole('button', { name: 'Check now' })], 'Check now', 20_000)

    try {
      await updateNow.waitFor({ state: 'visible', timeout: 120_000 })
      visible = true
    } catch {
      visible = false
    }
  }

  if (!visible) {
    await shot(page, 'ERROR-no-update-now')
    throw new Error('"Update now" never appeared — update check did not report behind > 0')
  }
  await shot(page, '04-update-available')

  // ── The click under test ──────────────────────────────────────────────
  await updateNow.click()
  log('clicked: Update now')

  // The "Updating Hermes — this window will close" overlay should appear,
  // then the app quits (hand-off dwell). Screenshot the overlay while the
  // window is still alive.
  await page.waitForTimeout(1200)
  await shot(page, '05-updating-overlay')

  // ── Wait for the hand-off to take over ────────────────────────────────
  // Clicking Update now spawns the detached updater (desktop-update.ps1 or
  // the staged binary), which claims HERMES_HOME/.hermes-update-in-progress
  // and then the desktop quits. We do NOT rely on Playwright's app 'close'
  // event: when the app self-quits for the hand-off that event is
  // unreliable (attempt 8 timed out on it even though the hand-off log
  // proved the desktop had exited and `hermes update` was already running).
  //
  // The authoritative "hand-off started" signal is the marker file (or the
  // result JSON, if the whole update finished fast). Poll for either, and
  // also accept a genuine app close. Any one is success — the PowerShell
  // driver owns asserting the update's OUTCOME (sha, marker cleanup,
  // relaunch) after we return.
  const hermesHome = process.env.HERMES_HOME
  const markerPath = hermesHome ? path.join(hermesHome, '.hermes-update-in-progress') : null
  const resultPath = hermesHome ? path.join(hermesHome, '.hermes-update-result.json') : null

  let appClosed = false
  app.on('close', () => {
    appClosed = true
  })

  const handoffDeadline = Date.now() + 150_000
  let handoffStarted = false

  while (Date.now() < handoffDeadline) {
    if (markerPath && fs.existsSync(markerPath)) {
      log('hand-off marker present — updater has taken over')
      handoffStarted = true
      break
    }
    if (resultPath && fs.existsSync(resultPath)) {
      log('update result JSON already present — updater finished fast')
      handoffStarted = true
      break
    }
    if (appClosed) {
      log('app closed — hand-off in progress')
      handoffStarted = true
      break
    }
    // Secondary: if the renderer window is gone, evaluate throws.
    try {
      await page.evaluate(() => true)
    } catch {
      log('renderer window gone — app quit for hand-off')
      handoffStarted = true
      break
    }
    await new Promise(r => setTimeout(r, 2000))
  }

  if (!handoffStarted) {
    await shot(page, 'ERROR-no-handoff')
    throw new Error('no hand-off within 150s of Update now (no marker, no result, app still alive)')
  }

  log('hand-off confirmed — detached updater owns the rest')
}

main()
  .then(() => process.exit(0))
  .catch(err => {
    console.error(`[drive-update] FAILED: ${err.message}`)
    process.exit(1)
  })
