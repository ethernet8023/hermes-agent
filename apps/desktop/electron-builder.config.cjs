// THE electron-builder configuration — the whole thing, one file. There is
// no "build" field in package.json: run-electron-builder.mjs always passes
// --config for this file, so a stray package.json field would be silently
// ignored anyway, and splitting the config across JSON + this overlay is
// how the two halves drift.
//
// A .cjs module (not JSON) for two reasons:
//   * mac.sign.ignore must be a FUNCTION. osx-sign's walk selects files to
//     sign with a generic binary-content probe, which flags plain binary
//     resources (the payload CPython's idlelib GIFs, wheels, .zip) as
//     signable. Signing those is wrong (non-Mach-O resources are covered
//     by the bundle's CodeResources seal) and each bogus signing hits
//     Apple's timestamp service — thousands of payload files flooded it
//     until it refused ("The timestamp service is not available"). The
//     function scopes signing to real Mach-O files.
//   * the variant is decided at require time: HERMES_DESKTOP_VARIANT=light
//     builds "Hermes Light", the remote-only client with no agent payload
//     and no local backend. The whole config derives from the one `light`
//     flag below — a separate app to the OS and to the updater, so both
//     variants install and update side by side.
// @ts-check — typed via JSDoc against app-builder-lib's own declarations;
// enforced by the checkJs pass in npm run typecheck.
'use strict'

const fs = require('node:fs')
const path = require('node:path')

const {
  light,
  displayName,
  appId,
  appNamePascal,
  channel,
  msixAppIdWithOrg
} = require('./product-identity.cjs')

/** @typedef {import("app-builder-lib").Configuration} Configuration */

