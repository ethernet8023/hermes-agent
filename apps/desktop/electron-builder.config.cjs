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
  ...(process.platform === 'darwin' ? { afterSign: 'scripts/notarize.mjs' } : {}),
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
  asar: {
    unpack: ['**/*.node', '**/prebuilds/**', 'dist/**']
  },
  mac: {
    category: 'public.app-category.developer-tools',
    extendInfo: {
      CFBundleDisplayName: displayName,
      CFBundleExecutable: displayName,
      CFBundleName: displayName,
      LSRequiresNativeExecution: true,
      NSAudioCaptureUsageDescription: `${displayName} uses audio capture for voice conversations.`,
      NSCameraUsageDescription: `${displayName} uses the camera when a plugin or feature you enable requests it.`,
      NSMicrophoneUsageDescription: `${displayName} uses the microphone for voice input and voice conversations.`,
      NSCalendarsUsageDescription: `${displayName} needs access to Calendar to provide requested meeting and scheduling support.`,
      NSCalendarsFullAccessUsageDescription: `${displayName} needs full access to Calendar to read and manage events when explicitly requested.`,
      NSRemindersUsageDescription: `${displayName} needs access to Reminders to provide requested personal-assistant and scheduling support.`,
      NSRemindersFullAccessUsageDescription: `${displayName} needs full access to Reminders to read and manage reminders when explicitly requested.`,
      NSScreenCaptureUsageDescription: `${displayName} captures the screen when you ask the agent to screenshot or record it.`,
      NSLocalNetworkUsageDescription: `${displayName} connects to devices on your local network when a plugin or feature you enable requests it.`,
      NSAppleMusicUsageDescription: `${displayName} accesses your music library when a plugin or feature you enable requests it.`
    },
    target: ['dmg', 'zip'],
    sign: {
      entitlements: 'electron/entitlements.mac.plist',
      entitlementsInherit: 'electron/entitlements.mac.inherit.plist',
      hardenedRuntime: true,
      ignore: (/** @type {string} */ file) => {
        try {
          if (fs.lstatSync(file).isDirectory()) {
            return false
          }
          return !isMachO(file)
        } catch {
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
    target: ['msix'],
    ...windowsSigning()
  },
  msix: {
    identityName: msixAppIdWithOrg,
    applicationId: appNamePascal,
    displayName,
    publisher: 'CN=Nous Research Inc., O=Nous Research Inc., L=Austin, S=Texas, C=US',
    publisherDisplayName: 'Nous Research',
    // Floor Windows 11 22H2. Below build 18307 the manifest schema caps
    // AppExtension Name at 39 chars and Microsoft's own
    // "com.microsoft.windows.copilotkeyprovider" is 40 (makeappx
    // 0x80080204 — A/B-verified against the 26100 kit; 18307 exactly
    // still failed on it, 22621 passes), and 22621 is the documented
    // Copilot hardware key floor anyway.
    minVersion: '10.0.22621.0',
    maxVersionTested: '10.0.26100.0',
    customExtensionsPath: writeMsixExtensions(),
    customManifestPath: 'assets/msix-manifest.xml',
    showNameOnTiles: true
  },
  linux: {
    category: 'Development',
    maintainer: 'Nous Research <support@nousresearch.com>',
    synopsis: light
      ? 'Remote-only desktop client for Hermes Agent.'
      : 'Native desktop shell for Hermes Agent.',
    target: ['AppImage']
  }
}

function stageMsixAssets() {
  const sourceDir = path.join(__dirname, 'assets', 'appx')
  const stageDir = path.join(__dirname, 'build', 'appx')
  const names = [
    'Square44x44Logo.png',
    'Square150x150Logo.png',
    'StoreLogo.png',
    'Wide310x150Logo.png'
  ]

  fs.mkdirSync(stageDir, { recursive: true })
  for (const name of names) {
    const source = path.join(sourceDir, name)
    if (!fs.existsSync(source)) {
      throw new Error(`missing MSIX asset ${source}`)
    }
    fs.copyFileSync(source, path.join(stageDir, name))
  }
}

function writeMsixExtensions() {
  const output = path.join('build', 'msix-extensions.xml')
  const file = path.join(__dirname, output)
  const aliases = light
    ? ''
    : `<uap5:Extension
    xmlns:uap5="http://schemas.microsoft.com/appx/manifest/uap/windows10/5"
    Category="windows.appExecutionAlias"
    Executable="${['app', 'resources', 'agent-payload', 'venv', 'Scripts', 'hermes.exe'].join(String.fromCharCode(92))}"
    EntryPoint="Windows.FullTrustApplication">
  <uap5:AppExecutionAlias>
    <uap5:ExecutionAlias Alias="hermes.exe" />
    <uap5:ExecutionAlias Alias="hermes-agent.exe" />
    <uap5:ExecutionAlias Alias="hermes-acp.exe" />
  </uap5:AppExecutionAlias>
</uap5:Extension>`
  // The uap3:AppExtension fragment that registers the app as a Windows
  // Copilot hardware key provider. The press activates hermes://copilot-key/start.
  //
  // Content rules (violations are an opaque makeappx 0x80080204):
  //   * xmlns:uap3 rides on the fragment root — the stock manifest template
  //     declares no uap3 prefix. A/B-verified fine.
  //   * children of uap3:Properties are UNPREFIXED (xs:any content, per
  //     Microsoft's copilot-key-state sample).
  const copilot = light
    ? ''
    : `<uap3:Extension
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
${aliases}`

  fs.mkdirSync(path.dirname(file), { recursive: true })
  fs.writeFileSync(file, copilot)
  return output
}

stageMsixAssets()

// Azure Trusted Signing. The custom hook keeps signed bytes by their unsigned
// hash. Rebuilds only send changed binaries to the remote signing service.
const MACHO_MAGICS = new Set([
  0xfeedface,
  0xcefaedfe,
  0xfeedfacf,
  0xcffaedfe,
  0xcafebabe,
  0xbebafeca
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

function windowsSigning() {
  if (!process.env.AZURE_SIGN_ENDPOINT || !process.env.AZURE_CLIENT_ID) {
    return {}
  }
  return {
    sign: {
      type: 'signtool',
      sign: './scripts/sign-cached.mjs',
      signingHashAlgorithms: ['sha256'],
      publisherName: process.env.AZURE_SIGN_PUBLISHER
    }
  }
}
