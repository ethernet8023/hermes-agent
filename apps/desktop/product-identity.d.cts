interface ProductIdentity {
  /** True when this artifact is Hermes Light (remote-only client). */
  light: boolean
  /** Display name. e.g. "Hermes Light" */
  displayName: string
  /** OS-level app identity. e.g. "com.nousresearch.hermes-light" */
  appId: string
  /** app name in pascal case. e.g. "HermesLight" */
  appNamePascal: string
  /** OS-level app identity w/ org prefix. e.g. "NousResearch.HermesLight" */
  msixAppIdWithOrg: string
  /** electron-updater feed channel this build publishes to. Stable tags:
   *  "latest" | "light"; nightly tags: "nightly" | "light-nightly". */
  channel: string
}

declare const identity: ProductIdentity
export = identity
