// THE electron-builder configuration — the whole thing, one file. There is
// no "build" field in package.json: run-electron-builder.mjs always passes
// --config for this file, so a stray package.json field would be silently
// ignored anyway, and splitting the config across JSON + this overlay is
// how the two halves drift.
//
// A .cjs module (not JSON) so the variant is decided at require time:
// HERMES_DESKTOP_VARIANT=light builds "Hermes Light". The whole config
// derives from that one flag.
// @ts-check
'use strict'

const {
  light,
  displayName,
  appId,
  appNamePascal,
  channel
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
      schemes: ['hermes']
    }
  ],
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
  extraMetadata: {
    name: appNamePascal,
    desktopName: appId
  },
  directories: {
    output: 'release'
  },
  files: ['dist/**', 'assets/**', 'public/**', 'package.json'],
  beforeBuild: 'scripts/before-build.mjs',
  beforePack: 'scripts/before-pack.mjs',
  afterPack: 'scripts/after-pack.mjs',
  afterSign: 'scripts/notarize.mjs',
  extraResources: [
    {
      from: 'build/install-stamp.json',
      to: 'install-stamp.json'
    },
    {
      from: 'build/agent-payload',
      to: 'agent-payload'
    },
    {
      from: 'assets/icon.ico',
      to: 'icon.ico'
    }
  ],
  asar: true,
  asarUnpack: ['**/*.node', '**/prebuilds/**', 'dist/**'],
  mac: {
    category: 'public.app-category.developer-tools',
    entitlements: 'electron/entitlements.mac.plist',
    entitlementsInherit: 'electron/entitlements.mac.inherit.plist',
    hardenedRuntime: true,
    extendInfo: {
      CFBundleDisplayName: displayName,
      CFBundleExecutable: displayName,
      CFBundleName: displayName,
      // Refuse Rosetta translation: without this, the x64 build launches
      // silently emulated on Apple silicon and stays emulated through every
      // update (electron-updater keys the feed on process.arch).
      LSRequiresNativeExecution: true,
      NSAudioCaptureUsageDescription: `${displayName} uses audio capture for voice conversations.`,
      NSCameraUsageDescription: `${displayName} uses the camera when a plugin or feature you enable requests it.`,
      NSMicrophoneUsageDescription: `${displayName} uses the microphone for voice input and voice conversations.`
    },
    target: ['dmg', 'zip'],
    // electron-builder 26 treats mac.sign as a hook function. A nested
    // options object is resolved as customSign and then called.
    // Entitlements and hardenedRuntime live on mac.* (schema keys).
    // signIgnore is tested against the full path. Skip data files so
    // osx-sign does not codesign every botocore .json.gz and chromium
    // .pak (measured: one codesign per file, Apple timestamp each).
    signIgnore: [
      '\.(gif|png|jpe?g|webp|svg|ico|icns|woff2?|ttf|otf)$',
      '\.(whl|zip|gz|bz2|xz|pak|dat|bin|wasm|wav|onnx|tflite)$',
      '\.(pyc|py|txt|md|rst|json|ya?ml|toml|ini|cfg|xml|html|css|js|map|csv)$',
      '\.(plist|strings|nib|car)$',
      '\.dist-info/'
    ]
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
    target: ['nsis'],
    ...windowsSigning()
  },
  linux: {
    category: 'Development',
    maintainer: 'Nous Research <support@nousresearch.com>',
    synopsis: light
      ? 'Remote-only desktop client for Hermes Agent.'
      : 'Native desktop shell for Hermes Agent.',
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
    warningsAsErrors: false
  }
}

// Azure Trusted Signing. Composed here, not as -c.win.* CLI arguments:
// the publisherName holds spaces and commas that do not survive cmd.exe
// argument hops. electron-builder 26 reads win.azureSignOptions. Absent
// env vars leave the build unsigned — the fork / local path.
function windowsSigning() {
  if (!process.env.AZURE_SIGN_ENDPOINT || !process.env.AZURE_CLIENT_ID) {
    return {}
  }
  return {
    azureSignOptions: {
      publisherName: process.env.AZURE_SIGN_PUBLISHER,
      endpoint: process.env.AZURE_SIGN_ENDPOINT,
      codeSigningAccountName: process.env.AZURE_SIGN_ACCOUNT,
      certificateProfileName: process.env.AZURE_SIGN_PROFILE
    },
    // extraResources payload PEs are not under app.asar.unpacked.
    // 26 only signs those if signExts matches during the extra-file copy.
    signExts: ['.exe', '.dll']
  }
}