const [owner, repo] = (process.env.GITHUB_REPOSITORY || 'NousResearch/hermes-agent').split('/')
if (!owner || !repo) {
  throw new Error(`invalid GITHUB_REPOSITORY ${process.env.GITHUB_REPOSITORY}`)
}
const electronVersion = require('./package.json').devDependencies.electron
if (!/^\d+\.\d+\.\d+$/.test(electronVersion)) {
  throw new Error(`invalid electron version ${electronVersion} in package.json`)
}
/** @type {Configuration} */
module.exports = {
  electronVersion,
  appId,
  productName: displayName,
  executableName: displayName,
  protocols: [
    {
      name: `${displayName} Protocol`,
      schemes: ["hermes"]
    }
  ],
  // separate variants for release filenames
  artifactName: `${appNamePascal}-\${version}-\${os}-\${arch}.\${ext}`,
  icon: 'assets/icon',
  publish: [
    {
      provider: 'github',
      owner,
      repo,
      channel
    }
  ],
  // overrides package.json
  extraMetadata: {
    // separate variants for electron-updater download cache dirs
    name: appNamePascal,
    // for .desktop file on linux
    desktopName: appId
  },
  directories: {
    output: 'release'
  },
  files: ['dist/**', 'assets/**', 'public/**', 'package.json'],
  beforeBuild: 'scripts/before-build.mjs',
  beforePack: 'scripts/before-pack.mjs',
  afterPack: 'scripts/after-pack.mjs',
  extraResources: [
    {
      from: 'build/agent-payload',
      to: 'agent-payload'
    },
    {
      from: 'assets/icon.ico',
      to: 'icon.ico'
    }
  ],
  asar: {
    unpack: ['**/*.node', '**/prebuilds/**', 'dist/**']
  },
  mac: {
    category: 'public.app-category.developer-tools',
    extendInfo: {
      CFBundleDisplayName: displayName,
      CFBundleExecutable: displayName,
      CFBundleName: displayName,
      // Refuse Rosetta translation: without this, the x64 build launches
      // silently emulated on Apple silicon and stays emulated through every
      // update (electron-updater keys the feed on process.arch). With it,
      // LaunchServices blocks the launch and tells the user to get the
      // arm64 build. The arm64 build never runs on Intel, so the key is
      // safe to set unconditionally.
      LSRequiresNativeExecution: true,
      NSAudioCaptureUsageDescription: `${displayName} uses audio capture for voice conversations.`,
      NSCameraUsageDescription: `${displayName} uses the camera when a plugin or feature you enable requests it.`,
      NSMicrophoneUsageDescription: `${displayName} uses the microphone for voice input and voice conversations.`
    },
    target: ['dmg', 'zip'],
    sign: {
      entitlements: 'electron/entitlements.mac.plist',
      entitlementsInherit: 'electron/entitlements.mac.inherit.plist',
      hardenedRuntime: true,
      // (gatekeeperAssess is gone: osx-sign v3 dropped the --gatekeeper-assess
      // pass entirely, and the v27 ElectronSignOptions type rejects the key.)
      // true → skip. Directories pass through (the walk hands over .app and
      // .framework bundles, which codesign must see whole); every regular
      // file must prove it is Mach-O to be signed individually.
      ignore: (/** @type {string} */ file) => {
        try {
          if (fs.lstatSync(file).isDirectory()) {
            return false
          }
          return !isMachO(file)
        } catch {
          // Unreadable/vanished: nothing to sign either way.
          return true
        }
      }
    }
  },
  dmg: {
    title: `Install ${displayName}`,
    backgroundColor: '#f5f5f7',
    iconSize: 96,
    window: {
      width: 560,
      height: 360
    },
    contents: [
      {
        x: 160,
        y: 170,
        type: 'file'
      },
      {
        x: 400,
        y: 170,
        type: 'link',
        path: '/Applications'
      }
    ]
  },
  win: {
    legalTrademarks: displayName,
    target: ['nsis', 'msix'],
    ...windowsSigning()
  },
  // MSIX ships beside NSIS: the exe keeps electron-updater and normal
  // distribution; the MSIX exists for Store/sideload installs and for the
  // Windows Copilot hardware key.
  // electron-updater does not update MSIX installs.
  msix: {
    identityName: msixAppIdWithOrg,
    applicationId: appNamePascal,
    displayName: displayName,
    publisher: 'CN=Nous Research Inc., O=Nous Research Inc., L=Austin, S=Texas, C=US',
    publisherDisplayName: 'Nous Research',
    // Floor Windows 11 22H2. Two reasons: below build 18307 the manifest
    // schema caps AppExtension Name at 39 chars and Microsoft's own
    // "com.microsoft.windows.copilotkeyprovider" is 40 (makeappx
    // 0x80080204 — A/B-verified against the 26100 kit; 18307 exactly
    // still failed on it, 22621 passes), and 22621 is the documented
    // Copilot hardware key floor anyway.
    minVersion: '10.0.22621.0',
    maxVersionTested: '10.0.26100.0',
    customExtensionsPath: msixExtensionsPath(),
    // The stock app-builder-lib manifest template's IgnorableNamespaces
    // covers only "uap10 desktop6". Our execution-alias extension is a
    // uap5:Extension carrying desktop4:Subsystem, and makeappx refuses any
    // namespace it finds that the Package root neither declares nor lists
    // as ignorable (0x80080204, reported with no detail). This manifest is
    // the stock template plus uap5 + desktop4 in both places; keep in sync
    // with templates/msix/appxmanifest.xml when electron-builder bumps.
    customManifestPath: 'assets/msix-manifest.xml',
    // Without this the Start tile renders logo-only, no app name.
    showNameOnTiles: true
  },
  linux: {
    category: 'Development',
    maintainer: 'Nous Research <support@nousresearch.com>',
    synopsis: light ? 'Remote-only desktop client for Hermes Agent.' : 'Native desktop shell for Hermes Agent.',
    target: ['AppImage']
  },
  nsis: {
    oneClick: true,
    perMachine: false,
    installerIcon: 'assets/icon.ico',
    uninstallerIcon: 'assets/icon.ico',
    installerHeaderIcon: 'assets/icon.ico',
    shortcutName: displayName,
    uninstallDisplayName: displayName,
    warningsAsErrors: false,
    // Combined NSIS include (electron-builder's nsis.include takes ONE
    // file): customInit arch guard (refuse wrong-arch installs) +
    // customInstall/customUnInstall PATH exposure of the payload's
    // prebuilt CLI shims (resources\agent-payload\bin). See the .nsh for
    // the full rationale; the path resolves relative to the project dir.
    include: 'electron/nsis-include.nsh'
  }
}

// ── msix assets ─────────────────────────────────────────────────────────────

