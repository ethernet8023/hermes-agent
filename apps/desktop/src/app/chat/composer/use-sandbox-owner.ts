import { useStore } from '@nanostores/react'
import { computed } from 'nanostores'
import { useMemo } from 'react'

import { getApiRequestConnection } from '@/api/client'
import { type SandboxOwner } from '@/api/sandbox'
import { useSessionView } from '@/app/chat/session-view'
import { $activeGatewayProfile } from '@/store/profile'
import { $connection, $cronSessions, $messagingSessions, $sessions, $unlistedSessionOwnerRows } from '@/store/session'
import { ambientGatewayOwnsEverySession } from '@/store/session-owner-resolution'
import {
  $sessionStates,
  $sessionTiles,
  knownOwnerForSession,
  storedSessionIdForRuntimeId
} from '@/store/session-states'

/** Settings supplies its Applies-to selection; never infer it from the foreground chat. */
export function useSettingsSandboxOwner(profile?: string): SandboxOwner {
  const active = useStore($activeGatewayProfile)
  useStore($connection)
  const connectionId = getApiRequestConnection()

  return useMemo(() => ({ connectionId, profile: profile ?? active }), [profile, active, connectionId])
}

/** React to durable owner changes without subscribing to streaming message state. */
export function useKnownSessionSandboxOwner(
  sessionId: string | null | undefined,
  runtimeId?: string | null
): SandboxOwner | null {
  const ownerState = useMemo(() => {
    const identity = computed($sessionStates, states =>
      JSON.stringify([states[sessionId ?? '']?.storedSessionId, states[runtimeId ?? '']?.storedSessionId])
    )

    return computed(
      [$sessionTiles, $sessions, $cronSessions, $messagingSessions, $unlistedSessionOwnerRows, identity],
      () =>
        knownOwnerForSession(sessionId) ??
        (!sessionId || (runtimeId && storedSessionIdForRuntimeId(runtimeId) === sessionId)
          ? knownOwnerForSession(runtimeId)
          : undefined)
    )
  }, [sessionId, runtimeId])

  const known = useStore(ownerState)
  const profile = typeof known === 'string' ? known : known?.profile
  const connectionId = typeof known === 'string' ? null : known?.connectionId

  return useMemo(() => (profile ? { connectionId: connectionId ?? null, profile } : null), [profile, connectionId])
}

export function useSessionSandboxOwner(): SandboxOwner | null {
  const view = useSessionView()
  const runtime = useStore(view.$runtimeId)
  const stored = useStore(view.$storedId)
  const ambient = useSettingsSandboxOwner()
  // Cold replay can select a stored conversation before the previous runtime retires.
  // Its durable selection must never inherit that runtime's policy.
  const known = useKnownSessionSandboxOwner(stored, runtime)
  const fallback = !stored && (!runtime || ambientGatewayOwnsEverySession())

  return known ?? (fallback ? ambient : null)
}
