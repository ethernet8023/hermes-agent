import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { test } from 'vitest'

import { findPackedPayload, materializePayloadLinks, stripFetchCache } from './materialize-payload-links.mjs'

function tempRoot() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-materialize-'))
}

test('findPackedPayload locates the mac nested payload', () => {
  const root = tempRoot()
  try {
    const app = path.join(root, 'Hermes.app')
    const payload = path.join(app, 'Contents', 'Resources', 'agent-payload')
    fs.mkdirSync(payload, { recursive: true })
    assert.equal(findPackedPayload(app, 'darwin'), payload)
    assert.equal(findPackedPayload(root, 'darwin'), payload)
    assert.equal(findPackedPayload(root, 'linux'), null)
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test('materializePayloadLinks turns a symlink into an independent copy', () => {
  if (process.platform === 'win32') return
  const root = tempRoot()
  try {
    const store = path.join(root, 'tools', 'python', 'bin')
    const venv = path.join(root, 'venv', 'bin')
    fs.mkdirSync(store, { recursive: true })
    fs.mkdirSync(venv, { recursive: true })
    const real = path.join(store, 'python3.11')
    fs.writeFileSync(real, 'interpreter-bytes')
    const link = path.join(venv, 'python3')
    fs.symlinkSync(real, link)
    assert.equal(fs.lstatSync(link).isSymbolicLink(), true)

    const n = materializePayloadLinks(root)
    assert.equal(n, 1)
    assert.equal(fs.lstatSync(link).isSymbolicLink(), false)
    assert.equal(fs.readFileSync(link, 'utf8'), 'interpreter-bytes')
    fs.writeFileSync(link, 'signed')
    assert.equal(fs.readFileSync(real, 'utf8'), 'interpreter-bytes')
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test('materializePayloadLinks splits a hardlinked pair', () => {
  const root = tempRoot()
  try {
    const store = path.join(root, 'tools', 'python', 'bin')
    const venv = path.join(root, 'venv', 'bin')
    fs.mkdirSync(store, { recursive: true })
    fs.mkdirSync(venv, { recursive: true })
    const real = path.join(store, 'python3.11')
    const twin = path.join(venv, 'python3')
    fs.writeFileSync(real, 'interpreter-bytes')
    try {
      fs.linkSync(real, twin)
    } catch (err) {
      if (err && (err.code === 'EPERM' || err.code === 'ENOSYS')) return
      throw err
    }
    assert.ok(fs.lstatSync(twin).nlink >= 2)

    const n = materializePayloadLinks(root)
    assert.ok(n >= 1)
    assert.equal(fs.lstatSync(twin).nlink, 1)
    fs.writeFileSync(twin, 'signed')
    assert.equal(fs.readFileSync(real, 'utf8'), 'interpreter-bytes')
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test('stripFetchCache removes only fetch- dirs under tools/', () => {
  const root = tempRoot()
  try {
    const tools = path.join(root, 'tools')
    fs.mkdirSync(path.join(tools, 'fetch-546f7f8a6c70ff13'), { recursive: true })
    fs.writeFileSync(path.join(tools, 'fetch-546f7f8a6c70ff13', 'uv.tar.gz'), 'x')
    fs.mkdirSync(path.join(tools, 'uv-0.12.3-darwin-arm64'), { recursive: true })
    fs.writeFileSync(path.join(tools, 'uv-0.12.3-darwin-arm64', 'uv'), 'bin')
    assert.equal(stripFetchCache(root), 1)
    assert.equal(fs.existsSync(path.join(tools, 'fetch-546f7f8a6c70ff13')), false)
    assert.equal(fs.existsSync(path.join(tools, 'uv-0.12.3-darwin-arm64', 'uv')), true)
    assert.equal(stripFetchCache(root), 0)
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})
