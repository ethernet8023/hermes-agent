// feature-flags.ts is the single resolver for which gated surfaces are on in
// this artifact. These tests pin the flag table: launch argv opts in on any
// build, preview builds ride along by default, tagged stable builds stay strict.
import assert from 'node:assert/strict'

import { test } from 'vitest'

import { isCanaryTag, isPreviewBuild, resolveFeatureFlags } from './feature-flags'
import type { InstallStamp } from './install-stamp'

test('stable builds need the --local launch flag for local models', () => {
  assert.deepEqual(resolveFeatureFlags({ argv: [], preview: false }), { localModels: false })
  assert.deepEqual(resolveFeatureFlags({ argv: ['Hermes.exe'], preview: false }), { localModels: false })
})

test('--local in argv opts into local models on any build', () => {
  assert.deepEqual(resolveFeatureFlags({ argv: ['Hermes.exe', '--local'], preview: false }), { localModels: true })
  assert.deepEqual(resolveFeatureFlags({ argv: ['--local'], preview: true }), { localModels: true })
})

test('preview builds get local models by default, no flag needed', () => {
  assert.deepEqual(resolveFeatureFlags({ argv: [], preview: true }), { localModels: true })
  assert.deepEqual(resolveFeatureFlags({ argv: ['Hermes.exe'], preview: true }), { localModels: true })
})

test('isCanaryTag recognizes canary stamps and rejects stable/dev tags', () => {
  assert.equal(isCanaryTag('v0.28.0-canary.20260818'), true)
  assert.equal(isCanaryTag('v0.28.0-canary.20260818123456'), true)
  assert.equal(isCanaryTag('v0.28.0'), false)
  assert.equal(isCanaryTag(''), false)
  assert.equal(isCanaryTag(null), false)
  assert.equal(isCanaryTag(undefined), false)
})

const stamp = (overrides: Partial<InstallStamp>): InstallStamp => ({
  schemaVersion: 1, commit: 'a'.repeat(40), commitDate: null, branch: null, builtAt: null, dirty: false,
  source: 'ci', distribution: 'desktop-app', updateMechanism: 'external', baseVersion: '1.2.3',
  displayVersion: '1.2.3', distance: null, payload: 'bundled', tag: null, ...overrides
})

test('only a tagged stable release is not a preview build', () => {
  assert.equal(isPreviewBuild(stamp({ tag: 'v1.2.3' })), false)
  assert.equal(isPreviewBuild(stamp({ tag: 'v1.2.4-canary.20260911010101' })), true)
})

test('commit builds and channel builds are preview builds even with no tag', () => {
  assert.equal(isPreviewBuild(stamp({ source: 'commit-build', tag: null })), true)
  assert.equal(isPreviewBuild(stamp({
    source: 'channel-build', tag: null,
    channelBuild: { schema: 1, buildId: 'b'.repeat(32), channel: 'stable', sequence: 1,
      repository: 'example/hermes-agent', commit: 'a'.repeat(40), sourceVersion: '1.2.3', version: '0.0.1',
      windowsVersion: '0.0.1.0', publicBase: 'https://example.com', bundleEnv: {},
      identity: { token: '1234567890abcdef', displayName: 'Preview', appId: 'chat.preview', appNamePascal: 'Preview',
        artifactNamePascal: 'Preview', cliName: 'preview', windowsExecutableName: 'Preview.exe', msixAppIdWithOrg: 'Nous.Preview' } }
  })), true)
})

test('dev runs with no stamp are preview builds', () => {
  assert.equal(isPreviewBuild(null), true)
})
