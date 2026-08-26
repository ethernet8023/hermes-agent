import assert from 'node:assert/strict'
import crypto from 'node:crypto'
import fs from 'node:fs'
import { createRequire } from 'node:module'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { test } from 'vitest'

import { contentHash } from './sign-cache.mjs'
import { resolveCacheDir, signWithCache } from './sign-cached.mjs'

const require = createRequire(import.meta.url)

function tempDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-sign-cached-'))
}

test('cache miss calls the signer exactly once and stores the signed bytes', async () => {
  const dir = tempDir()
  try {
    const cacheDir = path.join(dir, 'cache')
    const file = path.join(dir, 'app.exe')
    const unsigned = Buffer.from('unsigned payload')
    const signed = Buffer.from('unsigned payload + signature blob')
    fs.writeFileSync(file, unsigned)
    const key = contentHash(unsigned)

    let calls = 0
    await signWithCache(file, cacheDir, async () => {
      calls += 1
      fs.writeFileSync(file, signed) // the real signer rewrites in place
    })

    assert.equal(calls, 1)
    assert.deepEqual(fs.readFileSync(file), signed)
    // The cache entry is keyed by the UNSIGNED content hash but holds the
    // post-sign bytes.
    assert.deepEqual(fs.readFileSync(path.join(cacheDir, key)), signed)
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('cache hit never calls the signer and replaces the file with cached bytes', async () => {
  const dir = tempDir()
  try {
    const cacheDir = path.join(dir, 'cache')
    const file = path.join(dir, 'app.exe')
    const unsigned = Buffer.from('identical build output')
    const signed = Buffer.from('identical build output, previously signed')
    fs.writeFileSync(file, unsigned)
    fs.mkdirSync(cacheDir, { recursive: true })
    fs.writeFileSync(path.join(cacheDir, contentHash(unsigned)), signed)

    await signWithCache(file, cacheDir, async () => {
      throw new Error('signer must not run on a cache hit')
    })

    assert.deepEqual(fs.readFileSync(file), signed)
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('a second call with identical unsigned input is a hit', async () => {
  const dir = tempDir()
  try {
    const cacheDir = path.join(dir, 'cache')
    const unsigned = crypto.randomBytes(2048)
    const signed = Buffer.concat([unsigned, Buffer.from('signature')])
    let calls = 0
    const signer = (file) => async () => {
      calls += 1
      fs.writeFileSync(file, signed)
    }

    const first = path.join(dir, 'first.exe')
    fs.writeFileSync(first, unsigned)
    await signWithCache(first, cacheDir, signer(first))

    const second = path.join(dir, 'second.exe')
    fs.writeFileSync(second, unsigned)
    await signWithCache(second, cacheDir, signer(second))

    assert.equal(calls, 1, 'identical input must be signed only once')
    assert.deepEqual(fs.readFileSync(second), signed)
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('resolveCacheDir defaults under apps/desktop/build/sign-cache', () => {
  const desktopDir = path.dirname(path.dirname(fileURLToPath(import.meta.url)))
  assert.equal(resolveCacheDir({}), path.join(desktopDir, 'build', 'sign-cache'))
})

test('resolveCacheDir honors HERMES_SIGN_CACHE', () => {
  assert.equal(resolveCacheDir({ HERMES_SIGN_CACHE: '/tmp/somewhere-else' }), '/tmp/somewhere-else')
})

// ─── builder-bump tripwire ──────────────────────────────────────────────────
// sign-cached.mjs deep-imports app-builder-lib's WindowsSignAzureManager by
// file path (the package exports map exposes only "." and "./internal", so
// the dist path is reached the same way run-electron-builder.mjs finds the
// CLI: resolve the entry, walk up to the package root, then direct-file
// import). If an electron-builder/app-builder-lib bump moves or renames the
// class, THIS test is what fails in js-tests — before a release build does.
// 60s timeout: the direct-file import drags in app-builder-lib's whole
// module graph (toolsets, electronGet, vm), which takes ~30s on a cold
// disk cache — far past vitest's 5s default.
test('the real WindowsSignAzureManager resolves and constructs against a packager shim', { timeout: 60_000 }, async () => {
  const entry = require.resolve('app-builder-lib')
  let root = path.dirname(entry)
  while (!fs.existsSync(path.join(root, 'package.json'))) {
    const parent = path.dirname(root)
    assert.notEqual(parent, root, 'app-builder-lib package root not found')
    root = parent
  }
  const mod = await import(
    pathToFileURL(path.join(root, 'dist', 'codeSign', 'win', 'windowsSignAzureManager.js')).href
  )
  assert.equal(typeof mod.WindowsSignAzureManager, 'function')

  // Constructor contract (verified against 27.0.0-alpha.6): reads
  // packager.platformOptions.sign (throws unless type === 'azure'),
  // packager.config.toolsets (optional), and packager.buildResourcesDir
  // (only stored — WineVmManager's constructor touches no filesystem).
  const mgr = new mod.WindowsSignAzureManager({
    platformOptions: {
      sign: {
        type: 'azure',
        endpoint: 'x',
        codeSigningAccountName: 'y',
        certificateProfileName: 'z',
        publisherName: 'p'
      }
    },
    config: {},
    buildResourcesDir: '/tmp'
  })
  assert.equal(typeof mgr.signFile, 'function')
  assert.equal(typeof mgr.initialize, 'function')
})
