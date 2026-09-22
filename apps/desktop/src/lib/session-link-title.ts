/** Owner-scoped session-reference titles; user references may select another
 * profile, but model references cannot turn that spelling into authority. */
import { useStore } from '@nanostores/react'
import { useEffect, useMemo, useState } from 'react'

import { currentSandboxOwner, type SandboxOwner } from '@/api/sandbox'
import { getSession } from '@/hermes'
import { parseSessionRefValue, sessionRefCacheKey, sessionRefFallbackLabel } from '@/lib/session-refs'
import { confirmModelOutputAccess } from '@/store/sandbox'
import { $connection, $sessions, sessionMatchesStoredId } from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

const titleCache = new Map<string, string>()
const titleInflight = new Map<string, Promise<string>>()

function sessionRowTitle(row: SessionInfo): string {
  return row.title?.trim() || row.preview?.trim() || ''
}

function referenceOwner(value: string, modelOwner?: SandboxOwner | null): SandboxOwner | null {
  const { profile } = parseSessionRefValue(value)

  if (modelOwner !== undefined) {
    if (!modelOwner || (profile && profile !== (modelOwner.profile ?? 'default'))) {
      return null
    }

    return modelOwner
  }

  return currentSandboxOwner(profile)
}

export function lookupLocalSessionTitle(value: string, owner = referenceOwner(value)): string {
  const { sessionId } = parseSessionRefValue(value)

  if (!sessionId || !owner) {
    return ''
  }

  const row = $sessions
    .get()
    .find(
      session =>
        sessionMatchesStoredId(session, sessionId) &&
        (session.connection_id ?? null) === owner.connectionId &&
        (session.profile || 'default') === (owner.profile || 'default')
    )

  return row ? sessionRowTitle(row) : ''
}

export async function fetchSessionLinkTitle(value: string, modelOwner?: SandboxOwner | null): Promise<string> {
  const owner = referenceOwner(value, modelOwner)

  if (!owner) {
    return ''
  }

  const key = sessionRefCacheKey(value, owner)

  if (!key) {
    return ''
  }

  try {
    const check = modelOwner !== undefined ? await confirmModelOutputAccess(owner) : undefined
    const known = titleCache.get(key) || lookupLocalSessionTitle(value, owner)

    if (known) {
      return known
    }

    let inflight = titleInflight.get(key)

    if (!inflight) {
      const { sessionId } = parseSessionRefValue(value)
      check?.()
      inflight = Promise.resolve(
        getSession(sessionId, { connectionId: owner.connectionId ?? undefined, profile: owner.profile ?? 'default' })
      )
        .then(row => (row ? sessionRowTitle(row) : ''))
        .finally(() => titleInflight.delete(key))
      titleInflight.set(key, inflight)
    }

    const title = await inflight
    check?.()
    titleCache.set(key, title)

    return title
  } catch {
    return ''
  }
}

export function useSessionLinkTitle(value: string, fallbackLabel?: string, modelOwner?: SandboxOwner | null): string {
  useStore($connection)
  const resolvedOwner = referenceOwner(value, modelOwner)
  const connectionId = resolvedOwner?.connectionId
  const profile = resolvedOwner?.profile
  const known = resolvedOwner !== null

  const owner = useMemo(
    () => (known ? { connectionId: connectionId ?? null, profile: profile ?? null } : null),
    [known, connectionId, profile]
  )

  const key = owner ? sessionRefCacheKey(value, owner) : ''
  const fallback = fallbackLabel?.trim() || sessionRefFallbackLabel(value)
  const [resolved, setResolved] = useState({ key: '', title: '' })

  useEffect(() => {
    let active = true

    if (!key || !owner) {
      return
    }

    // Never paint a preceding connection's state while this lookup is pending.
    void fetchSessionLinkTitle(value, modelOwner === undefined ? undefined : owner).then(title => {
      if (active) {
        setResolved({ key, title })
      }
    })

    return () => {
      active = false
    }
  }, [key, value, owner, modelOwner])

  return (resolved.key === key ? resolved.title : '') || fallback
}

export function __resetSessionLinkTitleCache(): void {
  titleCache.clear()
  titleInflight.clear()
}
