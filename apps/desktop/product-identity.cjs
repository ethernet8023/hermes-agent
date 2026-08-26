// The desktop product identity — THE single source for every name-shaped
// value a variant owns. HERMES_DESKTOP_VARIANT=light builds "Hermes
// Light", the remote-only client; everything else is full "Hermes".
//
// Consumed at build time by electron-builder.config.cjs (packaging
// identity). electron/product-identity.ts is the typed runtime accessor.
// @ts-check
/// <reference types="node" />
'use strict'

const variants = {
  '': { display: 'Hermes', kebab: 'hermes', pascal: 'Hermes' },
  light: {
    display: 'Hermes Light',
    kebab: 'hermes-light',
    pascal: 'HermesLight'
  },
  bundled: {
    display: 'Hermes',
    kebab: 'hermes-bundled',
    pascal: 'HermesBundled'
  }
}

const variant = process.env.HERMES_DESKTOP_VARIANT
if (variant !== 'light' && variant !== 'bundled' && variant !== '' && variant !== undefined) {
  throw new Error(`Unknown HERMES_DESKTOP_VARIANT ${variant}. expected one of (empty), light, bundled`)
}

const name = variants[variant ?? '']

const light = process.env.HERMES_DESKTOP_VARIANT === 'light'

// The electron-updater feed channel this build PUBLISHES to. A nightly
// tag (vX.Y.0-nightly.YYYYMMDDHHMMSS) writes nightly.yml / light-nightly.yml;
// stable tags write latest.yml / light.yml. Keyed on the payload tag so
// the one release workflow serves both channels — a nightly build can
// never overwrite the stable feed file, and vice versa.
const nightly = /-nightly\.20\d{6}(?:\d{6})?$/.test(process.env.HERMES_PAYLOAD_TAG || '')

/** @typedef {import("./product-identity.d.cts")} ProductIdentity */

/** @type {ProductIdentity} */
const identity = {
  light,
  displayName: name.display,
  appId: `com.nousresearch.${name.kebab}`,
  channel: light ? (nightly ? 'light-nightly' : 'light') : (nightly ? 'nightly' : 'latest'),
  appNamePascal: name.pascal,
  msixAppIdWithOrg: `NousResearch.${name.pascal}`
}

module.exports = identity
