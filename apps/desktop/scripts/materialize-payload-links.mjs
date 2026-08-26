// Replace payload-internal python links with independent copies.
//
// uv venv --relocatable on POSIX makes venv/bin/python* a symlink (or a
// hardlink) onto the store interpreter. codesign signs one path, the other
// inode changes, and the next pass reports "file modified" and retries.
// Only venv/bin is rewritten. Do not walk the rest of the payload: a
// file-symlink at Foo.framework/Foo is the framework binary, and
// flattening it makes codesign report "bundle format is ambiguous".

import fs from 'node:fs'
import path from 'node:path'

export function findPackedPayload(appOutDir, platform) {
  if (!appOutDir) return null
  const candidates =
    platform === 'darwin'
      ? [
          path.join(appOutDir, 'Contents', 'Resources', 'agent-payload'),
          path.join(appOutDir, 'Hermes.app', 'Contents', 'Resources', 'agent-payload'),
          path.join(appOutDir, 'HermesBundled.app', 'Contents', 'Resources', 'agent-payload')
        ]
      : [path.join(appOutDir, 'resources', 'agent-payload')]
  return candidates.find(p => fs.existsSync(p)) || null
}

// The store keeps verified downloads under fetch-<sha16>/. A sealed
// payload already has the unpacked entries. Apple's notary unpacks
// every archive it finds and rejects unsigned Mach-O inside them
// (uv tarball, leftover chromium). Drop the cache and leftover
// chromium store entries before codesign.
export function stripFetchCache(root) {
  if (!root) return 0
  const tools = path.join(root, 'tools')
  if (!fs.existsSync(tools)) return 0
  let removed = 0
  for (const ent of fs.readdirSync(tools, { withFileTypes: true })) {
    if (!ent.isDirectory()) continue
    if (ent.name.startsWith('fetch-') || /^chromium(_headless_shell)?-\d+/.test(ent.name)) {
      fs.rmSync(path.join(tools, ent.name), { recursive: true, force: true })
      removed += 1
    }
  }
  return removed
}

export function materializePayloadLinks(root) {
  if (!root || !fs.existsSync(root)) return 0
  const bin = path.join(root, 'venv', 'bin')
  if (!fs.existsSync(bin)) return 0
  let count = 0
  let entries
  try {
    entries = fs.readdirSync(bin, { withFileTypes: true })
  } catch {
    return 0
  }
  for (const ent of entries) {
    const p = path.join(bin, ent.name)
    let st
    try {
      st = fs.lstatSync(p)
    } catch {
      continue
    }
    if (st.isSymbolicLink()) {
      let target
      try {
        target = fs.statSync(p)
      } catch {
        continue
      }
      if (!target.isFile()) continue
      const tmp = `${p}.__materialize__`
      fs.copyFileSync(p, tmp)
      fs.unlinkSync(p)
      fs.renameSync(tmp, p)
      count += 1
      continue
    }
    if (st.isFile() && st.nlink > 1) {
      const tmp = `${p}.__unlink__`
      fs.copyFileSync(p, tmp)
      fs.renameSync(tmp, p)
      count += 1
    }
  }
  return count
}