// MSIX tile/store logos. Without these, makeappx falls back to
// app-builder-lib's bundled SampleAppx.*.png placeholders — the packaged app
// installs with a generic gray sample icon in Start/taskbar/Settings.
//
// MsixTarget discovers user assets via getResource(undefined, "appx"), which
// only looks inside directories.buildResources (default: build/ — gitignored
// scratch). The committed source of truth lives in assets/appx/; this stages
// it into build/appx/ at config-require time, same pattern as the copilot
// key fragment above. Regenerate from assets/icon.png with ImageMagick:
//   magick assets/icon.png -resize 44x44   assets/appx/Square44x44Logo.png
//   magick assets/icon.png -resize 50x50   assets/appx/StoreLogo.png
//   magick assets/icon.png -resize 150x150 assets/appx/Square150x150Logo.png
//   magick assets/icon.png -resize 128x128 -background none -gravity center \
//     -extent 310x150 assets/appx/Wide310x150Logo.png
function stageMsixAssets() {
  const sourceDir = path.join(__dirname, 'assets', 'appx')
  const stageDir = path.join(__dirname, 'build', 'appx')
  fs.mkdirSync(stageDir, { recursive: true })
  const staged = []
  for (const name of fs.readdirSync(sourceDir)) {
    if (!name.endsWith('.png')) {
      continue
    }
    fs.copyFileSync(path.join(sourceDir, name), path.join(stageDir, name))
    staged.push(name)
  }
  if (staged.length === 0) {
    throw new Error(`no MSIX assets found in ${sourceDir}`)
  }
  return staged
}
stageMsixAssets()

// ── copilot key provider fragment + CLI execution aliases ───────────────────

// The uap3:AppExtension fragment that registers the app as a Windows
// Copilot hardware key provider.
// The press activates <scheme>://copilot-key/start.
//
// Content rules (violations are an opaque makeappx 0x80080204; the full
// reasons only surface when makeappx runs against a plain directory):
//   * xmlns:uap3 rides on the fragment root — the stock manifest template
//     declares no uap3 prefix. A/B-verified fine (namespace placement is
//     NOT what 0x80080204 was about; msix.minVersion and the alias
//     element family were).
//   * children of uap3:Properties are UNPREFIXED (xs:any content, per
//     Microsoft's copilot-key-state sample).
//
// The same customExtensionsPath file also carries the CLI execution
// aliases for the bundled variant: MSIX payloads live under WindowsApps
// where registry PATH edits do not reach, and AppExecutionAlias is the
// platform's own answer — the alias is part of the sealed package
// manifest, covered by the package signature by construction. All three
// aliases point at the ONE packaged shim (hermes.exe in the payload bin);
// the shim dispatches on argv[0], which carries the alias name. The light
// variant ships no payload, so it gets no aliases.
//
// Alias content rules, verified against the real makeappx (26100 kit) by
// packing a staged directory and reading the C00CE015 detail text that the
// bare 0x80080204 hides:
//   * The alias rides on uap5:Extension, NOT uap3:Extension. uap3's
//     AppExecutionAlias takes no attributes and its children are
//     uap3:ExecutionAliasChoice, so a desktop4:Subsystem attribute on a
//     uap3:Extension is rejected outright: "The attribute ...desktop/
//     windows10/4}Subsystem on the element ...uap/windows10/3}Extension is
//     not defined in the DTD/Schema."
//   * Subsystem is NOT declared here. The validator refuses
//     Subsystem="console" unless SupportsMultipleInstances="true" appears
//     "in element Application" — and the fragment cannot supply it:
//     uap11:SupportsMultipleInstances on the uap5:Extension is rejected
//     with the same message. Putting it on Application would mean forking
//     app-builder-lib's manifest template AND multi-instancing the whole
//     app, which fights the requestSingleInstanceLock() the deep-link
//     routing depends on (electron/main.ts).
//     Dropping the attribute costs nothing measurable: the shim's own PE
//     is console-subsystem, so an installed package with no Subsystem
//     declaration still blocks its caller for the child's full runtime and
//     still hands back stdout — A/B-measured on Windows 11 against an
//     installed, signed package.
function msixExtensionsPath() {
  const shimExecutable = 'app\\resources\\agent-payload\\bin\\hermes.exe'
  const aliasNames = ['hermes.exe', 'hermes-agent.exe', 'hermes-acp.exe']
  const aliasFragment = light
    ? ''
    : `<uap5:Extension
    xmlns:uap5="http://schemas.microsoft.com/appx/manifest/uap/windows10/5"
    Category="windows.appExecutionAlias"
    Executable="${shimExecutable}"
    EntryPoint="Windows.FullTrustApplication">
  <uap5:AppExecutionAlias>
${aliasNames.map((alias) => `    <uap5:ExecutionAlias Alias="${alias}" />`).join('\n')}
  </uap5:AppExecutionAlias>
</uap5:Extension>
`
  const fragment = `<uap3:Extension
    xmlns:uap3="http://schemas.microsoft.com/appx/manifest/uap/windows10/3"
    Category="windows.appExtension">
  <uap3:AppExtension
      Name="com.microsoft.windows.copilotkeyprovider"
      Id="${appNamePascal}CopilotKeyProvider"
      DisplayName="${displayName}"
      Description="Launch ${displayName} with the Copilot key"
      PublicFolder="Public">
    <uap3:Properties>
      <SingleTap>hermes://copilot-key/start?state=Tap</SingleTap>
      <PressAndHoldStart>hermes://copilot-key/start?state=Down</PressAndHoldStart>
      <PressAndHoldStop>hermes://copilot-key/stop?state=Up</PressAndHoldStop>
    </uap3:Properties>
  </uap3:AppExtension>
</uap3:Extension>
${aliasFragment}`
  const rel = path.join('build', 'msix-copilot-key-extensions.xml')
  const abs = path.join(__dirname, rel)
  fs.mkdirSync(path.dirname(abs), { recursive: true })
  fs.writeFileSync(abs, fragment)
  return rel
}

