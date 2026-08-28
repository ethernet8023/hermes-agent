// app-updater.ts — the update arms for bundled desktop installs.
//
// D1-settled design: platform arms, one renderer-facing surface.
//
//   win32  — the OS App Installer owns the apply. The package was installed
//            from an .appinstaller, which registered the feed URI as the
//            package's update source; the OS checks it and swaps the package
//            wholesale. The app's only job is the checker: ask the OS whether
//            an update is available (via the bundled payload python's winrt),
//            show its own prompt, run graceful teardown, then trigger
//            ms-appinstaller: and quit. Installations that Windows manages
//            for us — Microsoft Store deployments (process.windowsStore) and
//            stamps whose updateMechanism is 'external' — get NO in-app
//            updater at all: the store/steward owns the update loop.
//   darwin — electron-updater against latest-mac.yml (per-channel feed file
//            under the release the channel resolves to).
//
// Source installs never reach this module. The callers gate on the install
// stamp first and fall through to the git-based update path.
//
// The decision helpers are pure so vitest covers them. The impure wrappers
// at the bottom lazy-load electron / electron-updater, because the module
// must not cost anything on thin builds.

import type { AppUpdater } from 'electron-updater'

import type { ArtifactKind } from './install-stamp'
import type { UpdateChannel } from './payload-backend'

// ─── feed hosting ───────────────────────────────────────────────────────────

/**
 * PLACEHOLDER — the default base URL of the desktop release feeds.
 *
 * W3 (R2 hosting) supplies the real value; until it lands, this default is
 * a documented dead end that `resolveFeedBaseUrl` lets config override
 * (`updates.desktop_feed_base_url` in config.yaml). Layout under the base:
 *
 *   <base>/win32/<channel>/            — App Installer feed dir (.appinstaller
 *                                        + .msixbundle) for out-of-store MSIX.
 *   <base>/darwin/<channel>/latest-mac.yml
 *                                      — electron-updater feed file + dmg/zip.
 *
 * `<channel>` is 'stable' or 'nightly' ('main' never reaches a bundled
 * artifact — resolveUpdaterChannel folds it to 'stable' for feed purposes).
 */
export const PLACEHOLDER_FEED_BASE_URL = 'https://updates.invalid/hermes-desktop'

/**
 * The feed base URL: the config override when present, else the
 * placeholder. `configuredBaseUrl` is the raw value of
 * `updates.desktop_feed_base_url` from config.yaml (the caller reads it;
 * this stays pure). Trailing slashes are trimmed so feed paths join
 * predictably.
 */
export function resolveFeedBaseUrl(configuredBaseUrl: string | null | undefined): string {
  const base = typeof configuredBaseUrl === 'string' && configuredBaseUrl.trim() !== ''
    ? configuredBaseUrl.trim()
    : PLACEHOLDER_FEED_BASE_URL

  return base.replace(/\/+$/, '')
}

// ─── gate + arm selection ───────────────────────────────────────────────────

export interface UpdaterGateFacts {
  stampPayload: ArtifactKind
  isPackaged: boolean
  /** The stamp's updateMechanism. 'external' → the steward owns updates. */
  mechanism: string | null
  /** Electron's process.windowsStore: true for Microsoft Store deployments. */
  windowsStore: boolean
}

/**
 * True when this launch must run an in-app updater.
 *
 * All conditions are necessary:
 * - the artifact kind self-updates through a release feed: 'bundled' and
 *   'light' both do ('bootstrap' artifacts have no matching feed artifacts
 *   and keep the git path),
 * - the app is packaged (dev runs have no feed identity),
 * - the stamp says electron owns updates ('external' means a steward —
 *   store, package manager — applies them),
 * - the install is not Microsoft-Store-managed: a store-deployed MSIX is
 *   updated by the Store, and an in-app updater would fight it.
 */
export function shouldUseAppUpdater(facts: UpdaterGateFacts): boolean {
  if (facts.stampPayload !== 'bundled' && facts.stampPayload !== 'light') {
    return false
  }

  if (!facts.isPackaged) {
    return false
  }

  if (facts.mechanism === 'external') {
    return false
  }

  if (facts.windowsStore) {
    return false
  }

  return true
}

export type UpdaterArm = 'win32-app-installer' | 'darwin-electron-updater'

/**
 * Which updater arm serves `platform`. Linux bundles have no in-app
 * updater yet (AppImage replacement is a manual download) — null, and the
 * caller reports "unsupported" the same way a gated-off install does.
 */
export function selectUpdaterArm(platform: NodeJS.Platform): UpdaterArm | null {
  if (platform === 'win32') {
    return 'win32-app-installer'
  }

  if (platform === 'darwin') {
    return 'darwin-electron-updater'
  }

  return null
}

