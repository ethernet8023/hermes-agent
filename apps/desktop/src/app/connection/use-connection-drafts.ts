// app/connection/use-connection-drafts.ts — one in-progress draft PER mode,
// keyed by mode.
//
// Switching cards must not discard a half-typed URL, which is the whole
// reason drafts outlive the mounted panel. Keying by mode (rather than
// merging every mode's fields into one bag) buys that without letting one
// mode's panel reach another's state: a panel only ever receives the draft
// its own module produced.
//
// Drafts are seeded per mode from the saved config through each module's
// fromSaved, so re-opening Settings shows the saved URL/host immediately.
// Re-seeding is keyed on the config identity: a scope change (which loads a
// different config) reseeds every mode, while ordinary typing does not.

import { useCallback, useRef, useState } from 'react'

import type { DesktopConnectionConfig } from '@/global'

import type { ConnectionDraft, ConnectionMode } from './types'

import { CONNECTION_MODE_MODULES } from './index'

type DraftsByMode = Record<ConnectionMode, ConnectionDraft>

function seedDrafts(config: DesktopConnectionConfig | null): DraftsByMode {
  const drafts = {} as DraftsByMode

  for (const module of CONNECTION_MODE_MODULES) {
    drafts[module.mode] = module.fromSaved(config)
  }

  return drafts
}

export interface ConnectionDrafts {
  draftFor: (mode: ConnectionMode) => ConnectionDraft
  updateDraft: (mode: ConnectionMode, patch: Partial<ConnectionDraft>) => void
  /** Drop every typed value and reseed from the current saved config. */
  resetDrafts: () => void
}

export function useConnectionDrafts(config: DesktopConnectionConfig | null): ConnectionDrafts {
  const [drafts, setDrafts] = useState<DraftsByMode>(() => seedDrafts(config))
  // Reseed when the loaded config is REPLACED (scope switch, first load),
  // not on every render that happens to carry an equal object.
  const seededFrom = useRef<DesktopConnectionConfig | null>(config)

  if (seededFrom.current !== config) {
    seededFrom.current = config
    setDrafts(seedDrafts(config))
  }

  const updateDraft = useCallback((mode: ConnectionMode, patch: Partial<ConnectionDraft>): void => {
    setDrafts(current => ({ ...current, [mode]: { ...current[mode], ...patch } }))
  }, [])

  const resetDrafts = useCallback((): void => {
    setDrafts(seedDrafts(seededFrom.current))
  }, [])

  const draftFor = useCallback((mode: ConnectionMode): ConnectionDraft => drafts[mode], [drafts])

  return { draftFor, updateDraft, resetDrafts }
}
