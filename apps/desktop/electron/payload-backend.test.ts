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
