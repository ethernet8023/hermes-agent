// bundled-drive-update.cjs — launch the installed bundled Hermes desktop app
// under Playwright's Electron driver and perform the update the way a user
// does: click the version string "(update)" in the status bar, then click
// "Update now".
//
// This is a variant of drive-update.cjs tailored for the bundled E2E flow:
// - Handles the "Gateway offline" error screen by clicking "Use local gateway"
// - Clicks the version string "(update)" in the status bar (not Settings > About)
// - The update feed is served from a local HTTP server (not GitHub releases)
//
// Run from a directory with @playwright/test installed:
//   node bundled-drive-update.cjs <path-to-Hermes.app/exe> <proof-dir>
//
// Exit codes: 0 = update hand-off started and the app quit; 1 = any step failed.

const path = require('node:path')
const fs = require('node:fs')

const { _electron } = require('@playwright/test')

const exePath = process.argv[2]
const proofDir = process.argv[3]

if (!exePath || !proofDir) {
  console.error('usage: node bundled-drive-update.cjs <Hermes.app/exe> <proof-dir>')
  process.exit(1)
}

fs.mkdirSync(proofDir, { recursive: true })

function log(msg) {
  console.log(`[bundled-drive] ${new Date().toISOString()} ${msg}`)
}

async function shot(page, name) {
  const file = path.join(proofDir, `${name}.png`)
  try { await page.screenshot({ path: file }); log(`screenshot: ${file}`) }
  catch (err) { log(`screenshot ${name} failed: ${err.message}`) }
}

const KILL_AFTER_MS = 15 * 60 * 1000
const killer = setTimeout(() => {
  console.error('[bundled-drive] global timeout — aborting')
  process.exit(1)
}, KILL_AFTER_MS)
killer.unref()

