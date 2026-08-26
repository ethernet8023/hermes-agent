// app-updater.ts — the two-armed in-app updater for bundled desktop installs.
//
// D1-settled design: two platform arms, one renderer-facing surface.
//
//   win32  — Electron's BUILTIN autoUpdater against the MSIX release feed.
//            The artifact is an MSIX package; the builtin updater is fed a
//            per-channel feed URL via setFeedURL and drives download +
//            install itself (update-downloaded → quitAndInstall). Installs
//            that Windows manages for us — Microsoft Store deployments
//            (process.windowsStore) and stamps whose updateMechanism is
//            'external' — get NO in-app updater at all: the store/steward
//            owns the update loop, and a second updater fighting it corrupts
//            the package identity.
//   darwin — electron-updater against latest-mac.yml (per-channel feed file
//            under the release the channel resolves to).
//
// Feed URLs come from config (updates.desktop_feed_base_url) with a
// documented placeholder default: W3's R2 hosting supplies the real base
// URL. Nothing in this module hardcodes a GitHub Releases URL.
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
 *   <base>/win32/<channel>/            — MSIX feed dir (RELEASES manifest +
 *                                        .msix packages) for the builtin
 *                                        autoUpdater's setFeedURL.
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

export type UpdaterArm = 'win32-builtin' | 'darwin-electron-updater'

/**
 * Which updater arm serves `platform`. Linux bundles have no in-app
 * updater yet (AppImage replacement is a manual download) — null, and the
 * caller reports "unsupported" the same way a gated-off install does.
 */
export function selectUpdaterArm(platform: NodeJS.Platform): UpdaterArm | null {
  if (platform === 'win32') {
    return 'win32-builtin'
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
 * win32 arm: the MSIX feed URL for the builtin autoUpdater's setFeedURL.
 * One directory per channel; the RELEASES manifest inside it names the
 * newest package. The light variant feeds from its own subtree so the two
 * variants can never serve each other's packages.
 */
export function win32FeedUrl(
  baseUrl: string,
  channel: 'stable' | 'nightly',
  light: boolean
): string {
  const variant = light ? 'light/' : ''

  return `${baseUrl}/win32/${variant}${channel}/`
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

// ─── win32 arm (builtin autoUpdater, MSIX feed) ─────────────────────────────

/**
 * The slice of Electron's builtin autoUpdater the win32 arm consumes.
 * Injectable so vitest covers the arm without an Electron runtime.
 */
export interface BuiltinAutoUpdater {
  setFeedURL(options: { url: string }): void
  checkForUpdates(): void
  quitAndInstall(): void
  on(event: string, listener: (...args: unknown[]) => void): unknown
  removeListener(event: string, listener: (...args: unknown[]) => void): unknown
}

let cachedBuiltin: BuiltinAutoUpdater | null = null

/** Lazy singleton for Electron's builtin autoUpdater (win32 arm). */
export function getBuiltinAutoUpdater(): BuiltinAutoUpdater {
  if (cachedBuiltin) {
    return cachedBuiltin
  }

  const { autoUpdater } = require('electron') as { autoUpdater: BuiltinAutoUpdater }

  cachedBuiltin = autoUpdater

  return autoUpdater
}

/**
 * win32 arm: point the builtin autoUpdater at the channel's MSIX feed and
 * run one check. The builtin updater has no promise API — it announces the
 * outcome through events — so this wraps one check cycle in a promise:
 * `update-available`/`update-not-available` resolve the availability
 * question, `error` rejects, and every listener comes off in all cases
 * (the updater is a process-wide singleton; leaked listeners fire on the
 * NEXT check and double-report).
 *
 * The builtin updater downloads in the background once it announces
 * update-available; `update-downloaded` carries the new version. The
 * caller applies it later via quitAndInstall (applyAppUpdate).
 */
export function checkWin32Update(
  feedUrl: string,
  updater: BuiltinAutoUpdater = getBuiltinAutoUpdater()
): Promise<{ updateAvailable: boolean; version: string | null }> {
  updater.setFeedURL({ url: feedUrl })

  return new Promise((resolve, reject) => {
    const cleanup = () => {
      updater.removeListener('update-available', onAvailable)
      updater.removeListener('update-not-available', onNotAvailable)
      updater.removeListener('error', onError)
    }

    const onAvailable = () => {
      cleanup()
      resolve({ updateAvailable: true, version: null })
    }

    const onNotAvailable = () => {
      cleanup()
      resolve({ updateAvailable: false, version: null })
    }

    const onError = (...args: unknown[]) => {
      cleanup()
      reject(args[0] instanceof Error ? args[0] : new Error(String(args[0])))
    }

    updater.on('update-available', onAvailable)
    updater.on('update-not-available', onNotAvailable)
    updater.on('error', onError)
    updater.checkForUpdates()
  })
}

/**
 * win32 arm: wait for the background download to finish, run the caller's
 * teardown, then hand the process to the installer. `beforeInstall` runs
 * between `update-downloaded` and quitAndInstall — the caller uses it for
 * backend teardown that must happen while the process is alive (a
 * surviving backend grandchild keeps files in the install directory
 * locked while the package swaps).
 */
export function applyWin32Update(
  beforeInstall?: () => void | Promise<void>,
  updater: BuiltinAutoUpdater = getBuiltinAutoUpdater()
): Promise<{ ok: true }> {
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      updater.removeListener('update-downloaded', onDownloaded)
      updater.removeListener('error', onError)
    }

    const onDownloaded = async () => {
      cleanup()

      try {
        if (beforeInstall) {
          await beforeInstall()
        }
      } catch (error) {
        reject(error instanceof Error ? error : new Error(String(error)))

        return
      }

      updater.quitAndInstall()
      resolve({ ok: true })
    }

    const onError = (...args: unknown[]) => {
      cleanup()
      reject(args[0] instanceof Error ? args[0] : new Error(String(args[0])))
    }

    updater.on('update-downloaded', onDownloaded)
    updater.on('error', onError)
  })
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
