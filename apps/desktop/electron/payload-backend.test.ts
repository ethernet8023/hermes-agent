import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import { adoptPayloadVenv, resolvePayload } from './payload-backend'

function tmpdir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'payload-test-'))
}

function writePayload(
  root: string,
  {
    manifest = { schema: 1, target: 'win32-x64', repo: 'hermes-agent', venv: 'venv', store: 'tools' },
    isWindows = true
  }: any = {}
) {
  const dir = path.join(root, 'agent-payload')

  fs.mkdirSync(path.join(dir, 'hermes-agent'), { recursive: true })
  fs.mkdirSync(path.join(dir, 'tools', 'python-3.11.16-win32-x64'), { recursive: true })

  const scripts = isWindows ? path.join(dir, 'venv', 'Scripts') : path.join(dir, 'venv', 'bin')

  fs.mkdirSync(scripts, { recursive: true })
  fs.writeFileSync(path.join(scripts, isWindows ? 'python.exe' : 'python'), '')
  fs.writeFileSync(path.join(dir, 'venv', 'pyvenv.cfg'), 'home = C:\\ci\\build\\python\nversion_info = 3.11.16\n')
  fs.writeFileSync(path.join(dir, 'manifest.json'), JSON.stringify(manifest))
  fs.writeFileSync(
    path.join(dir, 'tools', 'facts.json'),
    JSON.stringify({ packages: { python: { entry: 'python-3.11.16-win32-x64', version: '3.11.16' } } })
  )

  return dir
}

const fsDeps = {
  fileExists: (p: string) => {
    try {
      return fs.statSync(p).isFile()
    } catch {
      return false
    }
  },
  directoryExists: (p: string) => {
    try {
      return fs.statSync(p).isDirectory()
    } catch {
      return false
    }
  }
}

test('resolvePayload finds a complete payload', () => {
  const root = tmpdir()

  writePayload(root)

  const payload = resolvePayload(root, { ...fsDeps, isWindows: true })

  assert.ok(payload)
  assert.equal(payload.repoDir, path.join(root, 'agent-payload', 'hermes-agent'))
  assert.ok(payload.venvPython.endsWith(path.join('Scripts', 'python.exe')))
})

test('resolvePayload returns null without a manifest, for external stubs, and for a broken venv', () => {
  assert.equal(resolvePayload(tmpdir(), { ...fsDeps, isWindows: true }), null)
  assert.equal(resolvePayload(undefined, { ...fsDeps, isWindows: true }), null)

  const externalRoot = tmpdir()

  writePayload(externalRoot, { manifest: { schema: 1, external: true } })
  assert.equal(resolvePayload(externalRoot, { ...fsDeps, isWindows: true }), null)

  const brokenRoot = tmpdir()
  const dir = writePayload(brokenRoot)

  fs.rmSync(path.join(dir, 'venv'), { recursive: true })
  assert.equal(resolvePayload(brokenRoot, { ...fsDeps, isWindows: true }), null)
})

test('adoptPayloadVenv rewrites home from facts.json and is idempotent', () => {
  const root = tmpdir()
  const dir = writePayload(root)
  const payload = resolvePayload(root, { ...fsDeps, isWindows: true })

  assert.ok(payload)
  assert.equal(adoptPayloadVenv(payload, { isWindows: true }), true)

  const text = fs.readFileSync(path.join(dir, 'venv', 'pyvenv.cfg'), 'utf8')

  assert.ok(text.includes(path.join(dir, 'tools', 'python-3.11.16-win32-x64')))
  assert.ok(!text.includes('C:\\ci\\build'))
  assert.ok(text.includes('version_info = 3.11.16'), 'other lines survive')

  // second call: already-correct home is left alone and still reports usable
  assert.equal(adoptPayloadVenv(payload, { isWindows: true }), true)
})

test('adoptPayloadVenv fails closed without facts or python entry', () => {
  const root = tmpdir()
  const dir = writePayload(root)

  fs.writeFileSync(path.join(dir, 'tools', 'facts.json'), JSON.stringify({ packages: {} }))

  const payload = resolvePayload(root, { ...fsDeps, isWindows: true })

  assert.ok(payload)
  assert.equal(adoptPayloadVenv(payload, { isWindows: true }), false)
})

// ─── update channel helpers ─────────────────────────────────────────

import { createHash } from 'node:crypto'

import {
  defaultUpdateChannel,
  installIdForRoot,
  isNightlyTag,
  latestReleaseFromLsRemote,
  updateChannelFromConfig
} from './payload-backend'

const ID = 'a4f3b2c1d0e9f8a7'

const record = (channel: string, id: string = ID) =>
  `update:\n  installs:\n    ${id}:\n      path: /home/u/.hermes/hermes-agent\n      channel: ${channel}\n`

test('channel comes from the per-install record; absent means main', () => {
  assert.equal(updateChannelFromConfig(record('stable'), ID), 'stable')
  assert.equal(updateChannelFromConfig(record('"stable"'), ID), 'stable')
  assert.equal(updateChannelFromConfig(record('nightly'), ID), 'nightly')
  assert.equal(updateChannelFromConfig(record('main'), ID), 'main')
  assert.equal(updateChannelFromConfig('model:\n  provider: nous\n', ID), 'main')
  assert.equal(updateChannelFromConfig(null, ID), 'main')
  assert.equal(updateChannelFromConfig('', ID), 'main')
})

