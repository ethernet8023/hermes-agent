// app/connection/index.ts — the registry. The ONE ordered list of connection
// mode modules; every surface (Settings → Gateway, first-run setup) walks
// this instead of hardcoding its own cards and panels.
//
// Order here is the order cards render. A module owns its own draft type, so
// nothing in this file — or in types.ts — names a mode's fields.

import { cloudMode } from './modes/cloud'
import { localMode } from './modes/local'
import { remoteMode } from './modes/remote'
import { sshMode } from './modes/ssh'
import type { ConnectionMode, ErasedConnectionModule } from './types'
import { defineConnectionMode } from './types'

/**
 * Every module, in card order. Drafts differ per mode by design, so entries
 * are erased through defineConnectionMode: the registry stays one uniform
 * list while each module keeps its concrete draft type at its definition.
 */
export const CONNECTION_MODE_MODULES: readonly ErasedConnectionModule[] = [
  defineConnectionMode(localMode),
  defineConnectionMode(cloudMode),
  defineConnectionMode(remoteMode),
  defineConnectionMode(sshMode)
]

export function moduleFor(mode: ConnectionMode): ErasedConnectionModule {
  const found = CONNECTION_MODE_MODULES.find(entry => entry.mode === mode)

  if (!found) {
    throw new Error(`Unknown connection mode: ${mode}`)
  }

  return found
}

export { ConnectionActions } from './connection-actions'
export { ConnectionModeCards } from './mode-card'
export { savedCloudConnectionUrl } from './modes/cloud'
export type {
  ConnectionCardContext,
  ConnectionCommit,
  ConnectionCopy,
  ConnectionDraft,
  ConnectionMode,
  ConnectionModeCard,
  ConnectionModeModule,
  ConnectionConfigPanelProps as ConnectionPanelProps,
  ConnectionSurface,
  ConnectionSurfaceKind,
  ErasedConnectionModule
} from './types'

export { CONNECTION_MODES, connectionCopy, defineConnectionMode } from './types'
export { useConnectionDrafts } from './use-connection-drafts'
