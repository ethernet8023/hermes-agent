// Content-addressed store for signed Windows binaries. Pure filesystem
// primitives — no electron-builder imports — so the core is testable and
// reusable from any hook. Entries are keyed by the sha256 of the UNSIGNED
// file content and hold the signed bytes, so an identical rebuild of the
// same input skips the (slow, remote) Azure Trusted Signing round-trip.

import crypto from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'

/**
 * @param {Buffer} buffer
 * @returns {string} sha256 hex digest of the buffer
 */
export function contentHash(buffer) {
  return crypto.createHash('sha256').update(buffer).digest('hex')
}

/**
 * @param {string} dir cache directory
 * @param {string} key content hash of the unsigned file
 * @returns {string | null} path to the cached signed entry, or null on miss
 */
export function lookupSigned(dir, key) {
  const entry = path.join(dir, key)
  return fs.existsSync(entry) ? entry : null
}

/**
 * Store `buffer` under `<dir>/<key>` atomically: write to a unique temp file
 * in the SAME directory, then rename (same-dir rename is atomic on every
 * platform we build on), so a reader never observes a partial entry.
 *
 * @param {string} dir cache directory (created if missing)
 * @param {string} key content hash of the unsigned file
 * @param {Buffer} buffer signed file bytes
 * @returns {string} path to the stored entry
 */
export function storeSigned(dir, key, buffer) {
  fs.mkdirSync(dir, { recursive: true })
  const target = path.join(dir, key)
  const tmp = path.join(dir, `.tmp-${process.pid}-${crypto.randomBytes(6).toString('hex')}`)
  fs.writeFileSync(tmp, buffer)
  try {
    fs.renameSync(tmp, target)
  } catch (err) {
    // Windows can refuse the rename with EEXIST/EPERM when a concurrent
    // build already landed this key. Content-addressed means same key =
    // same bytes, so an existing target IS success — drop the temp file.
    if ((err.code === 'EEXIST' || err.code === 'EPERM') && fs.existsSync(target)) {
      fs.rmSync(tmp, { force: true })
      return target
    }
    fs.rmSync(tmp, { force: true })
    throw err
  }
  return target
}
