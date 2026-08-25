/**
 * Desktop bundles ship precompiled renderer assets. Returning false here tells
 * electron-builder to skip the node_modules collector/install step, which
 * avoids workspace dependency graph explosions and keeps packaging
 * deterministic across environments.
 *
 * Also guarantees build/agent-payload exists: extraResources copies it on
 * every build, and electron-builder's behavior for a missing `from` varies
 * between versions. A bundled build stages the real payload there first
 * (`hermes pm bundle --out apps/desktop/build/agent-payload`); anything else
 * gets a stub manifest with external:true, which resolvePayload() treats as
 * "no payload" so the backend resolver falls through to the runtime rungs.
 */
import fs from 'node:fs'
import path from 'node:path'

export default async function beforeBuild() {
  const payloadDir = path.join(import.meta.dirname, '..', 'build', 'agent-payload')
  const manifest = path.join(payloadDir, 'manifest.json')

  if (!fs.existsSync(manifest)) {
    fs.mkdirSync(payloadDir, { recursive: true })
    fs.writeFileSync(manifest, JSON.stringify({ schema: 1, external: true }, null, 2) + '\n')
  }

  return false
}
