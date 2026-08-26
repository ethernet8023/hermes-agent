// electron-builder custom win.sign hook: Azure Trusted Signing behind a
// content-addressed cache. Signing is the slowest, flakiest part of the
// Windows release build — every file is a remote round-trip — and rebuilds
// of unchanged inputs produce byte-identical unsigned binaries. Keyed by the
// unsigned content hash, a rebuild reuses yesterday's signature instead of
// re-signing (Authenticode signatures embed a timestamp, not an expiry tied
// to the build, so replaying a previously signed copy is exactly as valid).
//
// Wired from electron-builder.config.cjs as
//   win.sign = { type: 'signtool', sign: './scripts/sign-cached.mjs', ... }
// The Azure endpoint/account/profile do NOT ride through the config: this
// module reads the AZURE_SIGN_* environment variables directly
// (azureConfigFromEnv) and hands them to app-builder-lib's own
// WindowsSignAzureManager, so the actual signing path is byte-for-byte the
// one a plain { type: 'azure' } config would run.

import fs from 'node:fs'
import { createRequire } from 'node:module'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

import { contentHash, lookupSigned, storeSigned } from './sign-cache.mjs'

const require = createRequire(import.meta.url)
const desktopDir = path.dirname(path.dirname(fileURLToPath(import.meta.url)))

/**
 * Cache directory: HERMES_SIGN_CACHE (CI points this at a persisted
 * actions/cache path) or apps/desktop/build/sign-cache for local builds.
 *
 * @param {NodeJS.ProcessEnv} [env]
 */
export function resolveCacheDir(env = process.env) {
  return env.HERMES_SIGN_CACHE || path.join(desktopDir, 'build', 'sign-cache')
}

/**
 * @param {NodeJS.ProcessEnv} [env]
 * @returns {{ type: 'azure' } & Record<string, string | undefined>} the
 *   win.sign azure configuration, composed from the environment.
 */
export function azureConfigFromEnv(env = process.env) {
  return {
    type: 'azure',
    endpoint: env.AZURE_SIGN_ENDPOINT,
    codeSigningAccountName: env.AZURE_SIGN_ACCOUNT,
    certificateProfileName: env.AZURE_SIGN_PROFILE,
    publisherName: env.AZURE_SIGN_PUBLISHER
  }
}

/**
 * Sign `file` in place via `signer()`, unless the cache already holds a
 * signed copy for its current (unsigned) content.
 *
 * @param {string} file path to the binary electron-builder wants signed
 * @param {string} cacheDir content-addressed store directory
 * @param {() => Promise<unknown>} signer signs `file` in place
 */
export async function signWithCache(file, cacheDir, signer) {
  const key = contentHash(fs.readFileSync(file))
  const name = path.basename(file)
  const hit = lookupSigned(cacheDir, key)
  if (hit) {
    fs.copyFileSync(hit, file)
    console.log(`[sign-cached] cache hit ${name} (${key.slice(0, 12)})`)
    return
  }
  await signer()
  storeSigned(cacheDir, key, fs.readFileSync(file))
  console.log(`[sign-cached] signed + stored ${name} (${key.slice(0, 12)})`)
}

// app-builder-lib's exports map exposes only "." and "./internal";
// import('app-builder-lib/dist/...') throws ERR_PACKAGE_PATH_NOT_EXPORTED.
// Resolve the class the way run-electron-builder.mjs finds the CLI: resolve
// the entry module, walk up to the package root, then direct-file import
// (file URLs bypass the exports map). The constructibility of this class is
// pinned by the tripwire test in sign-cached.test.mjs, so a builder bump
// that moves it fails js-tests instead of a release build.
async function loadAzureManagerClass() {
  const entry = require.resolve('app-builder-lib')
  let root = path.dirname(entry)
  while (!fs.existsSync(path.join(root, 'package.json'))) {
    const parent = path.dirname(root)
    if (parent === root) throw new Error('app-builder-lib package root not found')
    root = parent
  }
  const mod = await import(
    pathToFileURL(path.join(root, 'dist', 'codeSign', 'win', 'windowsSignAzureManager.js')).href
  )
  return mod.WindowsSignAzureManager
}

// One manager per process: electron-builder calls the hook once per file,
// and the manager memoizes toolset downloads / dlib metadata behind it.
let managerPromise = null

function azureManager(packager) {
  if (managerPromise == null) {
    managerPromise = (async () => {
      const WindowsSignAzureManager = await loadAzureManagerClass()
      // The manager's constructor re-derives the signing config from
      // packager.platformOptions.sign and throws unless type === 'azure' —
      // but our config's win.sign is the { type: 'signtool' } hook wiring.
      // Shim the packager with the azure config composed from the
      // environment; everything else (config.toolsets, buildResourcesDir,
      // getTempFile) delegates to the real packager via the prototype.
      const shim = Object.create(packager)
      Object.defineProperty(shim, 'platformOptions', {
        value: { ...packager.platformOptions, sign: azureConfigFromEnv() }
      })
      const manager = new WindowsSignAzureManager(shim)
      // No-op on the modern signtool /dlib path (winCodeSign >= 1.3.0);
      // installs the legacy PowerShell module otherwise.
      await manager.initialize()
      return manager
    })()
  }
  return managerPromise
}

/**
 * The electron-builder custom sign hook.
 *
 * @param {{ path: string }} configuration CustomWindowsSignTaskConfiguration
 * @param {any} packager WinPackager
 */
export default async function sign(configuration, packager) {
  const mgr = await azureManager(packager)
  await signWithCache(configuration.path, resolveCacheDir(), () =>
    // signFileWithDlib reads only options.path (plus the manager's own
    // signing config), so platformOptions is sufficient here.
    mgr.signFile({ path: configuration.path, options: packager.platformOptions })
  )
}
