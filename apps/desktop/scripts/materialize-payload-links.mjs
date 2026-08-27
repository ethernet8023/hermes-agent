// Rewrite payload-internal python links to RELATIVE symlinks.
//
// uv venv --relocatable on POSIX makes venv/bin/python* a symlink (or a
// hardlink) onto the store interpreter, with an ABSOLUTE target (the build
// runner's staging path). An absolute symlink dangles once the unpacked
// tree moves (first-boot smoke, or a user installing to a different path),
// and a materialized COPY loses the tree identity the interpreter needs to
// resolve its stdlib (sys.prefix falls back to the baked build prefix →
// "No module named 'encodings'").
//
// The correct relocatable form is a RELATIVE symlink: the target lives
// inside the bundle (resources/agent-payload/tools/<entry>/bin/python3),
// so venv/bin/python -> ../tools/<entry>/bin/python3 survives moving the
// whole tree, and the interpreter resolves its real prefix through the
// link (stdlib found, no /install fallback).
//
// Only venv/bin is rewritten. Do not walk the rest of the payload: a
// file-symlink at Foo.framework/Foo is the framework binary, and
// flattening it makes codesign report "bundle format is ambiguous".
//
// A symlink whose target points OUTSIDE the bundle is a build bug (the
// payload's own venv must never reference the builder's machine) — fail
// loudly rather than ship a dangling link.

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

/**
 * Rewrite payload-internal absolute symlinks under venv/bin to RELATIVE
 * ones. Returns the count rewritten.
 *
 * The link's current target is the BUILD staging path (build/agent-payload/
 * tools/<entry>/bin/python3) — it must not be shipped verbatim (it dangles
 * on the user's machine). The store lives INSIDE the payload at
 * <payload>/tools/<entry>/..., so the link is rewritten to point at the
 * payload's own store tree, relative to venv/bin. Throws only if the
 * store entry the link names does not exist in the payload at all (a
 * genuinely broken bundle).
 */
export function relativizePayloadLinks(root) {
  if (!root || !fs.existsSync(root)) return 0
  const bin = path.join(root, 'venv', 'bin')
  if (!fs.existsSync(bin)) return 0
  const payloadRoot = path.resolve(root)
  const toolsDir = path.join(payloadRoot, 'tools')
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
    if (!st.isSymbolicLink()) continue
    let target
    try {
      target = fs.readlinkSync(p)
    } catch {
      continue
    }
    // The tail of the target (tools/<entry>/bin/python3) is what names the
    // store entry; it must exist inside THIS payload's tools/.
    const m = target.match(/(?:^|\/)tools\/(.+)$/)
    if (!m) {
      // A sibling link (e.g. python3 -> python) is already relative and
      // resolves through the rewritten store link — leave it alone.
      const sibling = path.resolve(bin, target)
      if (sibling !== bin && sibling.startsWith(bin + path.sep) && fs.existsSync(sibling)) {
        continue
      }
      throw new Error(
        `venv/bin/${ent.name} -> ${target} does not name a store entry (tools/<entry>/...); ` +
        'a bundled venv must reference the payload store',
      )
    }
    const inPayload = path.join(toolsDir, m[1])
    if (!fs.existsSync(inPayload)) {
      throw new Error(
        `venv/bin/${ent.name} -> ${target}: store entry ${m[1]} is not present in the payload ` +
        `(${inPayload}); a bundled venv must reference the payload store`,
      )
    }
    const rel = path.relative(bin, inPayload)
    if (target === rel) continue
    fs.unlinkSync(p)
    fs.symlinkSync(rel, p)
    count += 1
  }
  return count
}
