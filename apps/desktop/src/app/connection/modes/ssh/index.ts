// app/connection/modes/ssh/index.ts — a backend on another machine over ssh.
//
// Availability is a machine fact decided in electron (backends/ssh.ts): an
// ssh client must exist to spawn. Every artifact kind offers ssh when one is
// present, INCLUDING a light build — which is exactly why first-run has to
// render this card rather than assuming remote is the only non-local option.

import type { DesktopConnectionConfig, DesktopConnectionConfigInput } from '@/global'
import { Terminal } from '@/lib/icons'

import type { ConnectionCardContext, ConnectionModeCard, ConnectionModeModule } from '../../types'

import { SshPanel } from './panel'

export interface SshDraft {
  host: string
  user: string
  port: number | null
  keyPath: string
  remoteHermesPath: string
  /** Which profile on the remote host to attach to. Profile scopes only. */
  remoteProfile: string
}

function emptySshDraft(): SshDraft {
  return { host: '', user: '', port: null, keyPath: '', remoteHermesPath: '', remoteProfile: '' }
}

export const sshMode: ConnectionModeModule<'ssh', SshDraft> = {
  mode: 'ssh',

  emptyDraft: emptySshDraft,

  fromSaved: (config: DesktopConnectionConfig | null): SshDraft =>
    config && config.mode === 'ssh'
      ? {
          host: config.sshHost,
          user: config.sshUser,
          port: config.sshPort,
          keyPath: config.sshKeyPath,
          remoteHermesPath: config.sshRemoteHermesPath,
          remoteProfile: config.sshRemoteProfile
        }
      : emptySshDraft(),

  toPayload: (draft: SshDraft, scope: null | string): DesktopConnectionConfigInput => ({
    mode: 'ssh',
    profile: scope ?? undefined,
    sshHost: draft.host.trim(),
    sshUser: draft.user.trim() || undefined,
    sshPort: draft.port,
    sshKeyPath: draft.keyPath.trim() || undefined,
    sshRemoteHermesPath: draft.remoteHermesPath.trim(),
    // Preserve an intentional blank so an existing remote-profile mapping can
    // be cleared instead of read as an omitted field.
    sshRemoteProfile: draft.remoteProfile.trim()
  }),

  card: ({ copy }: ConnectionCardContext): ConnectionModeCard => ({
    icon: Terminal,
    title: copy.sshTitle,
    description: copy.sshDesc,
    hint: copy.sshTrustHint
  }),

  ConfigPanel: SshPanel
}
