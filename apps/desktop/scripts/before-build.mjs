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
 *
 * Also stages the MSIX build-time assets (the build/appx icon set and the
 * build/msix-extensions.xml fragment the config points customExtensionsPath
 * at). These MUST happen here, in the hook electron-builder calls at build
 * time — not in electron-builder.config.cjs at require time, or every
 * typecheck/test import of the config would write files.
 */
import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)
const {
  light,
  displayName,
  appNamePascal
} = require('../product-identity.cjs')

export default async function beforeBuild() {
  stageMsixAssets()
  writeMsixExtensions()

  const payloadDir = path.join(import.meta.dirname, '..', 'build', 'agent-payload')
  const manifest = path.join(payloadDir, 'manifest.json')

  if (!fs.existsSync(manifest)) {
    fs.mkdirSync(payloadDir, { recursive: true })
    fs.writeFileSync(manifest, JSON.stringify({ schema: 1, external: true }, null, 2) + '\n')
  }

  return false
}

function stageMsixAssets() {
  const desktop = path.join(import.meta.dirname, '..')
  const sourceDir = path.join(desktop, 'assets', 'appx')
  const stageDir = path.join(desktop, 'build', 'appx')
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
  const desktop = path.join(import.meta.dirname, '..')
  const output = path.join('build', 'msix-extensions.xml')
  const file = path.join(desktop, output)
  const aliases = light
    ? ''
    : `<uap5:Extension
    xmlns:uap5="http://schemas.microsoft.com/appx/manifest/uap/windows10/5"
    Category="windows.appExecutionAlias"
    Executable="${['app', 'resources', 'agent-payload', 'bin', 'hermes.exe'].join(String.fromCharCode(92))}"
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
}
