// msix-shared.mjs — the shared MSIX-distribution building blocks used by
// BOTH the out-of-store feed generator (apps/desktop/scripts/gen-appinstaller.mjs)
// and the release job that stages the feed (scripts/stage-msixbundle.mjs).
//
// The two call sites must agree on every name/URL that Windows keys on — the
// .appinstaller's MainPackage identity and the bundle URI — so the XML
// builder and the version/filename derivations live here, once.

import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)

// The out-of-store MSIX publisher — the ATS signing cert subject, which is
// what Windows compares against the package manifest publisher at install.
// Mirrored from electron-builder.config.cjs so the .appinstaller and the
// manifest can never drift.
export const OUT_OF_STORE_PUBLISHER =
  'CN=Nous Research Inc., O=Nous Research Inc., L=Austin, S=Texas, C=US'

// Content-Type for MSIX / App Installer artifacts. Without the right MIME the
// browser cannot hand a clicked .appinstaller / .msixbundle to the OS App
// Installer (it would download as octet-stream instead). Everything else
// stays octet-stream (R2's default) unchanged. Keys match by filename suffix,
// case-insensitively.
const CONTENT_TYPES = {
  '.appinstaller': 'application/appinstaller',
  '.msixbundle': 'application/msixbundle',
  '.msix': 'application/msix',
  // Termux APT repo artifacts (uploaded under releases/termux/<channel>/).
  // InRelease/Release/Packages are extensionless; match by exact basename
  // too so apt gets text/plain instead of octet-stream.
  '.deb': 'application/vnd.debian.binary-package',
  '.gz': 'application/gzip',
  'release.gpg': 'application/pgp-signature',
  inrelease: 'text/plain',
  release: 'text/plain',
  packages: 'text/plain'
}

/**
 * The Content-Type to store for a staged release artifact, if any.
 *
 * Keys starting with '.' (or containing one, like 'release.gpg') match by
 * filename suffix. Extensionless keys (inrelease/release/packages — the apt
 * repo metadata) match by exact basename only, so 'foo-release' or
 * 'xrelease' never collide with the apt 'Release' file.
 */
export function contentTypeFor(filename) {
  const lower = String(filename).toLowerCase()
  const base = lower.slice(lower.lastIndexOf('/') + 1)
  for (const [key, mime] of Object.entries(CONTENT_TYPES)) {
    if (key.includes('.')) {
      if (lower.endsWith(key)) return mime
    } else if (base === key) {
      return mime
    }
  }
  return undefined
}

function escapeAttr(value) {
  return String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
}

/**
 * Build an .appinstaller document for a channel.
 *
 * @param {{
 *   baseUrl: string             // feed host root (no trailing slash)
 *   variantChannelPath: string  // e.g. "win32/", "win32/light/", "win32/nightly/"
 *   identityName: string        // package Identity Name (e.g. "NousResearch.HermesBundled")
 *   version: string             // 4-part MSIX version, e.g. "1.2.3.0"
 *   bundleFilename: string      // the universal .msixbundle filename in the feed dir
 * }} o
 * @returns {string} the .appinstaller XML
 */
export function buildAppInstaller(o) {
  const bundleUrl = `${o.baseUrl}/${o.variantChannelPath.replace(/\/+$/, '')}/${o.bundleFilename}`
  const appinstallerUri = bundleUrl.replace(/\.msixbundle$/, '.appinstaller')

  return [
    '<?xml version="1.0" encoding="utf-8"?>',
    '<AppInstaller',
    `  Uri="${escapeAttr(appinstallerUri)}"`,
    `  Version="${escapeAttr(o.version)}"`,
    '  xmlns="http://schemas.microsoft.com/appx/appinstaller/2017/2">',
    '  <MainPackage',
    `    Name="${escapeAttr(o.identityName)}"`,
    `    Publisher="${escapeAttr(OUT_OF_STORE_PUBLISHER)}"`,
    `    Version="${escapeAttr(o.version)}"`,
    `    Uri="${escapeAttr(bundleUrl)}" />`,
    '  <UpdateSettings>',
    '    <OnLaunch HoursBetweenUpdateChecks="12" ShowPrompt="false" />',
    '  </UpdateSettings>',
    '</AppInstaller>',
    ''
  ].join('\n')
}

/**
 * Resolve the app identity for a desktop build from the app dir: the product
 * identity + package version. Pure-ish (reads product-identity.cjs and
 * package.json from the app dir) so callers on any runner can derive the
 * exact feed filename/identity without duplicating the derivation.
 *
 * @param {string} desktopDir absolute apps/desktop path
 * @returns {{ identity: object, version: string, name: string }}
 */
export function appIdentity(desktopDir) {
  const identity = require(path.join(desktopDir, 'product-identity.cjs'))
  const pkg = JSON.parse(fs.readFileSync(path.join(desktopDir, 'package.json'), 'utf8'))
  return { identity, version: `${pkg.version}.0`, name: identity.appNamePascal }
}
