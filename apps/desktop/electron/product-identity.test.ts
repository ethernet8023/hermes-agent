// product-identity.cjs is the single derivation of the desktop product
// identity; electron/product-identity.ts re-exports it. These tests hold
// the identity contract: the TS accessor resolves to the .cjs object, and
// the two variants disagree on every OS-visible marker (side-by-side
// installs must not collide).
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'

import { afterEach, beforeEach, test, vi } from 'vitest'

const require = createRequire(import.meta.url)

beforeEach(() => {
  vi.resetModules()
})

afterEach(() => {
  delete process.env.HERMES_DESKTOP_VARIANT
  delete process.env.HERMES_PAYLOAD_TAG
  vi.resetModules()
})

async function identityForVariant(variant: string | undefined) {
  if (variant === undefined) {
    delete process.env.HERMES_DESKTOP_VARIANT
  } else {
    process.env.HERMES_DESKTOP_VARIANT = variant
  }

  delete require.cache[require.resolve('../product-identity.cjs')]
  vi.resetModules()

  return (await import('./product-identity')).PRODUCT_IDENTITY
}

test('light identity is fully distinct from the full identity', async () => {
  const full = await identityForVariant(undefined)
  const light = await identityForVariant('light')

  assert.equal(light.light, true)

  // Every OS-visible identity marker must differ, or side-by-side
  // installs collide (userData dir, handler registration, updater feed).
  for (const prop of [...Object.keys(light), ...Object.keys(full)]) {
    assert.notEqual(light[prop], full[prop])
  }
})

test('a nightly payload tag moves BOTH variants onto their nightly feed channel', async () => {
  process.env.HERMES_PAYLOAD_TAG = 'v0.28.0-nightly.20260818'
  const full = await identityForVariant(undefined)
  assert.equal(full.channel, 'nightly')

  process.env.HERMES_PAYLOAD_TAG = 'v0.28.0-nightly.20260818'
  const light = await identityForVariant('light')
  assert.equal(light.channel, 'light-nightly')
})

test('stable tags and tagless dev builds publish to the stable channels', async () => {
  process.env.HERMES_PAYLOAD_TAG = 'v0.28.0'
  assert.equal((await identityForVariant(undefined)).channel, 'latest')

  delete process.env.HERMES_PAYLOAD_TAG
  assert.equal((await identityForVariant('light')).channel, 'light')
})

test('bundled variant keeps the Hermes display name and a distinct appId', async () => {
  const full = await identityForVariant(undefined)
  const bundled = await identityForVariant('bundled')

  assert.equal(bundled.light, false)
  assert.equal(bundled.displayName, full.displayName)
  assert.equal(bundled.appNamePascal, 'HermesBundled')
  assert.notEqual(bundled.appId, full.appId)
  assert.equal(bundled.channel, 'latest')
})
