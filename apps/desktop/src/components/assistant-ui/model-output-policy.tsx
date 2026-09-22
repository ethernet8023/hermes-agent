import { useStore } from '@nanostores/react'
import { atom } from 'nanostores'
import { createContext, useContext, useEffect, useMemo } from 'react'

import { type SandboxOwner, sandboxOwnerKey } from '@/api/sandbox'
import { useSessionSandboxOwner } from '@/app/chat/composer/use-sandbox-owner'
import { useSessionView } from '@/app/chat/session-view'
import { useI18n } from '@/i18n'
import { observeSandboxStatus, refreshSandboxStatus, type SandboxSnapshot, sandboxState } from '@/store/sandbox'

const unresolved = atom<SandboxSnapshot>({ status: null, confirmed: false, busy: false, error: null })
const pending = new Set<string>()
const ModelOutputOwnerContext = createContext<SandboxOwner | null | undefined>(undefined)
/** Static transcripts must supply provenance rather than inherit the foreground session. */
export const ModelOutputOwnerProvider = ModelOutputOwnerContext.Provider

export function useModelOutputOwner(ownerOverride?: SandboxOwner | null): SandboxOwner | null {
  const sessionOwner = useSessionSandboxOwner()
  const contextOwner = useContext(ModelOutputOwnerContext)
  const explicitOwner = ownerOverride === undefined ? contextOwner : ownerOverride
  const owner = explicitOwner === undefined ? sessionOwner : explicitOwner

  const view = useSessionView()
  const stored = useStore(view.$storedId)
  const runtime = useStore(view.$runtimeId)
  const sessionId = explicitOwner === undefined ? stored || runtime || undefined : owner?.sessionId
  const connectionId = owner?.connectionId
  const profile = owner?.profile
  const known = owner !== null

  return useMemo(
    () =>
      known
        ? { connectionId: connectionId ?? null, profile: profile ?? null, ...(sessionId ? { sessionId } : {}) }
        : null,
    [known, connectionId, profile, sessionId]
  )
}

/** Model-controlled host IO is allowed only by a confirmed non-MXC owner. */
export function useModelOutputRestriction(ownerOverride?: SandboxOwner | null): 'sandbox' | 'unconfirmed' | null {
  const owner = useModelOutputOwner(ownerOverride)
  const snapshot = useStore(owner ? sandboxState(owner) : unresolved)

  useEffect(() => {
    if (!owner || snapshot.confirmed || snapshot.busy || snapshot.error) {
      return
    }

    const key = sandboxOwnerKey(owner)

    if (!pending.has(key)) {
      pending.add(key)
      void refreshSandboxStatus(owner).finally(() => pending.delete(key))
    }
  }, [owner, snapshot.confirmed, snapshot.busy, snapshot.error])

  useEffect(() => (owner ? observeSandboxStatus(owner) : undefined), [owner])

  if (snapshot.status?.enabled === true) {
    return 'sandbox'
  }

  return snapshot.confirmed && !snapshot.busy && snapshot.status?.enabled === false ? null : 'unconfirmed'
}

export function InertModelOutput({ reason, target }: { reason: 'sandbox' | 'unconfirmed'; target?: string }) {
  const { t } = useI18n()

  return (
    <span className="my-2 block wrap-anywhere text-sm text-muted-foreground">
      {reason === 'sandbox' ? t.settings.sandbox.modelOutputBlocked : t.settings.sandbox.modelOutputUnconfirmed}
      {target && <code className="mt-1 block whitespace-pre-wrap">{target}</code>}
    </span>
  )
}