// ─── channel → feed selection ───────────────────────────────────────────────

/**
 * The channel the updater actually feeds from. Bundled artifacts only have
 * two feeds — 'main' is a source-checkout concept (a git branch, not a
 * release feed), so a record that says main folds to stable here.
 */
export function resolveUpdaterChannel(channel: UpdateChannel): 'stable' | 'nightly' {
  return channel === 'nightly' ? 'nightly' : 'stable'
}

/**
 * The App Installer feed dir for a channel. The .appinstaller for a channel
 * lives under this dir; the OS re-reads it on launch (HoursBetweenUpdateChecks)
 * and swaps the bundle when a newer version is there. The light variant feeds
 * from its own subtree so the two variants can never serve each other's
 * packages.
 */
export function win32AppInstallerFeedPath(
  channel: 'stable' | 'nightly',
  light: boolean
): string {
  const variant = light ? 'light/' : ''
  return `win32/${variant}${channel}/`
}

/**
 * darwin arm: the electron-updater feed selection for a channel — which
 * `<feed>.yml` file (latest-mac.yml for stable, nightly-mac.yml for
 * nightly; the light variant prefixes its own name), and whether
 * prereleases are eligible.
 *
 * `channel` must always be an explicit feed name, never null. null means
 * "no override", and the provider then falls back to the channel baked
 * into app-update.yml — on a nightly artifact that is 'nightly', and with
 * `allowPrerelease: false` the provider reads the feed file from the
 * newest STABLE release, where nightly-mac.yml does not exist: a 404 with
 * no fallback. allowPrerelease also selects how the release is chosen:
 * true walks the releases feed for the newest matching prerelease, false
 * takes the latest stable release. The two must always move together.
 */
export function feedSelection(
  channel: 'stable' | 'nightly',
  light: boolean
): { channel: string; allowPrerelease: boolean } {
  if (channel === 'nightly') {
    return { channel: light ? 'light-nightly' : 'nightly', allowPrerelease: true }
  }

  return { channel: light ? 'light' : 'latest', allowPrerelease: false }
}

// ─── renderer-facing result shape ───────────────────────────────────────────

export interface FeedCheckResult {
  supported: boolean
  mechanism: 'app-updater'
  arm: UpdaterArm | null
  channel: 'stable' | 'nightly'
  currentVersion: string
  latestVersion: string | null
  latestTag: string | null
  updateAvailable: boolean
  fetchedAt: number
}

/**
 * Map an updater check result to the renderer's update-check shape (the
 * shape hermes:updates:check already returns for the git path). The
 * renderer then needs no new states: `updateAvailable` plus `mechanism`
 * drive the existing UI.
 */
export function describeFeedCheck(
  arm: UpdaterArm | null,
  current: string,
  info: { version?: string } | null | undefined,
  isUpdateAvailable?: boolean,
  channel: 'stable' | 'nightly' = 'stable'
): FeedCheckResult {
  const latest = info && typeof info.version === 'string' ? info.version : null

  return {
    supported: arm !== null,
    mechanism: 'app-updater',
    arm,
    // The channel this bundled install tracks. Saying so here lets every
    // renderer surface pick release vocabulary without a separate probe.
    channel,
    currentVersion: current,
    latestVersion: latest,
    latestTag: latest ? `v${latest}` : null,
    // Prefer the updater's own semver verdict: a plain string compare
    // would offer a locally-newer dev build a downgrade.
    updateAvailable: isUpdateAvailable ?? (latest !== null && latest !== current),
    fetchedAt: Date.now()
  }
}

// ─── win32 arm (OS App Installer checker + trigger) ────────────────────────

/** The result of an App Installer update check. */
export interface AppInstallerCheck {
  /** null when the check could not run (no winrt, not packaged, error). */
  available: boolean | null
  /** Human-readable availability string from the OS, when reported. */
  availability?: string
  error?: string
}

/**
 * The payload-python invocation the win32 arm needs. Injectable so vitest
 * covers the arm without a payload.
 */
export interface PayloadPythonRunner {
  /** Absolute path to the bundled payload python (tools/<entry>/python.exe). */
  python: string
  /** The checker script's absolute path. */
  script: string
  /** Run the script; resolve with {code, stdout}. */
  run: (python: string, script: string) => Promise<{ code: number; stdout: string }>
}

/**
 * win32 arm: ask the OS whether an App Installer update is available, via
 * the bundled payload python's winrt (PackageManager.CheckPackageUpdateAvailabilityAsync).
 * The OS compares the package's registered .appinstaller source against the
 * installed version; it does NOT download anything.
 */
