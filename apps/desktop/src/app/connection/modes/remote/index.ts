// app/connection/modes/remote/index.ts — a user-owned remote gateway.
//
// The mode's private draft is a URL plus a session token as typed. The token
// is deliberately NOT rehydrated from a saved config: the bridge only ever
// returns a masked preview, so treating the field as write-only keeps a
// blank input meaning "keep the saved token" instead of "clear it".

import type { DesktopConnectionConfig, DesktopConnectionConfigInput } from '@/global'
import { Globe } from '@/lib/icons'
import { coerceRemoteUrlScheme } from '@/lib/remote-url'

import type { ConnectionCardContext, ConnectionModeCard, ConnectionModeModule } from '../../types'

import { RemotePanel } from './panel'

export interface RemoteDraft {
  url: string
  /** As typed. Blank means "keep whatever is already saved". */
  token: string
  /** Resolved by the panel's probe; seeded from the saved config. */
  authMode: 'oauth' | 'token'
}

export const remoteMode: ConnectionModeModule<'remote', RemoteDraft> = {
  mode: 'remote',

  emptyDraft: (): RemoteDraft => ({ url: '', token: '', authMode: 'token' }),

  fromSaved: (config: DesktopConnectionConfig | null): RemoteDraft => ({
    // Only adopt the saved URL when the saved connection IS a plain remote:
    // a cloud connection stores its agent's dashboardUrl in the same field,
    // and prefilling that here would offer to re-save a cloud instance as a
    // hand-typed remote.
    url: config && config.mode === 'remote' ? config.remoteUrl : '',
    token: '',
    authMode: config && config.mode === 'remote' ? config.remoteAuthMode : 'token'
  }),

  toPayload: (draft: RemoteDraft, scope: null | string): DesktopConnectionConfigInput => ({
    mode: 'remote',
    profile: scope ?? undefined,
    remoteAuthMode: draft.authMode,
    remoteToken: draft.authMode === 'token' ? draft.token.trim() || undefined : undefined,
    remoteUrl: coerceRemoteUrlScheme(draft.url)
  }),

  card: ({ copy }: ConnectionCardContext): ConnectionModeCard => ({
    icon: Globe,
    title: copy.remoteTitle,
    description: copy.remoteDesc,
    hint: copy.remoteAuthHint
  }),

  ConfigPanel: RemotePanel
}
