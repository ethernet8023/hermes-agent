/**
 * MSIX asset staging contract.
 *
 * MsixTarget discovers per-app tile/store logos via
 * getResource(undefined, "appx") — i.e. ONLY from
 * directories.buildResources/appx (build/appx, gitignored scratch). When a
 * default asset name is absent there, computeUserAssets silently maps
 * app-builder-lib's SampleAppx.*.png placeholder into the package and the
 * installed app shows a generic gray icon.
 *
 * The committed source of truth is assets/appx/; requiring the builder
 * config stages it into build/appx/. These tests exercise that real path:
 * require the actual config, then assert every default asset slot
 * app-builder-lib knows about is filled — keyed off the library's own
 * vendorAssetsForDefaultAssets/isDefaultAssetIncluded so a lib bump that
 * adds a new slot fails here instead of shipping a placeholder.
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { createRequire } from 'node:module'
import path from 'node:path'
import { test } from 'vitest'

import {
  isDefaultAssetIncluded,
  vendorAssetsForDefaultAssets
} from '../../../node_modules/app-builder-lib/dist/targets/win/winAppUtil.js'

const desktop = path.resolve(__dirname, '..')
const stageDir = path.join(desktop, 'build', 'appx')

// The same filter computeUserAssets applies to the directory listing.
function listUserAssets(dir) {
  return fs.readdirSync(dir).filter(it => !it.startsWith('.') && !it.endsWith('.db') && it.includes('.'))
}

test('requiring the builder config stages MSIX assets into build/appx', () => {
  fs.rmSync(stageDir, { recursive: true, force: true })
  const require = createRequire(__filename)
  delete require.cache[require.resolve(path.join(desktop, 'electron-builder.config.cjs'))]
  require(path.join(desktop, 'electron-builder.config.cjs'))

  assert.ok(fs.existsSync(stageDir), 'build/appx was not staged by config require')
  assert.ok(listUserAssets(stageDir).length > 0, 'build/appx staged empty')
})

test('every default asset slot is filled — no SampleAppx placeholder can leak in', () => {
  const userAssets = listUserAssets(stageDir)
  for (const defaultAsset of Object.keys(vendorAssetsForDefaultAssets)) {
    assert.ok(
      isDefaultAssetIncluded(userAssets, defaultAsset),
      `${defaultAsset} missing from build/appx — computeUserAssets would fall back to ` +
        `the ${vendorAssetsForDefaultAssets[defaultAsset]} placeholder for this slot`
    )
  }
})

test('staged PNGs have the dimensions their filenames promise', () => {
  // IHDR width/height live at fixed offsets 16/20 in any valid PNG.
  const pngSize = file => {
    const buf = fs.readFileSync(file)
    return { width: buf.readUInt32BE(16), height: buf.readUInt32BE(20) }
  }
  for (const name of listUserAssets(stageDir)) {
    const m = name.match(/(\d+)x(\d+)Logo\.png$/)
    if (!m) {
      continue
    } // StoreLogo.png carries no size in its name (50x50 by convention)
    const { width, height } = pngSize(path.join(stageDir, name))
    assert.equal(width, Number(m[1]), `${name} width`)
    assert.equal(height, Number(m[2]), `${name} height`)
  }
})
