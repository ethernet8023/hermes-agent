/**
 * payload-backend.ts
 *
 * The bundled-install backend: a pm payload staged by `hermes pm bundle`
 * and shipped under resources/agent-payload. The payload carries the repo
 * snapshot, the tool store (with facts.json), and a relocatable venv
 * built on the staged python-build-standalone interpreter.
 *
 * Electron's whole job here is finding the interpreter and re-pointing
 * the venv at the machine's install location once. Everything else —
 * managed tool PATHs, env composition — happens in-process via pm when
 * the backend runs.
 *
 * The one first-boot step that CANNOT live in python: pyvenv.cfg `home`
 * is an absolute path baked on the build runner, and the venv's python
 * cannot boot at all until it points at the shipped base interpreter.
 * adoptPayloadVenv() rewrites it from the payload's own facts.json.
 */

import fs from 'node:fs'
import path from 'node:path'
import { createHash } from 'node:crypto'

export interface PayloadInfo {
  root: string
  repoDir: string
  toolsDir: string
  venvPython: string
}

export function resolvePayload(
  resourcesPath: string | undefined,
  deps: {
    fileExists: (p: string) => boolean
    directoryExists: (p: string) => boolean
    isWindows: boolean
  }
): PayloadInfo | null {
  if (!resourcesPath) {
    return null
  }

  const root = path.join(resourcesPath, 'agent-payload')
  const manifestPath = path.join(root, 'manifest.json')

  if (!deps.fileExists(manifestPath)) {
    return null
  }

  let manifest: any

  try {
    manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'))
  } catch {
    return null
  }

  if (!manifest || manifest.external === true) {
    return null
  }

  // The manifest names the payload layout; a bundle that omits the fields
  // is malformed — refuse it rather than guess at cmd_bundle's layout.
  if (typeof manifest.repo !== 'string' || typeof manifest.store !== 'string' || typeof manifest.venv !== 'string') {
    return null
  }

  const repoDir = path.join(root, manifest.repo)
  const toolsDir = path.join(root, manifest.store)
  const venvDir = path.join(root, manifest.venv)
  const venvPython = deps.isWindows ? path.join(venvDir, 'Scripts', 'python.exe') : path.join(venvDir, 'bin', 'python')

  if (!deps.directoryExists(repoDir) || !deps.fileExists(venvPython)) {
    return null
  }

  return { root, repoDir, toolsDir, venvPython }
}

/**
 * Re-point the payload venv's pyvenv.cfg at the shipped interpreter.
 * Reads the python entry from the payload's facts.json; idempotent (a
 * matching `home` line is left untouched). Returns true when the cfg is
 * usable after the call.
 */
export function adoptPayloadVenv(
  payload: PayloadInfo,
  deps: { isWindows: boolean; log?: (m: string) => void }
): boolean {
  const cfgPath = path.join(path.dirname(path.dirname(payload.venvPython)), 'pyvenv.cfg')

  let facts: any

  try {
    facts = JSON.parse(fs.readFileSync(path.join(payload.toolsDir, 'facts.json'), 'utf8'))
  } catch {
    return false
  }

  const pythonFact = facts?.packages?.python

  if (!pythonFact?.entry) {
    return false
  }

  const entry = path.join(payload.toolsDir, pythonFact.entry)
  const home = deps.isWindows ? entry : path.join(entry, 'bin')

  let text: string

  try {
    text = fs.readFileSync(cfgPath, 'utf8')
  } catch {
    return false
  }

  const lines = text.split(/\r?\n/)
  const current = lines.find(line => line.toLowerCase().startsWith('home ='))

  if (current && current.slice('home ='.length).trim() === home) {
    return true
  }

  const fixed = lines.map(line => (line.toLowerCase().startsWith('home =') ? `home = ${home}` : line))

  try {
    fs.writeFileSync(cfgPath, fixed.join('\n'), 'utf8')
    deps.log?.(`[payload] re-pointed venv home at ${home}`)

    return true
  } catch (error: any) {
    deps.log?.(`[payload] could not adopt venv: ${error.message}`)

    return false
  }
}

// ─── update channel ─────────────────────────────────────────────────────────
//
// Pure mirrors of hermes_cli/update_channel.py: the CLI owns the channel
// records; Electron only reads them for the version pill and the updater's
// feed selection. Keep the three shapes byte-compatible:
//   - install id: sha16 of the canonical install-root PATH,
//   - nightly tag regex,
//   - update.installs.<sha16>.channel in config.yaml.

export type UpdateChannel = 'stable' | 'main' | 'nightly'

/**
 * The install id of the tree at `root`: sha16 of the canonical PATH,
 * byte-identical to Python's install id (sha256 of the resolved root,
 * first 16 hex chars — `boot_bootstrap._install_key` /
 * `update_channel._install_key_sha16`). Path-derived so it survives
 * artifact replacement at the same location; the same key names
 * `installs/<sha16>/`.
 */
export function installIdForRoot(root: string, canonicalize: (p: string) => string = p => p): string {
  return createHash('sha256').update(canonicalize(root), 'utf8').digest('hex').slice(0, 16)
}

/**
 * A nightly release tag: `v<major>.<minor>.<patch>-nightly.<YYYYMMDDHHMMSS>`,
 * or the legacy date-only shape. Mirrors `_NIGHTLY_TAG_RE` in
 * hermes_cli/update_channel.py — both key off the same stamp tag.
 */
