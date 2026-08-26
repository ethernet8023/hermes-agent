// Sign payload Chromium Mach-O with our Developer ID before osx-sign
// seals Hermes.app. Google's leftover signatures have no timestamp and
// no hardened runtime, so Apple's notary rejects them. signIgnore keeps
// osx-sign off the .app tree so it does not hit the Foo.framework/Foo
// symlink ("bundle format is ambiguous"). This walk signs only regular
// files, never a symlink.

import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'

const MACHO_MAGICS = new Set([
  0xfeedface, 0xcefaedfe, 0xfeedfacf, 0xcffaedfe, 0xcafebabe, 0xbebafeca
])

export function isMachO(file) {
  const buf = Buffer.alloc(4)
  let fd
  try {
    fd = fs.openSync(file, 'r')
    if (fs.readSync(fd, buf, 0, 4, 0) !== 4) return false
  } catch {
    return false
  } finally {
    if (fd != null) fs.closeSync(fd)
  }
  return MACHO_MAGICS.has(buf.readUInt32BE(0))
}

export function chromiumRoots(payload) {
  const tools = path.join(payload, 'tools')
  if (!fs.existsSync(tools)) return []
  return fs
    .readdirSync(tools, { withFileTypes: true })
    .filter(ent => ent.isDirectory() && /^chromium(_headless_shell)?-\d+/.test(ent.name))
    .map(ent => path.join(tools, ent.name))
}

export function listSignableMachO(root) {
  const out = []
  const walk = dir => {
    let entries
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true })
    } catch {
      return
    }
    for (const ent of entries) {
      const p = path.join(dir, ent.name)
      if (ent.isSymbolicLink()) continue
      if (ent.isDirectory()) {
        walk(p)
        continue
      }
      if (!ent.isFile()) continue
      if (isMachO(p)) out.push(p)
    }
  }
  walk(root)
  return out
}

export function parseDeveloperId(identityList) {
  const line = String(identityList || '')
    .split(/\r?\n/)
    .find(l => l.includes('Developer ID Application:'))
  if (!line) return null
  const quoted = line.match(/"([^"]+)"/)
  return quoted ? quoted[1] : null
}

export function findDeveloperId(exec = execFileSync, keychain = null) {
  if (process.env.CSC_NAME) return process.env.CSC_NAME
  const args = ['find-identity', '-v', '-p', 'codesigning']
  if (keychain) args.push(keychain)
  let out
  try {
    out = exec('security', args, { encoding: 'utf8' })
  } catch {
    return null
  }
  return parseDeveloperId(out)
}

export async function resolveSigningIdentity(packager, exec = execFileSync) {
  if (!packager?.codeSigningInfo?.value) {
    return { identity: findDeveloperId(exec), keychain: null }
  }
  let info
  try {
    info = await packager.codeSigningInfo.value
  } catch {
    return { identity: findDeveloperId(exec), keychain: null }
  }
  const keychain = info?.keychainFile || process.env.CSC_KEYCHAIN || null
  return { identity: findDeveloperId(exec, keychain), keychain }
}

export function signNestedChromium(payload, opts = {}) {
  const identity = opts.identity
  if (!identity) return { signed: 0, identity: null }
  const entitlements = opts.entitlements
  if (!entitlements || !fs.existsSync(entitlements)) {
    throw new Error(`sign-nested-chromium: entitlements missing at ${entitlements}`)
  }
  const exec = opts.exec ?? execFileSync
  const keychain = opts.keychain || null
  let signed = 0
  for (const root of chromiumRoots(payload)) {
    for (const file of listSignableMachO(root)) {
      const args = [
        '--force',
        '--sign',
        identity,
        '--timestamp',
        '--options',
        'runtime',
        '--entitlements',
        entitlements
      ]
      if (keychain) args.push('--keychain', keychain)
      args.push(file)
      exec('codesign', args, { stdio: 'pipe' })
      signed += 1
    }
  }
  return { signed, identity }
}