// ── windows signing ─────────────────────────────────────────────────────────

// Azure Trusted Signing. Composed here, not as -c.win.sign.* CLI arguments:
// the publisherName holds spaces and commas that do not survive cmd.exe
// argument hops. This file loads inside the electron-builder process, so
// the values pass from the environment verbatim.
//
// Do NOT put ExcludeCredentials in additionalMetadata: the v27 schema
// types it Record<string,string> while the dlib deserializes it as
// List<string> — no value satisfies both. The credential chain is
// narrowed with the AZURE_TOKEN_CREDENTIALS env var instead (set in the
// release workflow), which Azure.Identity reads directly.
function windowsSigning() {
  if (!process.env.AZURE_SIGN_ENDPOINT || !process.env.AZURE_CLIENT_ID) {
    return {}
  }
  // type 'signtool' + a custom `sign` hook, not type 'azure': the hook
  // (scripts/sign-cached.mjs) wraps the exact same WindowsSignAzureManager
  // behind a content-addressed cache, so rebuilds of byte-identical inputs
  // reuse yesterday's signature instead of a remote signing round-trip.
  // electron-builder invokes the hook once per file per hash algorithm —
  // the default algorithms are ['sha1', 'sha256'], which would mean TWO
  // passes per file, and the second (nested) signature would rewrite the
  // binary and miss the cache forever. Azure Trusted Signing only ever
  // produced sha256, so pin signingHashAlgorithms to that single pass.
  // publisherName stays here because electron-updater's
  // verifyUpdateCodeSignature reads it from app-update.yml; the Azure
  // endpoint/account/profile no longer ride through this config at all —
  // they travel via the AZURE_SIGN_* environment variables straight into
  // the hook (azureConfigFromEnv in sign-cached.mjs). With no certificate
  // configured, signtoolBaseSignManager.handleNullCscInfo(customSign)
  // returns !customSign, so the custom hook proceeds unhindered.
  return {
    sign: {
      type: 'signtool',
      sign: './scripts/sign-cached.mjs',
      signingHashAlgorithms: ['sha256'],
      publisherName: process.env.AZURE_SIGN_PUBLISHER
    }
  }
}

// ── mac signing scope ───────────────────────────────────────────────────────

// The four magics that open a Mach-O or universal (fat) binary, in both
// byte orders: MH_MAGIC(_64) and FAT_MAGIC read big-endian at offset 0.
const MACHO_MAGICS = new Set([
  0xfeedface, // MH_MAGIC (32-bit)
  0xcefaedfe, // MH_CIGAM
  0xfeedfacf, // MH_MAGIC_64
  0xcffaedfe, // MH_CIGAM_64
  0xcafebabe, // FAT_MAGIC (universal)
  0xbebafeca // FAT_CIGAM
])

/** @param {string} file */
function isMachO(file) {
  const buf = Buffer.alloc(4)
  const fd = fs.openSync(file, 'r')
  try {
    if (fs.readSync(fd, buf, 0, 4, 0) !== 4) {
      return false
    }
  } finally {
    fs.closeSync(fd)
  }
  return MACHO_MAGICS.has(buf.readUInt32BE(0))
}
