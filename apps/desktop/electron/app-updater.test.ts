import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  applyDarwinUpdate,
  checkAppInstallerUpdate,
  describeFeedCheck,
  feedSelection,
  PLACEHOLDER_FEED_BASE_URL,
  resolveFeedBaseUrl,
  resolveUpdaterChannel,
  selectUpdaterArm,
  shouldUseAppUpdater,
  triggerAppInstallerUpdate,
  win32AppInstallerFeedPath
} from './app-updater'

// ── gate ────────────────────────────────────────────────────────────

const baseFacts = {
  stampPayload: 'bundled' as const,
  isPackaged: true,
  mechanism: 'electron-updater',
  windowsStore: false
}

test('app updater runs for packaged embedded builds', () => {
  assert.equal(shouldUseAppUpdater(baseFacts), true)
})

test('app updater runs for packaged light builds', () => {
  assert.equal(shouldUseAppUpdater({ ...baseFacts, stampPayload: 'light' }), true)
})

test('a bootstrap build never uses the app updater', () => {
  assert.equal(shouldUseAppUpdater({ ...baseFacts, stampPayload: 'bootstrap' }), false)
})

test('dev runs never use the app updater', () => {
  assert.equal(shouldUseAppUpdater({ ...baseFacts, isPackaged: false }), false)
  assert.equal(shouldUseAppUpdater({ ...baseFacts, stampPayload: 'light', isPackaged: false }), false)
})

test('store-managed MSIX installs get no in-app updater', () => {
  // The Microsoft Store owns the update loop for store deployments —
  // process.windowsStore is the detector, and an in-app updater fighting
  // the Store corrupts the package identity.
  assert.equal(shouldUseAppUpdater({ ...baseFacts, windowsStore: true }), false)
})

test('externally-managed stamps get no in-app updater', () => {
  assert.equal(shouldUseAppUpdater({ ...baseFacts, mechanism: 'external' }), false)
})

// ── arm selection ───────────────────────────────────────────────────

test('each platform gets its settled arm', () => {
  assert.equal(selectUpdaterArm('win32'), 'win32-app-installer')
  assert.equal(selectUpdaterArm('darwin'), 'darwin-electron-updater')
  assert.equal(selectUpdaterArm('linux'), null)
})

// ── channel folding + feed paths ───────────────────────────────────

test('main folds to stable for feed purposes; nightly stays', () => {
  // Bundled artifacts only have two feeds — 'main' is a git-branch
  // concept and never names a release feed.
  assert.equal(resolveUpdaterChannel('main'), 'stable')
  assert.equal(resolveUpdaterChannel('stable'), 'stable')
  assert.equal(resolveUpdaterChannel('nightly'), 'nightly')
})

test('the feed base URL comes from config, defaulting to the documented placeholder', () => {
  assert.equal(resolveFeedBaseUrl(null), PLACEHOLDER_FEED_BASE_URL)
  assert.equal(resolveFeedBaseUrl(''), PLACEHOLDER_FEED_BASE_URL)
  assert.equal(resolveFeedBaseUrl('  '), PLACEHOLDER_FEED_BASE_URL)
  assert.equal(resolveFeedBaseUrl('https://r2.example.com/feeds/'), 'https://r2.example.com/feeds')
})

test('win32 App Installer feed paths are per-channel and per-variant', () => {
  assert.equal(win32AppInstallerFeedPath('stable', false), 'win32/stable/')
  assert.equal(win32AppInstallerFeedPath('nightly', false), 'win32/nightly/')
  assert.equal(win32AppInstallerFeedPath('stable', true), 'win32/light/stable/')
  assert.equal(win32AppInstallerFeedPath('nightly', true), 'win32/light/nightly/')
})

// ── darwin feedSelection ────────────────────────────────────────────

test('every channel names its feed file explicitly', () => {
  // The regression class: a null channel falls back to the channel baked
  // into app-update.yml — on a nightly artifact that is 'nightly', and a
  // stable-channel check then asks for the nightly feed file under the
  // newest STABLE release: 404 with no retry.
  for (const light of [false, true]) {
    for (const channel of ['stable', 'nightly'] as const) {
      const feed = feedSelection(channel, light)

      assert.ok(feed.channel, `${channel}/light=${light} must name a feed`)
      assert.equal(typeof feed.channel, 'string')
    }
  }
})

test('feed names are per-variant so the two variants never share a feed', () => {
  assert.equal(feedSelection('stable', false).channel, 'latest')
  assert.equal(feedSelection('nightly', false).channel, 'nightly')
  assert.equal(feedSelection('stable', true).channel, 'light')
  assert.equal(feedSelection('nightly', true).channel, 'light-nightly')
})

test('only the nightly channel accepts prereleases', () => {
  // allowPrerelease also picks which release the feed file is read FROM: a
  // nightly feed must be paired with the prerelease walk, or it looks for
  // its feed file under the newest stable release.
  assert.equal(feedSelection('nightly', false).allowPrerelease, true)
  assert.equal(feedSelection('nightly', true).allowPrerelease, true)
  assert.equal(feedSelection('stable', false).allowPrerelease, false)
  assert.equal(feedSelection('stable', true).allowPrerelease, false)
})