async function main() {
  log(`launching ${exePath}`)

  // Seed 100% zoom
  const userDataDir = process.env.HERMES_DESKTOP_USER_DATA_DIR
    || (process.platform === 'win32'
      ? path.join(process.env.APPDATA || '', 'Hermes')
      : path.join(process.env.HOME || '', '.config', 'Hermes'))
  try {
    fs.mkdirSync(userDataDir, { recursive: true })
    fs.writeFileSync(path.join(userDataDir, 'zoom-state.json'), JSON.stringify({ zoomLevel: 0 }))
    log(`seeded 100% zoom`)
  } catch (e) { log(`zoom seed failed: ${e.message}`) }

  const app = await _electron.launch({
    executablePath: exePath,
    args: ['--disable-gpu', '--no-sandbox'],
    env: { ...process.env },
    timeout: 120_000
  })

  await app.firstWindow({ timeout: 120_000 })
  let page = null
  const windowDeadline = Date.now() + 120_000
  while (!page) {
    for (const candidate of app.windows()) {
      const hasUi = await candidate.evaluate(() => document.querySelector('button') !== null).catch(() => false)
      if (hasUi) { page = candidate; break }
    }
    if (!page) {
      if (Date.now() > windowDeadline) throw new Error('no window with app UI appeared within 120s')
      await new Promise(r => setTimeout(r, 1_000))
    }
  }
  log(`window picked (${app.windows().length} windows, url=${page.url()})`)

  // Wait for composer
  await page.waitForSelector('textarea, [contenteditable="true"]', { state: 'attached', timeout: 300_000 })
  log('renderer booted (composer attached)')
  await page.waitForTimeout(5000)
  await shot(page, '01-app-booted')

  // ── Dismiss the "Gateway offline" error screen ─────────────────────
  // The bundled app may show a "Could not connect to Hermes gateway" error
  // if the gateway component isn't fully configured. Click "Use local
  // gateway" or "Retry" to dismiss it.
  const gatewayPatterns = [
    () => page.getByText('Use local gateway').first(),
    () => page.getByRole('button', { name: /use local gateway/i }).first(),
    () => page.getByText('Retry').first(),
    () => page.getByRole('button', { name: /retry/i }).first(),
  ]
  for (const make of gatewayPatterns) {
    try {
      const el = make()
      if (await el.isVisible({ timeout: 2000 }).catch(() => false)) {
        await el.click({ timeout: 5000 })
        log('dismissed gateway error screen')
        await page.waitForTimeout(5000)
        await shot(page, '02-gateway-dismissed')
        break
      }
    } catch {}
  }

  // ── Dismiss onboarding overlay (if present) ─────────────────────────
  const onboardingPatterns = [
    () => page.getByRole('button', { name: /choose a provider later/i }).first(),
    () => page.getByText(/choose a provider later/i).first(),
    () => page.getByRole('button', { name: /skip/i }).first(),
    () => page.getByText(/I.ll choose/i).first(),
    () => page.getByText(/start hermes/i).first(),
    () => page.getByText(/get started/i).first(),
  ]
  for (const make of onboardingPatterns) {
    try {
      const el = make()
      if (await el.isVisible({ timeout: 1500 }).catch(() => false)) {
        await el.click({ timeout: 2000 })
        log('dismissed onboarding overlay')
        await page.waitForTimeout(2500)
        break
      }
    } catch {}
  }

  // ── Click the version string "(update)" in the status bar ───────────
  // The update UI is accessed by clicking the version string in the
  // status bar (which shows "(update)" when an update is available),
  // NOT through Settings > About.
  log('looking for version string with (update)...')
  const versionEl = page.getByText(/\(update\)/).first()
  let versionVisible = await versionEl.isVisible({ timeout: 10000 }).catch(() => false)

  if (!versionVisible) {
    log('version string not visible — the update check may not have run yet')
    // Try opening settings and looking for Check now there
    try {
      const settings = page.locator('[aria-label="Open settings"]').first()
      if (await settings.isVisible({ timeout: 3000 }).catch(() => false)) {
        await settings.click({ timeout: 5000 })
        log('opened settings as fallback')
        await page.waitForTimeout(2000)
        await shot(page, '03-settings-fallback')
      }
    } catch {}
    // Re-check version string
    versionVisible = await versionEl.isVisible({ timeout: 5000 }).catch(() => false)
  }

  if (!versionVisible) {
    await shot(page, 'ERROR-no-version-string')
    throw new Error('version string "(update)" not found — update may not be available')
  }

  log('found version string — clicking it')
  await versionEl.click({ timeout: 5000 })
  log('clicked version string')
  await page.waitForTimeout(3000)
  await shot(page, '03-version-panel')

  // ── Wait for "Update now" and click it ──────────────────────────────
  const updateNow = page.getByRole('button', { name: 'Update now' }).first()
  let updateVisible = await updateNow.isVisible({ timeout: 5000 }).catch(() => false)

  if (!updateVisible) {
    // Try "Check now"
    const checkNow = page.getByRole('button', { name: 'Check now' }).first()
    if (await checkNow.isVisible({ timeout: 3000 }).catch(() => false)) {
      await checkNow.click({ timeout: 5000 })
      log('clicked Check now')
      for (let i = 0; i < 18; i++) {
        await page.waitForTimeout(10000)
        updateVisible = await updateNow.isVisible({ timeout: 1000 }).catch(() => false)
        if (updateVisible) { log('Update now appeared!'); break }
        log(`waiting for Update now... (${i+1}/18)`)
      }
    }
  }

  if (!updateVisible) {
    await shot(page, 'ERROR-no-update-now')
    throw new Error('"Update now" never appeared')
  }

  await shot(page, '04-update-available')
  log('=== UPDATE NOW IS VISIBLE ===')

  await updateNow.click({ timeout: 10000 })
  log('=== CLICKED UPDATE NOW ===')

  await page.waitForTimeout(3000)
  await shot(page, '05-updating-overlay')

  // Wait for the app to quit (hand-off to the detached updater)
  log('waiting for hand-off...')
  await page.waitForTimeout(60000)
  await shot(page, '06-after-handoff')

  try { await app.close() } catch {}
  log('done')
}

main().catch(err => {
  console.error(`[bundled-drive] FAILED: ${err.message}`)
  process.exit(1)
})