export async function checkAppInstallerUpdate(
  runner: PayloadPythonRunner
): Promise<AppInstallerCheck> {
  const { code, stdout } = await runner.run(runner.python, runner.script)
  const text = stdout.trim()
  let parsed: { available?: boolean | null; availability?: string; error?: string; reason?: string } | null = null
  try {
    parsed = text ? JSON.parse(text) : null
  } catch {
    parsed = null
  }

  if (parsed && typeof parsed.available === 'boolean') {
    return { available: parsed.available, availability: parsed.availability, error: parsed.error }
  }
  if (parsed && parsed.available === null) {
    return { available: null, error: parsed.error || 'checker returned unknown' }
  }
  if (code !== 0) {
    return { available: null, error: parsed?.error || `checker exited ${code}` }
  }
  return { available: null, error: 'checker returned no availability' }
}

/**
 * win32 arm: trigger the OS App Installer to apply the update and quit.
 * `ms-appinstaller:?source=<feed .appinstaller URL>` makes the OS re-read the
 * package's update source and install the newer bundle; the app then exits so
 * the package swap is not contested. `beforeInstall` runs first (backend
 * teardown while the process is still alive).
 */
export async function triggerAppInstallerUpdate(
  feedBaseUrl: string,
  channel: 'stable' | 'nightly',
  light: boolean,
  shell: { openExternal: (url: string) => Promise<void> },
  beforeInstall?: () => void | Promise<void>
): Promise<{ ok: true }> {
  if (beforeInstall) {
    await beforeInstall()
  }

  const appinstallerUrl =
    `${feedBaseUrl.replace(/\/+$/, '')}/${win32AppInstallerFeedPath(channel, light)}${channel}.appinstaller`
  await shell.openExternal(`ms-appinstaller:?source=${encodeURIComponent(appinstallerUrl)}`)

  return { ok: true }
}

// ─── darwin arm (electron-updater) ──────────────────────────────────────────

let cachedUpdater: AppUpdater | null = null

/**
 * Lazy singleton for electron-updater's autoUpdater (darwin arm). The
 * require sits inside the function so thin builds and tests never pay for
 * the module load. autoDownload stays off: the renderer asks the user
 * before the download starts (same consent model as the git path).
 * autoInstallOnAppQuit stays off too: a quit-time install would skip the
 * pre-install backend teardown in applyDarwinUpdate.
 */
export function getElectronUpdater(): AppUpdater {
  if (cachedUpdater) {
    return cachedUpdater
  }

  const { autoUpdater } = require('electron-updater') as { autoUpdater: AppUpdater }

  autoUpdater.autoDownload = false
  autoUpdater.autoInstallOnAppQuit = false
  cachedUpdater = autoUpdater

  return autoUpdater
}

/** darwin arm: check the latest-mac.yml feed for the resolved channel. */
export async function checkDarwinUpdate(
  channel: 'stable' | 'nightly',
  light: boolean,
  updater: AppUpdater = getElectronUpdater()
): Promise<{ updateAvailable: boolean; version: string | null }> {
  // Set on every check: the user can flip the per-install record between
  // two checks of one app session.
  const feed = feedSelection(channel, light)

  updater.channel = feed.channel
  updater.allowPrerelease = feed.allowPrerelease

  const result = await updater.checkForUpdates()
  const version = result?.updateInfo && typeof result.updateInfo.version === 'string'
    ? result.updateInfo.version
    : null

  return { updateAvailable: result?.isUpdateAvailable ?? false, version }
}

/**
 * darwin arm: download the update, then quit and install. `onProgress`
 * receives percent values from electron-updater's download events.
 * `beforeInstall` runs after the download completes and before
 * quitAndInstall — backend teardown while the process is still alive.
 *
 * `updater` is injectable so vitest can assert the ordering contract
 * (download → beforeInstall → quitAndInstall) without electron-updater.
 */
export async function applyDarwinUpdate(
  onProgress?: (percent: number) => void,
  beforeInstall?: () => void | Promise<void>,
  updater: AppUpdater = getElectronUpdater()
): Promise<{ ok: true }> {
  const handler = onProgress ? (p: { percent: number }) => onProgress(p.percent) : null

  if (handler) {
    updater.on('download-progress', handler)
  }

  // The listener must come off on failure too: the updater is a process-wide
  // singleton, and a retry after a failed download would stack a second
  // listener that fires ghost progress events.
  try {
    await updater.downloadUpdate()
  } finally {
    if (handler) {
      updater.removeListener('download-progress', handler)
    }
  }

  if (beforeInstall) {
    await beforeInstall()
  }

  updater.quitAndInstall()

  return { ok: true }
}