export function isNightlyTag(tag: string | null | undefined): boolean {
  return typeof tag === 'string' && /^v(?:0|[1-9]\d{0,2})\.\d+\.\d+-nightly\.20\d{6}(?:\d{6})?$/.test(tag.trim())
}

/**
 * The channel an install with no per-install record tracks.
 *
 * An electron-updater artifact follows the feed it was itself published
 * to: a nightly tag means the nightly feed, anything else the stable
 * feed. Defaulting a nightly artifact to stable would make it ask for
 * its nightly feed file under the newest STABLE release — a 404 that
 * leaves the install permanently unable to update. Everything else
 * defaults to main, the source-checkout default.
 */
export function defaultUpdateChannel(
  stampTag: string | null | undefined,
  mechanism: string | null | undefined
): UpdateChannel {
  if (mechanism !== 'electron-updater') {
    return 'main'
  }

  return isNightlyTag(stampTag) ? 'nightly' : 'stable'
}

/**
 * The update channel of the install with id `installId`, read from
 * config.yaml text (`update.installs.<sha16>.channel` — the per-install
 * record `hermes update --set-channel` writes; there is no home-global
 * channel key). With no explicit record for THIS install, the answer is
 * the artifact's own default channel (`defaultUpdateChannel`) — callers
 * pass the stamp facts so a nightly bundle tracks nightly; omitting them
 * keeps the source-checkout `main`.
 *
 * The parser is deliberately narrow: find the top-level `update:` block,
 * the `installs:` block inside it, then the `<installId>:` block, then its
 * `channel:`. config.yaml is machine-written here, so this shape is stable.
 */
export function updateChannelFromConfig(
  configText: string | null | undefined,
  installId: string,
  stampTag: string | null = null,
  mechanism: string | null = null
): UpdateChannel {
  const fallback = defaultUpdateChannel(stampTag, mechanism)

  if (!configText || !installId) {
    return fallback
  }

  // Depth by indentation: update: (0) → installs: (>0) → <sha16>: (deeper) →
  // channel: (deeper still). Track the indent at which each block opened so
  // a sibling key at the same depth closes it.
  let updateIndent: number | null = null
  let installsIndent: number | null = null
  let idIndent: number | null = null

  for (const raw of configText.split('\n')) {
    const line = raw.replace(/\s+$/, '')

    if (!line || /^\s*#/.test(line)) {
      continue
    }

    const indent = line.length - line.replace(/^\s+/, '').length
    const key = line.replace(/^\s+/, '')

    if (updateIndent === null) {
      if (/^update:\s*$/.test(line)) {
        updateIndent = indent
      }

      continue
    }

    if (indent <= updateIndent) {
      break // the update block ended
    }

    if (installsIndent === null) {
      if (/^installs:\s*$/.test(key)) {
        installsIndent = indent
      }

      continue
    }

    if (indent <= installsIndent) {
      installsIndent = null
      idIndent = null

      continue
    }

    if (idIndent === null) {
      if (new RegExp(`^${installId}:\s*$`).test(key)) {
        idIndent = indent
      }

      continue
    }

    if (indent <= idIndent) {
      idIndent = null

      continue
    }

    const match = key.match(/^channel:\s*["']?(stable|main|nightly)["']?\s*(#.*)?$/)

    if (match) {
      return match[1] as UpdateChannel
    }
  }

  return fallback
}

/**
 * Pick the newest final release tag (vX.Y.Z, no prerelease suffix) from
 * `git ls-remote --tags` output. Numeric ordering, so v0.10.0 > v0.9.0.
 * Returns null when the output has no final release tag.
 *
 * A peeled entry (`refs/tags/v1.2.3^{}`) resolves the commit that an
 * annotated tag points at. It wins over the unpeeled line of the same tag.
 */
export function latestReleaseFromLsRemote(output: string): { tag: string; sha: string } | null {
  const versions = new Map<string, { key: [number, number, number]; sha: string; peeled: boolean }>()

  for (const line of output.split('\n')) {
    // The major component is capped at three digits: the historical CalVer
    // tags (v2026.7.20) would win every numeric sort. This mirrors
    // _RELEASE_TAG_RE in hermes_cli/update_cmd.py and _SEMVER_TAG_RE in
    // scripts/write_install_stamp.py.
    const m = line.match(/^([0-9a-f]{40})\trefs\/tags\/(v(?:0|[1-9]\d{0,2})\.\d+\.\d+)(\^\{\})?$/)

    if (!m) {
      continue
    }

    const [, sha, tag, peel] = m
    const existing = versions.get(tag)

    if (!existing || (peel && !existing.peeled)) {
      const [major, minor, patch] = tag.slice(1).split('.').map(Number)

      versions.set(tag, { key: [major, minor, patch], sha, peeled: Boolean(peel) })
    }
  }

  let best: { tag: string; sha: string; key: [number, number, number] } | null = null

  for (const [tag, { key, sha }] of versions) {
    const newer =
      !best ||
      key[0] > best.key[0] ||
      (key[0] === best.key[0] && (key[1] > best.key[1] || (key[1] === best.key[1] && key[2] > best.key[2])))

    if (newer) {
      best = { tag, sha, key }
    }
  }

  return best ? { tag: best.tag, sha: best.sha } : null
}
