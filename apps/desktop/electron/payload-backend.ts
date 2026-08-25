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
  const venvPython = deps.isWindows
    ? path.join(venvDir, 'Scripts', 'python.exe')
    : path.join(venvDir, 'bin', 'python')

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
