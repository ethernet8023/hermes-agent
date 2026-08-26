// Replace payload-internal python links with independent copies.
//
// uv venv --relocatable on POSIX makes venv/bin/python* a symlink (or a
// hardlink) onto the store interpreter. codesign signs one path, the other
// inode changes, and the next pass reports "file modified" and retries.
// A regular-file copy of each link lets codesign sign each path once.

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
// (uv tarball, agent-browser tgz). Drop the cache before codesign.
export function stripFetchCache(root) {
  if (!root) return 0
  const tools = path.join(root, 'tools')
  if (!fs.existsSync(tools)) return 0
  let removed = 0
  for (const ent of fs.readdirSync(tools, { withFileTypes: true })) {
    if (!ent.isDirectory() || !ent.name.startsWith('fetch-')) continue
    fs.rmSync(path.join(tools, ent.name), { recursive: true, force: true })
    removed += 1
  }
  return removed
}

export function materializePayloadLinks(root) {
  if (!root || !fs.existsSync(root)) return 0
  let count = 0
  const walk = dir => {
    let entries
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true })
    } catch {
      return
    }
    for (const ent of entries) {
      const p = path.join(dir, ent.name)
      if (ent.isDirectory() && !ent.isSymbolicLink()) {
        walk(p)
        continue
      }
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
  }
  walk(root)
  return count
}
