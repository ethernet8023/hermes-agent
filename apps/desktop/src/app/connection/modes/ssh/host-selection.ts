// app/connection/modes/ssh/host-selection.ts — pure draft transitions for
// picking and enriching an ssh host.
//
// Moved here from settings/ with the mode's own field names: these only ever
// operate on the ssh draft, so they belong to the ssh module rather than to
// a settings-wide helpers file.

import type { SshDraft } from './index'

export interface ResolvedSshHost {
  identityFile?: string | null
  port?: number | null
  user?: string | null
}

/**
 * Choosing a different host invalidates everything that described the old
 * one. Reselecting the same host is a no-op, preserving what the user typed.
 */
export function selectSshHost(draft: SshDraft, host: string): SshDraft {
  if (host === draft.host) {
    return draft
  }

  return { ...draft, host, user: '', port: null, keyPath: '', remoteHermesPath: '' }
}

/**
 * Fill blanks from an ~/.ssh/config lookup without overwriting anything the
 * user typed, and only for the host that produced the result — an earlier
 * lookup landing late must not enrich a host the user has since changed.
 * Port 22 stays null so the field shows the default rather than pinning it.
 */
export function enrichSelectedSshHost(draft: SshDraft, host: string, resolved: ResolvedSshHost): SshDraft {
  if (draft.host !== host) {
    return draft
  }

  return {
    ...draft,
    user: draft.user || resolved.user || '',
    port: draft.port ?? (resolved.port === 22 ? null : (resolved.port ?? null)),
    keyPath: draft.keyPath || resolved.identityFile || ''
  }
}
