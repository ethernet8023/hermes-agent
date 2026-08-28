#!/usr/bin/env node
// stage-msixbundle.mjs — the out-of-store MSIX distribution job.
//
// Runs on a Windows runner of the release workflow AFTER all legs built
// (needs: build). Two responsibilities:
//
//  1. OUT-OF-STORE FEED: bundle the x64 + arm64 per-arch .msix into one
//     universal .msixbundle, sign the bundle envelope, write the per-channel
//     .appinstaller, and upload both to the win32 feed dirs:
//         releases/win32/<stable|nightly>/<name>-<ver>.win.msixbundle
//         releases/win32/<stable|nightly>/stable.appinstaller (or nightly.*)
//     The .appinstaller is the install + auto-update entry point; the bundle
//     is what the OS installs and swaps on update. Per-arch .msix files stay
//     in the immutable releases/tag/<tag>/ archive (uploaded by the legs).
//
//  2. STORE ARCHIVE: re-upload the Store-submission .msix files (built by
//     the win legs, prefixed Store-) to the tag archive. The Store is the
//     distribution for those — they never touch a feed dir.
//
// Usage (win runner, bash):
//   node scripts/stage-msixbundle.mjs --tag vX.Y.Z [--variant bundled|light]
// Reads HERMES_DESKTOP_VARIANT (bundled|light) from the environment; the
// workflow runs this job once per variant.
import { createHash } from 'node:crypto'
import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { appIdentity, buildAppInstaller } from './msix-shared.mjs'

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

// node strips the first '--' (and an immediately-following option) for its
// own use; parse space-separated flag pairs, not --flag=value.
const args = process.argv.slice(2)
const flagValue = (name) => {
  for (let i = 0; i < args.length - 1; i += 1) {
    if (args[i] === name) return args[i + 1]
  }
  return undefined
}
const tag = flagValue('--tag')
const variant = flagValue('--variant') || process.env.HERMES_DESKTOP_VARIANT || 'bundled'

if (!tag) {
  console.error('[stage-msixbundle] --tag=<vX.Y.Z> is required')
  process.exit(1)
}
if (!['bundled', 'light'].includes(variant)) {
  console.error(`[stage-msixbundle] --variant must be 'bundled' or 'light', got '${variant}'`)
  process.exit(1)
}
if (process.platform !== 'win32') {
  console.error('[stage-msixbundle] this job must run on a Windows runner (makeappx + signtool)')
  process.exit(1)
}

const nightly = /-nightly\./.test(tag)
const channel = nightly ? 'nightly' : 'stable'
const channelDir = `releases/win32/${variant === 'light' ? 'light/' : ''}${channel}`

const desktop = path.join(REPO_ROOT, 'apps', 'desktop')
const releaseDir = path.join(desktop, 'release')
const { identity, version, name } = appIdentity(desktop)

function msixFile(arch) {
  return path.join(releaseDir, `${name}-${version}-win-${arch}.msix`)
}
function bundleFile() {
  return path.join(releaseDir, `${name}-${version}-win.msixbundle`)
}

// ── makeappx / signtool resolution (SDK BuildTools nuget) ──────────────────
// electron-builder downloads Microsoft.Windows.SDK.BuildTools into its winCodeSign
// cache; this job uses the SAME pin so makeappx/signtool match the builder's.
function resolveWinSdkTools() {
  const candidates = []
  const roots = [
    path.join(process.env.LOCALAPPDATA || '', 'electron-builder', 'Cache', 'winCodeSign'),
    path.join(process.env.LOCALAPPDATA || '', 'electron-builder', 'cache', 'winCodeSign'),
    path.join(process.env.USERPROFILE || '', 'AppData', 'Local', 'electron-builder', 'Cache', 'winCodeSign')
  ]
  for (const root of roots) {
    if (!root || !fs.existsSync(root)) continue
    for (const entry of fs.readdirSync(root)) {
      const dir = path.join(root, entry)
      if (!fs.statSync(dir).isDirectory()) continue
      // The nuget PACKAGE version (e.g. 10.0.26100.4948) is not the SDK BUILD
      // the inner bin/<ver>/x64 folder carries — glob bin/*/x64 and require
      // exactly one match (see the msix-packaging reference).
      const binDir = path.join(dir, 'bin')
      if (!fs.existsSync(binDir)) continue
      for (const sub of fs.readdirSync(binDir)) {
        const x64 = path.join(binDir, sub, 'x64')
        if (fs.existsSync(path.join(x64, 'makeappx.exe')) && fs.existsSync(path.join(x64, 'signtool.exe'))) {
          candidates.push(x64)
        }
      }
    }
  }
  if (candidates.length === 0) {
    console.error('[stage-msixbundle] no makeappx/signtool found under electron-builder winCodeSign cache')
    process.exit(1)
  }
  // Prefer the newest SDK build.
  candidates.sort((a, b) => (a < b ? -1 : a > b ? 1 : 0))
  return candidates[candidates.length - 1]
}

const winSdk = resolveWinSdkTools()
const makeappx = path.join(winSdk, 'makeappx.exe')
const signtool = path.join(winSdk, 'signtool.exe')

// ── 1. bundle ──────────────────────────────────────────────────────────────
const x64 = msixFile('x64')
const arm64 = msixFile('arm64')
const bundle = bundleFile()
if (!fs.existsSync(x64) || !fs.existsSync(arm64)) {
  console.error(`[stage-msixbundle] need both per-arch msix to bundle:\n  ${x64}\n  ${arm64}`)
  process.exit(1)
}

