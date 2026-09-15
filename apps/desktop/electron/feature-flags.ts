// feature-flags.ts — the desktop's feature-flag resolver.
//
// One place that decides which gated surfaces are on for this artifact.
// Flags resolve from two inputs:
//   - launch argv (e.g. `--local` on Hermes.exe, or `hermes desktop --local`)
//   - the artifact's release identity (preview builds get gated features)
// The verdict is static for the process lifetime; the renderer reads it
// once via the preload bridge (`hermes:feature-flags`).

import type { InstallStamp } from './install-stamp'

export interface FeatureFlags {
  /** Local-models GUI surfaces (settings pane, pickers, statusbar, tips). */
  localModels: boolean
}

/** True when a baked install tag names a canary-channel build. */
export function isCanaryTag(tag: string | null | undefined): boolean {
  return /-canary\./.test(tag || '')
}

/**
 * True for every artifact that is not a tagged stable release: canary tags,
 * commit builds (`releases/commit/<sha>/`), channel builds (disposable and
 * named channels), and dev runs with no stamp. Stable is the only identity
 * that keeps gated surfaces off by default; every other build exists to
 * preview what is coming.
 */
export function isPreviewBuild(stamp: Readonly<InstallStamp> | null): boolean {
  if (!stamp) { return true }
  if (stamp.channelBuild || stamp.source === 'commit-build') { return true }
  if (!stamp.tag) { return true }

  return isCanaryTag(stamp.tag)
}

export interface FeatureFlagInput {
  /** The main process argv (launch flags like `--local`). */
  argv: readonly string[]
  /** Whether this artifact is a preview build (anything but a tagged stable release). */
  preview: boolean
}

/**
 * Resolve every feature flag for this process. A flag is on when the launch
 * argv opts in (`--local`) OR the artifact is a preview build. Only a tagged
 * stable release keeps gated surfaces off without the flag.
 */
export function resolveFeatureFlags({ argv, preview }: FeatureFlagInput): FeatureFlags {
  return {
    localModels: preview || argv.includes('--local')
  }
}
