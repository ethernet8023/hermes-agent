import { atom } from 'nanostores'

/**
 * Feature-flag gate for every local-models surface in the GUI.
 *
 * Local models are on when the app was launched with `--local` (either
 * `hermes desktop --local` or the flag on Hermes.exe itself), OR when this
 * is a preview build (canary, commit, channel, or dev), which get gated
 * features by default. Only a tagged stable release without the flag shows
 * no local-models surface at all, even on a machine where local models are
 * configured and running.
 */
export const $localModelsEnabled = atom<boolean>(
  typeof window !== 'undefined' && window.hermesDesktop?.localModelsEnabled === true
)