// ── describeFeedCheck ───────────────────────────────────────────────

test('feed check reports an available update when versions differ', () => {
  const out = describeFeedCheck('darwin-electron-updater', '0.17.0', { version: '0.18.0' })

  assert.equal(out.supported, true)
  assert.equal(out.mechanism, 'app-updater')
  assert.equal(out.arm, 'darwin-electron-updater')
  assert.equal(out.channel, 'stable')
  assert.equal(out.currentVersion, '0.17.0')
  assert.equal(out.latestVersion, '0.18.0')
  assert.equal(out.latestTag, 'v0.18.0')
  assert.equal(out.updateAvailable, true)
  assert.ok(out.fetchedAt > 0)
})

test('feed check reports up to date when versions match', () => {
  const out = describeFeedCheck('win32-app-installer', '0.17.0', { version: '0.17.0' })

  assert.equal(out.updateAvailable, false)
  assert.equal(out.latestVersion, '0.17.0')
})

test('feed check tolerates a missing update info payload', () => {
  const out = describeFeedCheck('win32-app-installer', '0.17.0', null)

  assert.equal(out.updateAvailable, false)
  assert.equal(out.latestVersion, null)
})

test('a null arm reports unsupported', () => {
  assert.equal(describeFeedCheck(null, '0.17.0', null).supported, false)
})

// ── win32 arm (OS App Installer checker + trigger) ─────────────────

function fakePayloadRunner(stdout: string, code = 0) {
  const calls: string[] = []
  return {
    runner: {
      python: 'C:\\payload\\tools\\cpython\\python.exe',
      script: 'C:\\payload\\scripts\\check-appinstaller-update.py',
      run: async (python: string, script: string) => {
        calls.push(`${python} ${script}`)
        return { code, stdout }
      }
    } as any,
    calls
  }
}

test('win32 check reports available when the OS says a newer package exists', async () => {
  const { runner } = fakePayloadRunner(JSON.stringify({ available: true, availability: 'Available' }))
  const result = await checkAppInstallerUpdate(runner)

  assert.equal(result.available, true)
  assert.equal(result.availability, 'Available')
})

test('win32 check reports not-available cleanly', async () => {
  const { runner } = fakePayloadRunner(JSON.stringify({ available: false, availability: 'NoApplicableUpdate' }))
  const result = await checkAppInstallerUpdate(runner)

  assert.equal(result.available, false)
})

test('win32 check surfaces an unknown verdict (missing winrt) without crashing', async () => {
  const { runner } = fakePayloadRunner(JSON.stringify({ available: null, error: 'winrt import failed' }), 1)
  const result = await checkAppInstallerUpdate(runner)

  assert.equal(result.available, null)
  assert.match(result.error || '', /winrt import failed/)
})

test('win32 trigger opens ms-appinstaller with the channel .appinstaller after teardown', async () => {
  const calls: string[] = []
  const shell = {
    openExternal: async (url: string) => void calls.push(`open:${url}`)
  }

  const result = await triggerAppInstallerUpdate(
    'https://updates.example.com/',
    'stable',
    false,
    shell as any,
    () => void calls.push('teardown')
  )

  assert.equal(result.ok, true)
  assert.deepEqual(calls, [
    'teardown',
    'open:ms-appinstaller:?source=https%3A%2F%2Fupdates.example.com%2Fwin32%2Fstable%2Fstable.appinstaller'
  ])
})

test('win32 trigger light+nightly targets the light nightly feed dir', async () => {
  const calls: string[] = []
  const shell = {
    openExternal: async (url: string) => void calls.push(url)
  }

  await triggerAppInstallerUpdate('https://updates.example.com', 'nightly', true, shell as any)

  assert.equal(calls[0], 'ms-appinstaller:?source=https%3A%2F%2Fupdates.example.com%2Fwin32%2Flight%2Fnightly%2Fnightly.appinstaller')
})

// ── darwin arm (electron-updater) ───────────────────────────────────

function fakeElectronUpdater(calls: string[], failDownload = false) {
  return {
    on: () => void 0,
    removeListener: () => void 0,
    downloadUpdate: async () => {
      calls.push('download')

      if (failDownload) {
        throw new Error('download failed')
      }
    },
    quitAndInstall: () => void calls.push('install')
  } as any
}

test('darwin apply runs beforeInstall between the download and the install', async () => {
  const calls: string[] = []

  await applyDarwinUpdate(undefined, () => void calls.push('teardown'), fakeElectronUpdater(calls))

  assert.deepEqual(calls, ['download', 'teardown', 'install'])
})

test('a failed darwin download installs nothing and skips beforeInstall', async () => {
  const calls: string[] = []

  await assert.rejects(
    applyDarwinUpdate(undefined, () => void calls.push('teardown'), fakeElectronUpdater(calls, true))
  )

  assert.deepEqual(calls, ['download'])
})
