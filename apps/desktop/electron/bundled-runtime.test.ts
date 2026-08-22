import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'

import { test } from 'vitest'

import {
  defaultUpdateChannel,
  findEmbeddedPython,
  installIdForRoot,
  isNightlyTag,
  latestReleaseFromLsRemote,
  PAYLOAD_SCHEMA_VERSION,
  resolvePayload,
  updateChannelFromConfig
} from '../electron/bundled-runtime'

// ─── resolvePayload ────────────────────────────────────────────────

const readerFor = (manifest: unknown) => (p: string) => {
  if (!p.endsWith('manifest.json')) {
    throw new Error('ENOENT')
  }

  return JSON.stringify(manifest)
}

const completeManifest = {
  schemaVersion: PAYLOAD_SCHEMA_VERSION,
  tag: 'v1.2.3',
  commit: 'a'.repeat(40),
  python: 'python/cpython-3.11.15-macos-aarch64-none/bin/python3'
}

test('resolvePayload returns null for dev runs, external stubs, and garbage', () => {
  assert.equal(resolvePayload(null), null)
  assert.equal(resolvePayload(undefined), null)
  assert.equal(resolvePayload('/res', readerFor({ schemaVersion: PAYLOAD_SCHEMA_VERSION, external: true })), null)
  assert.equal(
    resolvePayload('/res', () => {
      throw new Error('ENOENT')
    }),
    null
  )
  assert.equal(resolvePayload('/res', readerFor('not-an-object')), null)
})

test('resolvePayload rejects old-schema manifests', () => {
  // The app and its payload travel together, so a mismatch means a
  // foreign artifact. Schema 3 is the pre-python-path shape: without the
  // recorded interpreter the shell would be back to guessing the layout.
  assert.equal(resolvePayload('/res', readerFor({ schemaVersion: 3, tag: 'v1.0.0', commit: 'a'.repeat(40) })), null)
})

test('resolvePayload rejects a manifest whose python path is missing or absolute', () => {
  // A schema-4 build cannot pass staging without probing the interpreter
  // it records, so a missing/absolute path means a malformed or foreign
  // manifest, not a degraded payload.
  assert.equal(resolvePayload('/res', readerFor({ ...completeManifest, python: undefined })), null)
  assert.equal(resolvePayload('/res', readerFor({ ...completeManifest, python: '' })), null)
  assert.equal(resolvePayload('/res', readerFor({ ...completeManifest, python: '/abs/python3' })), null)
})

test('resolvePayload gates on the manifest alone — store-entry tool names never break it', () => {
  // The regression this replaces: a bare-name item walk (`uv/`, `node/`,
  // `git/`) rejected every payload once tools staged as
  // `<tool>-<version>-<target>/` store entries, so v0.28.0 bundles threw
  // "damaged" on a complete artifact. Completeness is a build invariant;
  // the shell's only integrity probe is findEmbeddedPython on the
  // interpreter it spawns.
  const p = resolvePayload('/res', readerFor(completeManifest))

  assert.ok(p)
  assert.match(p.dir, /agent-payload$/)
  assert.equal(p.tag, 'v1.2.3')
  assert.equal(p.python, completeManifest.python)
})

// ─── findEmbeddedPython ────────────────────────────────────────────

test('findEmbeddedPython joins the recorded path and verifies the binary exists', () => {
  const payload = {
    dir: '/res/agent-payload',
    python: 'python/cpython-3.11.15-macos-aarch64-none/bin/python3'
  }

  const expected = '/res/agent-payload/python/cpython-3.11.15-macos-aarch64-none/bin/python3'

  assert.equal(findEmbeddedPython(payload, { existsSync: (p: string) => p === expected } as never), expected)

  // Recorded but not on disk = damaged artifact → null, not a throw.
  assert.equal(findEmbeddedPython(payload, { existsSync: () => false } as never), null)
})

// ─── updateChannelFromConfig ───────────────────────────────────────

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

// ─── defaultUpdateChannel ──────────────────────────────────────────

const NIGHTLY_TAG = 'v0.28.0-nightly.20260819171926'

test('a bundle with no record tracks the feed its own artifact publishes to', () => {
  // product-identity.cjs keys the published feed name on this same tag, so
  // the feed the app asks for and the feed it was published to agree.
  // Defaulting a nightly to stable made it request nightly.yml under the
  // newest STABLE release — a 404 with no fallback, which left a fresh
  // nightly install permanently unable to update.
  assert.equal(defaultUpdateChannel(NIGHTLY_TAG, 'electron-updater'), 'nightly')
  assert.equal(defaultUpdateChannel('v0.27.0', 'electron-updater'), 'stable')

  // Only artifacts with a release feed have a feed to track.
  assert.equal(defaultUpdateChannel(NIGHTLY_TAG, 'self'), 'main')
  assert.equal(defaultUpdateChannel(NIGHTLY_TAG, 'external'), 'main')
  assert.equal(defaultUpdateChannel(null, null), 'main')
})

test('isNightlyTag accepts both nightly tag shapes and nothing else', () => {
  assert.equal(isNightlyTag(NIGHTLY_TAG), true)
  // The legacy date-only shape release.py used to emit.
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
  // The tag supplies the DEFAULT only — opting a nightly build onto stable
  // has to keep working.
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

test('installIdForRoot matches boot_bootstrap._install_key (sha16 of the canonical path)', () => {
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

// ── latestReleaseFromLsRemote ───────────────────────────────────────

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
  // the three-digit major cap — otherwise 2026 would beat every SemVer
  // release forever.
  assert.equal(latest?.tag, 'v0.10.0')
  assert.equal(latest?.sha, 'c'.repeat(40))

  const semverOnly = latestReleaseFromLsRemote(
    [
      `${'a'.repeat(40)}\trefs/tags/v0.9.0`,
      `${'b'.repeat(40)}\trefs/tags/v0.10.0`,
      `${'c'.repeat(40)}\trefs/tags/v0.10.0^{}`
    ].join('\n')
  )

  assert.equal(semverOnly?.tag, 'v0.10.0')
  assert.equal(semverOnly?.sha, 'c'.repeat(40))
})

test('release picking returns null when no final release tag exists', () => {
  assert.equal(latestReleaseFromLsRemote(''), null)
  assert.equal(latestReleaseFromLsRemote(`${'d'.repeat(40)}\trefs/tags/v1.0.0-beta.2`), null)
})
