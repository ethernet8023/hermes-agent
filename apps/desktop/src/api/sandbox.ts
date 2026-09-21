import type { SandboxGrantMode, SandboxStatus } from '@/types/hermes'

import { hermesApi, profileScoped } from './client'

// Windows sandbox (MXC) policy. Reads and writes both go through the backend's
// single resolver (config.yaml `terminal.*`), so this panel, the CLI and the
// running agent can never disagree about what is granted. A write applies to
// the agent's next command; nothing restarts.

export function getSandboxStatus(
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

  return hermesApi<SandboxStatus>({
    ...profileScoped(),
    path: `/api/sandbox/status${query ? `?${query}` : ''}`
  })
}

export interface SandboxPolicyUpdate {
  enabled?: boolean
  readwrite_paths?: string[]
  readonly_paths?: string[]
  network?: boolean
}

export function updateSandboxPolicy(update: SandboxPolicyUpdate): Promise<SandboxStatus> {
  return hermesApi<SandboxStatus>({
    ...profileScoped(),
    path: '/api/sandbox/policy',
    method: 'POST',
    body: update
  })
}

export function grantSandboxPath(
  path: string,
  mode: SandboxGrantMode
): Promise<SandboxStatus & { granted: string; mode: SandboxGrantMode }> {
  return hermesApi<SandboxStatus & { granted: string; mode: SandboxGrantMode }>({
    ...profileScoped(),
    path: '/api/sandbox/grant',
    method: 'POST',
    body: { path, mode }
  })
}

export interface SandboxPrepareResult extends SandboxStatus {
  prepared: string[]
  needs_admin: string[]
  admin_command: string
  errors: string[]
}

export function prepareSandboxWorkspace(path?: string): Promise<SandboxPrepareResult> {
  return hermesApi<SandboxPrepareResult>({
    ...profileScoped(),
    path: '/api/sandbox/prepare',
    method: 'POST',
    body: path ? { path } : {}
  })
}
