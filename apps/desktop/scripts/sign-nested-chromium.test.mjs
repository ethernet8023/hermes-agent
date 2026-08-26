import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { test } from 'vitest'

import {
  chromiumRoots,
  isMachO,
  listSignableMachO,
  parseDeveloperId,
  signNestedChromium
} from './sign-nested-chromium.mjs'

function tempRoot() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-sign-chrome-'))
}

function machoBuf() {
  const buf = Buffer.alloc(16)
  buf.writeUInt32BE(0xfeedfacf, 0)
  return buf
}

test('isMachO accepts a 64-bit Mach-O magic and rejects text', () => {
  const dir = tempRoot()
  try {
    const bin = path.join(dir, 'a')
    const txt = path.join(dir, 'b.txt')
    fs.writeFileSync(bin, machoBuf())
    fs.writeFileSync(txt, 'not a binary')
    assert.equal(isMachO(bin), true)
    assert.equal(isMachO(txt), false)
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('chromiumRoots only names chromium store entries', () => {
  const payload = tempRoot()
  try {
    fs.mkdirSync(path.join(payload, 'tools', 'chromium-1208'), { recursive: true })
    fs.mkdirSync(path.join(payload, 'tools', 'chromium_headless_shell-1208'), { recursive: true })
    fs.mkdirSync(path.join(payload, 'tools', 'uv-0.12.3-darwin-arm64'), { recursive: true })
    const roots = chromiumRoots(payload).map(p => path.basename(p)).sort()
    assert.deepEqual(roots, ['chromium-1208', 'chromium_headless_shell-1208'])
  } finally {
    fs.rmSync(payload, { recursive: true, force: true })
  }
})

test('listSignableMachO skips framework file-symlinks', () => {
  if (process.platform === 'win32') return
  const root = tempRoot()
  try {
    const versioned = path.join(root, 'F.framework', 'Versions', 'A')
    fs.mkdirSync(versioned, { recursive: true })
    const real = path.join(versioned, 'F')
    fs.writeFileSync(real, machoBuf())
    const link = path.join(root, 'F.framework', 'F')
    fs.symlinkSync(path.join('Versions', 'A', 'F'), link)
    const files = listSignableMachO(root)
    assert.deepEqual(files, [real])
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test('parseDeveloperId takes the first Developer ID Application line', () => {
  const listing = `
  1) ABC "Apple Development: someone@example.com (TEAM)"
  2) DEF "Developer ID Application: Nous Research Inc (763K57MW7Z)"
  3) GHI "Developer ID Installer: Nous Research Inc (763K57MW7Z)"
`
  assert.equal(parseDeveloperId(listing), 'Developer ID Application: Nous Research Inc (763K57MW7Z)')
  assert.equal(parseDeveloperId('nothing here'), null)
})

test('signNestedChromium no-ops without an identity', () => {
  const payload = tempRoot()
  try {
    const r = signNestedChromium(payload, { identity: null, entitlements: path.join(payload, 'missing.plist') })
    assert.deepEqual(r, { signed: 0, identity: null })
  } finally {
    fs.rmSync(payload, { recursive: true, force: true })
  }
})

test('signNestedChromium codesigns each regular Mach-O and skips the symlink', () => {
  if (process.platform === 'win32') return
  const payload = tempRoot()
  try {
    const entitlements = path.join(payload, 'entitlements.plist')
    fs.writeFileSync(entitlements, '<plist/>')
    const versioned = path.join(
      payload,
      'tools',
      'chromium-1208',
      'F.framework',
      'Versions',
      'A'
    )
    fs.mkdirSync(versioned, { recursive: true })
    const real = path.join(versioned, 'F')
    fs.writeFileSync(real, machoBuf())
    const link = path.join(payload, 'tools', 'chromium-1208', 'F.framework', 'F')
    fs.symlinkSync(path.join('Versions', 'A', 'F'), link)
    const calls = []
    const r = signNestedChromium(payload, {
      identity: 'Developer ID Application: Test',
      entitlements,
      exec: (cmd, args) => {
        calls.push([cmd, ...args])
      }
    })
    assert.equal(r.signed, 1)
    assert.equal(calls.length, 1)
    assert.equal(calls[0][0], 'codesign')
    assert.ok(calls[0].includes(real))
    assert.ok(!calls[0].includes(link))
    assert.ok(calls[0].includes('--timestamp'))
    assert.ok(calls[0].includes('runtime'))
  } finally {
    fs.rmSync(payload, { recursive: true, force: true })
  }
})
