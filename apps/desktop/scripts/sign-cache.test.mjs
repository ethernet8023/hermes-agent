import assert from 'node:assert/strict'
import crypto from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { test } from 'vitest'

import { contentHash, lookupSigned, storeSigned } from './sign-cache.mjs'

function tempDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-sign-cache-'))
}

test('contentHash is the sha256 hex of the buffer', () => {
  const bytes = Buffer.from('unsigned executable bytes')
  const expected = crypto.createHash('sha256').update(bytes).digest('hex')
  assert.equal(contentHash(bytes), expected)
  assert.match(contentHash(bytes), /^[0-9a-f]{64}$/)
})

test('lookupSigned misses on an empty (even nonexistent) cache dir', () => {
  const dir = tempDir()
  try {
    assert.equal(lookupSigned(dir, 'a'.repeat(64)), null)
    assert.equal(lookupSigned(path.join(dir, 'never-created'), 'a'.repeat(64)), null)
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('storeSigned then lookupSigned round-trips the exact bytes', () => {
  const dir = tempDir()
  try {
    const cacheDir = path.join(dir, 'cache') // exercise mkdir -p
    const bytes = crypto.randomBytes(1024)
    const key = contentHash(bytes)
    storeSigned(cacheDir, key, bytes)
    const hit = lookupSigned(cacheDir, key)
    assert.ok(hit, 'expected a cache hit after store')
    assert.equal(hit, path.join(cacheDir, key))
    assert.deepEqual(fs.readFileSync(hit), bytes)
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('storeSigned is atomic: no .tmp files survive a store', () => {
  const dir = tempDir()
  try {
    const bytes = crypto.randomBytes(256)
    storeSigned(dir, contentHash(bytes), bytes)
    const leftovers = fs.readdirSync(dir).filter((name) => name.startsWith('.tmp-'))
    assert.deepEqual(leftovers, [])
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('duplicate store of the same key is idempotent', () => {
  const dir = tempDir()
  try {
    const bytes = crypto.randomBytes(512)
    const key = contentHash(bytes)
    storeSigned(dir, key, bytes)
    // A concurrent builder storing the same content-addressed key must not
    // throw and must leave the entry intact (same key = same bytes).
    storeSigned(dir, key, bytes)
    assert.deepEqual(fs.readFileSync(path.join(dir, key)), bytes)
    const leftovers = fs.readdirSync(dir).filter((name) => name.startsWith('.tmp-'))
    assert.deepEqual(leftovers, [])
    assert.deepEqual(fs.readdirSync(dir), [key])
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})