const NIGHTLY_TAG = 'v0.28.0-nightly.20260819171926'

test('a bundle with no record tracks the feed its own artifact publishes to', () => {
  // The published feed name keys off this same tag, so the feed the app
  // asks for and the feed it was published to agree. Defaulting a nightly
  // to stable made it request its nightly feed file under the newest
  // STABLE release — a 404 with no fallback.
  assert.equal(defaultUpdateChannel(NIGHTLY_TAG, 'electron-updater'), 'nightly')
  assert.equal(defaultUpdateChannel('v0.27.0', 'electron-updater'), 'stable')

  // Only artifacts with a release feed have a feed to track.
  assert.equal(defaultUpdateChannel(NIGHTLY_TAG, 'self'), 'main')
  assert.equal(defaultUpdateChannel(NIGHTLY_TAG, 'external'), 'main')
  assert.equal(defaultUpdateChannel(null, null), 'main')
})

test('isNightlyTag accepts both nightly tag shapes and nothing else', () => {
  assert.equal(isNightlyTag(NIGHTLY_TAG), true)
  // The legacy date-only shape.
  assert.equal(isNightlyTag('v0.28.0-nightly.20260818'), true)
  assert.equal(isNightlyTag('v0.27.0'), false)
  assert.equal(isNightlyTag('v0.28.0-rc.1'), false)
  assert.equal(isNightlyTag(null), false)
  assert.equal(isNightlyTag(undefined), false)
})

test('the artifact default answers wherever no record for this install exists', () => {
  // Every path out of the parser, not just the empty-config one: a nightly
  // bundle must not fall back to main because a SIBLING install has a record.
  const stampArgs = [NIGHTLY_TAG, 'electron-updater'] as const

  assert.equal(updateChannelFromConfig(null, ID, ...stampArgs), 'nightly')
  assert.equal(updateChannelFromConfig('', ID, ...stampArgs), 'nightly')
  assert.equal(updateChannelFromConfig('model:\n  provider: nous\n', ID, ...stampArgs), 'nightly')
  assert.equal(updateChannelFromConfig(record('stable', 'ffffffffffffffff'), ID, ...stampArgs), 'nightly')
  assert.equal(updateChannelFromConfig(`update:\n  interval: 1\n`, ID, ...stampArgs), 'nightly')
})

test('an explicit record still overrides the artifact default', () => {
  assert.equal(updateChannelFromConfig(record('stable'), ID, NIGHTLY_TAG, 'electron-updater'), 'stable')
  assert.equal(updateChannelFromConfig(record('main'), ID, NIGHTLY_TAG, 'electron-updater'), 'main')
})

test("another install's record never answers for this install", () => {
  // One config.yaml serves many installs — the whole reason the key is
  // per-install. A stable record under a DIFFERENT sha16 must not leak.
  assert.equal(updateChannelFromConfig(record('stable', 'ffffffffffffffff'), ID), 'main')

  // Two records: only ours answers.
  const both = record('stable', 'ffffffffffffffff') + '    ' + ID + ':\n      channel: nightly\n'
  assert.equal(updateChannelFromConfig(both, ID), 'nightly')
})

test('channel parsing stays inside update.installs', () => {
  // A channel key in ANOTHER block must not leak into the answer.
  const text = `gateway:\n  channel: stable\nupdate:\n  interval: 1\nmodel:\n  channel: stable\n`
  assert.equal(updateChannelFromConfig(text, ID), 'main')

  // The update block ends at the next top-level key.
  const ended = `update:\n  interval: 1\nother:\n  installs:\n    ${ID}:\n      channel: stable\n`
  assert.equal(updateChannelFromConfig(ended, ID), 'main')
})

test('installIdForRoot matches the Python install id (sha16 of the canonical path)', () => {
  // sha256('/home/u/.hermes/hermes-agent')[:16] — recomputed independently.
  assert.equal(
    installIdForRoot('/home/u/.hermes/hermes-agent'),
    createHash('sha256').update('/home/u/.hermes/hermes-agent', 'utf8').digest('hex').slice(0, 16)
  )
  // The canonicalizer output is what gets hashed (symlinked homes).
  assert.equal(
    installIdForRoot('/link/hermes-agent', () => '/real/hermes-agent'),
    installIdForRoot('/real/hermes-agent')
  )
})

test('release picking is numeric, skips prereleases, prefers peeled shas', () => {
  const output = [
    `${'a'.repeat(40)}\trefs/tags/v0.9.0`,
    `${'b'.repeat(40)}\trefs/tags/v0.10.0`,
    `${'c'.repeat(40)}\trefs/tags/v0.10.0^{}`,
    `${'d'.repeat(40)}\trefs/tags/v0.11.0-rc1`,
    `${'e'.repeat(40)}\trefs/tags/v2026.7.20`
  ].join('\n')

  const latest = latestReleaseFromLsRemote(output)

  // v0.10.0 beats v0.9.0 numerically (a lexicographic sort would invert
  // it), the rc prerelease is skipped, and the CalVer tag is excluded by
  // the three-digit major cap.
  assert.equal(latest?.tag, 'v0.10.0')
  assert.equal(latest?.sha, 'c'.repeat(40))
})

test('release picking returns null when no final release tag exists', () => {
  assert.equal(latestReleaseFromLsRemote(''), null)
  assert.equal(latestReleaseFromLsRemote(`${'d'.repeat(40)}\trefs/tags/v1.0.0-beta.2`), null)
})
