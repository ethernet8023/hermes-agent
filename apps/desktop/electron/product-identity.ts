// product-identity.ts — the typed build-time product identity.
//
// product-identity.cjs is the single derivation of every name-shaped
// value a variant owns (product name, appId, deep-link scheme, channel).
// bundle-electron-main.mjs bakes that object into the production bundle
// by defining the __HERMES_PRODUCT_IDENTITY__ global — the same
// mechanism as the install stamp — so runtime code and the packaged
// artifact can never disagree about who they are.
//
// Dev bundles and test runs define nothing and fall back to importing
// the .cjs itself (esbuild bundles it; vitest's node environment loads
// it natively), so the derivation exists in exactly one place and dev
// `electron .` behaves like whichever variant HERMES_DESKTOP_VARIANT
// says at launch.

import devIdentity from '../product-identity.cjs'

/** Mirrors product-identity.cjs (see product-identity.d.cts). */
export type ProductIdentity = typeof devIdentity

declare const __HERMES_PRODUCT_IDENTITY__: ProductIdentity

/** The baked identity of this artifact (dev bundles derive it live). */
export const PRODUCT_IDENTITY: Readonly<ProductIdentity> =
  typeof __HERMES_PRODUCT_IDENTITY__ === 'undefined'
    ? Object.freeze(devIdentity)
    : Object.freeze(__HERMES_PRODUCT_IDENTITY__)
