import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  PLACEHOLDER_FEED_BASE_URL,
  applyDarwinUpdate,
  applyWin32Update,
  checkWin32Update,
  describeFeedCheck,
  feedSelection,
  resolveFeedBaseUrl,
  resolveUpdaterChannel,
  selectUpdaterArm,
  shouldUseAppUpdater,
  win32FeedUrl
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
  assert.equal(selectUpdaterArm('win32'), 'win32-builtin')
  assert.equal(selectUpdaterArm('darwin'), 'darwin-electron-updater')
  assert.equal(selectUpdaterArm('linux'), null)
})

// ── channel folding + feed URLs ─────────────────────────────────────

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

test('win32 MSIX feed URLs are per-channel and per-variant', () => {
  const base = 'https://r2.example.com/feeds'

  assert.equal(win32FeedUrl(base, 'stable', false), 'https://r2.example.com/feeds/win32/stable/')
  assert.equal(win32FeedUrl(base, 'nightly', false), 'https://r2.example.com/feeds/win32/nightly/')
  assert.equal(win32FeedUrl(base, 'stable', true), 'https://r2.example.com/feeds/win32/light/stable/')
  assert.equal(win32FeedUrl(base, 'nightly', true), 'https://r2.example.com/feeds/win32/light/nightly/')
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
  const out = describeFeedCheck('win32-builtin', '0.17.0', { version: '0.17.0' })

  assert.equal(out.updateAvailable, false)
  assert.equal(out.latestVersion, '0.17.0')
})

test('feed check tolerates a missing update info payload', () => {
  const out = describeFeedCheck('win32-builtin', '0.17.0', null)

  assert.equal(out.updateAvailable, false)
  assert.equal(out.latestVersion, null)
})

test('a null arm reports unsupported', () => {
  assert.equal(describeFeedCheck(null, '0.17.0', null).supported, false)
})

// ── win32 arm (builtin autoUpdater) ─────────────────────────────────

type Listener = (...args: unknown[]) => void

function fakeBuiltin(calls: string[]) {
  const listeners = new Map<string, Listener[]>()

  return {
    updater: {
      setFeedURL: (options: { url: string }) => void calls.push(`feed:${options.url}`),
      checkForUpdates: () => void calls.push('check'),
      quitAndInstall: () => void calls.push('install'),
      on: (event: string, listener: Listener) => {
        listeners.set(event, [...(listeners.get(event) ?? []), listener])
      },
      removeListener: (event: string, listener: Listener) => {
        listeners.set(event, (listeners.get(event) ?? []).filter(l => l !== listener))
      }
    },
    emit: (event: string, ...args: unknown[]) => {
      for (const listener of [...(listeners.get(event) ?? [])]) {
        listener(...args)
      }
    },
    count: (event: string) => (listeners.get(event) ?? []).length
  }
}

test('win32 check sets the channel feed before checking and resolves on the verdict', async () => {
  const calls: string[] = []
  const fake = fakeBuiltin(calls)
  const pending = checkWin32Update('https://r2.example.com/feeds/win32/nightly/', fake.updater)

  assert.deepEqual(calls, ['feed:https://r2.example.com/feeds/win32/nightly/', 'check'])
  fake.emit('update-available')

  const result = await pending

  assert.equal(result.updateAvailable, true)
  // Every listener came off — the singleton must not double-report on the
  // NEXT check.
  assert.equal(fake.count('update-available'), 0)
  assert.equal(fake.count('update-not-available'), 0)
  assert.equal(fake.count('error'), 0)
})

test('win32 check resolves false when the feed has nothing newer', async () => {
  const fake = fakeBuiltin([])
  const pending = checkWin32Update('https://r2.example.com/feeds/win32/stable/', fake.updater)

  fake.emit('update-not-available')
  assert.equal((await pending).updateAvailable, false)
})

test('win32 check rejects on updater error and detaches listeners', async () => {
  const fake = fakeBuiltin([])
  const pending = checkWin32Update('https://r2.example.com/feeds/win32/stable/', fake.updater)

  fake.emit('error', new Error('feed unreachable'))
  await assert.rejects(pending, /feed unreachable/)
  assert.equal(fake.count('error'), 0)
})

test('win32 apply runs beforeInstall between the download and the install', async () => {
  const calls: string[] = []
  const fake = fakeBuiltin(calls)
  const pending = applyWin32Update(() => void calls.push('teardown'), fake.updater)

  fake.emit('update-downloaded')
  await pending
  assert.deepEqual(calls, ['teardown', 'install'])
})

test('a failed win32 download installs nothing and skips beforeInstall', async () => {
  const calls: string[] = []
  const fake = fakeBuiltin(calls)
  const pending = applyWin32Update(() => void calls.push('teardown'), fake.updater)

  fake.emit('error', new Error('download failed'))
  await assert.rejects(pending, /download failed/)
  assert.deepEqual(calls, [])
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
