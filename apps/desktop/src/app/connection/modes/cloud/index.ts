// app/connection/modes/cloud/index.ts — a discovered Hermes Cloud instance.
//
// Cloud persists a remote-shaped block (remoteUrl = the selected agent's
// dashboardUrl, oauth) but is remembered as its own mode so a surface can
// reopen into the agent picker instead of showing a URL the user never
// typed. Its draft is therefore the SELECTION (org + agent), not a URL: the
// URL is discovered, and the panel writes it into the draft when an agent is
// chosen.

import type { DesktopConnectionConfig, DesktopConnectionConfigInput } from '@/global'
import { Cloud } from '@/lib/icons'

import type { ConnectionCardContext, ConnectionModeCard, ConnectionModeModule } from '../../types'

import { CloudPanel } from './panel'

export interface CloudDraft {
  /** The chosen org (slug or id); empty until discovery resolves one. */
  org: string
  /** The chosen agent's dashboardUrl; empty until an agent is picked. */
  agentUrl: string
}

/**
 * The URL of the connected cloud instance, normalized for comparison against
 * a discovered agent's dashboardUrl. Empty unless the saved connection is a
 * cloud one: a stale URL left on a local or remote config must not read as a
 * connected agent.
 */
export function savedCloudConnectionUrl(config: Pick<DesktopConnectionConfig, 'mode' | 'remoteUrl'>): string {
  return config.mode === 'cloud' ? config.remoteUrl.trim().replace(/\/+$/, '').toLowerCase() : ''
}

export const cloudMode: ConnectionModeModule<'cloud', CloudDraft> = {
  mode: 'cloud',

  emptyDraft: (): CloudDraft => ({ org: '', agentUrl: '' }),

  fromSaved: (config: DesktopConnectionConfig | null): CloudDraft =>
    config && config.mode === 'cloud' ? { org: config.cloudOrg, agentUrl: config.remoteUrl } : { org: '', agentUrl: '' },

  toPayload: (draft: CloudDraft, scope: null | string): DesktopConnectionConfigInput => ({
    mode: 'cloud',
    profile: scope ?? undefined,
    remoteAuthMode: 'oauth',
    remoteUrl: draft.agentUrl,
    cloudOrg: draft.org || undefined
  }),

  card: ({ copy }: ConnectionCardContext): ConnectionModeCard => ({
    icon: Cloud,
    title: copy.cloudTitle,
    description: copy.cloudDesc
  }),

  ConfigPanel: CloudPanel
}