// makeappx bundle /d includes EVERY .msix in the dir — the Store-submission
// packages (Store-*.msix, same release dir after the legs merged their
// artifacts) must never ride inside the out-of-store bundle. Stage only the
// two per-arch packages into a clean dir before bundling.
const bundleStaging = path.join(releaseDir, '__bundle-staging')
fs.rmSync(bundleStaging, { recursive: true, force: true })
fs.mkdirSync(bundleStaging, { recursive: true })
fs.copyFileSync(x64, path.join(bundleStaging, path.basename(x64)))
fs.copyFileSync(arm64, path.join(bundleStaging, path.basename(arm64)))

if (fs.existsSync(bundle)) fs.rmSync(bundle, { force: true })
execFileSync(makeappx, ['bundle', '/o', '/bv', version, '/d', bundleStaging, '/p', bundle], { stdio: 'inherit' })

// Sign ONLY the bundle envelope; the inner .msix keep their build-leg
// signatures. Runs only when the Azure vars are present (fork without them
// ships unsigned — same posture as the build legs).
if (process.env.AZURE_SIGN_ENDPOINT && process.env.AZURE_SIGN_ACCOUNT && process.env.AZURE_SIGN_PROFILE) {
  const dlib = resolveTrustedSigningDlib()
  if (dlib) {
    const metaPath = path.join(releaseDir, 'msixbundle-sign.json')
    fs.writeFileSync(metaPath, JSON.stringify({
      Endpoint: process.env.AZURE_SIGN_ENDPOINT,
      CodeSigningAccountName: process.env.AZURE_SIGN_ACCOUNT,
      CertificateProfileName: process.env.AZURE_SIGN_PROFILE
    }))
    execFileSync(signtool, [
      'sign', '/fd', 'SHA256', '/td', 'SHA256', '/tr', 'http://timestamp.acs.microsoft.com',
      '/dlib', dlib, '/dmdf', metaPath, bundle
    ], { stdio: 'inherit' })
    execFileSync(signtool, ['verify', '/pa', bundle], { stdio: 'inherit' })
  } else {
    console.warn('[stage-msixbundle] Azure Trusted Signing dlib not found — bundle will be UNSIGNED')
  }
} else {
  console.warn('[stage-msixbundle] AZURE_SIGN_* not set — bundle will be UNSIGNED')
}

function resolveTrustedSigningDlib() {
  const roots = [
    path.join(process.env.LOCALAPPDATA || '', 'electron-builder', 'Cache', 'winCodeSign'),
    path.join(process.env.USERPROFILE || '', 'AppData', 'Local', 'electron-builder', 'Cache', 'winCodeSign')
  ]
  for (const root of roots) {
    if (!root || !fs.existsSync(root)) continue
    const found = []
    for (const entry of fs.readdirSync(root)) {
      const dir = path.join(root, entry)
      if (!fs.statSync(dir).isDirectory()) continue
      const walk = (p) => {
        if (!fs.existsSync(p)) return
        if (fs.statSync(p).isDirectory()) {
          for (const child of fs.readdirSync(p)) walk(path.join(p, child))
        } else if (path.basename(p).toLowerCase() === 'azure.codesigning.dlib.dll') {
          found.push(p)
        }
      }
      walk(dir)
    }
    if (found.length > 0) return found[0]
  }
  return null
}

// ── 2. .appinstaller + uploads ─────────────────────────────────────────────
const baseUrl = String(process.env.CLOUDFLARE_R2_PUBLIC_URL || '').replace(/\/+$/, '')
if (!baseUrl) {
  console.error('[stage-msixbundle] CLOUDFLARE_R2_PUBLIC_URL is required (feed dir URLs come from it)')
  process.exit(1)
}

const appinstaller = buildAppInstaller({
  baseUrl,
  variantChannelPath: channelDir,
  identityName: identity.msixAppIdWithOrg,
  version,
  bundleFilename: `${name}-${version}-win.msixbundle`
})
const appinstallerName = `${channel}.appinstaller`
fs.writeFileSync(path.join(releaseDir, appinstallerName), appinstaller)

const upload = (key, file, keyIsFull = false) => {
  const buf = fs.readFileSync(file)
  const sha = createHash('sha256').update(buf).digest('hex')
  console.log(`[stage-msixbundle] upload ${key} (${buf.length} bytes, sha256 ${sha})`)
  // Feed-dir keys are FULL object keys (releases/win32/<ch>/…) — pass
  // --key-is-full so r2 put does NOT wrap them under releases/tag/<tag>/.
  // r2-release.mjs put derives Content-Type from the key extension.
  execFileSync(process.execPath, ['scripts/r2-release.mjs', 'put', '--tag', tag, '--key', key, '--file', file, ...(keyIsFull ? ['--key-is-full'] : [])], {
    cwd: REPO_ROOT,
    stdio: 'inherit'
  })
}

// Feed dir manifests (the install + update source). Content-Types matter:
// .appinstaller / .msixbundle must reach the OS App Installer, not download.
upload(`${channelDir}/${appinstallerName}`, path.join(releaseDir, appinstallerName), true)
upload(`${channelDir}/${name}-${version}-win.msixbundle`, bundle, true)

// The Store-submission .msix files were already uploaded to the tag archive
// by the win legs (Store- prefix); nothing for this job to re-upload.
console.log('[stage-msixbundle] done — feed manifests + bundle staged')
