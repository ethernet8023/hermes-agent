import type { SandboxGrantMode, SandboxStatus } from '@/types/hermes'

import { getApiRequestConnection, getApiRequestProfile, hermesApi } from './client'

/** Capture once before async work. Null deliberately pins the legacy primary route. */
export interface SandboxOwner {
  connectionId: string | null
  profile: string | null
  /** Optional proven transcript identity for session-relative model files. */
  sessionId?: string
}

export function currentSandboxOwner(profile = getApiRequestProfile()): SandboxOwner {
  return { connectionId: getApiRequestConnection(), profile }
}

export function sandboxOwnerKey(owner: SandboxOwner): string {
  return JSON.stringify([owner.connectionId, owner.profile ?? 'default'])
}

const scoped = (owner: SandboxOwner) => ({
  connectionId: owner.connectionId ?? undefined,
  profile: owner.profile ?? undefined
})

export function getSandboxStatus(
  owner: SandboxOwner,
  options: { provision?: boolean; refresh?: boolean; workspace?: string } = {}
): Promise<SandboxStatus> {
  const params = new URLSearchParams()

  if (options.provision) {
    params.set('provision', 'true')
  }

  if (options.refresh) {
    params.set('refresh', 'true')
  }

  if (options.workspace) {
    params.set('workspace', options.workspace)
  }

  const query = params.toString()

  return hermesApi<SandboxStatus>({ ...scoped(owner), path: `/api/sandbox/status${query ? `?${query}` : ''}` })
}

export interface SandboxPolicyUpdate {
  enabled?: boolean
  readwrite_paths?: string[]
  readonly_paths?: string[]
  network?: boolean
}

export function updateSandboxPolicy(update: SandboxPolicyUpdate, owner: SandboxOwner): Promise<SandboxStatus> {
  return hermesApi<SandboxStatus>({ ...scoped(owner), path: '/api/sandbox/policy', method: 'POST', body: update })
}

export interface SandboxGrantTarget {
  target: string
  recursive: true
}

export function getSandboxGrantTarget(path: string, owner: SandboxOwner): Promise<SandboxGrantTarget> {
  return hermesApi<SandboxGrantTarget>({
    ...scoped(owner),
    path: `/api/sandbox/grant-target?${new URLSearchParams({ path })}`
  })
}

export function grantSandboxPath(
  path: string,
  mode: SandboxGrantMode,
  owner: SandboxOwner
): Promise<SandboxStatus & { granted: string; mode: SandboxGrantMode }> {
  return hermesApi({
    ...scoped(owner),
    path: '/api/sandbox/grant',
    method: 'POST',
    body: { path, mode, expected_target: path }
  })
}

export function revokeSandboxPath(path: string, owner: SandboxOwner): Promise<SandboxStatus> {
  return hermesApi({ ...scoped(owner), path: '/api/sandbox/grant', method: 'DELETE', body: { path } })
}
