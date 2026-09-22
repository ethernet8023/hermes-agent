// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { setApiRequestConnection, setApiRequestProfile } from '@/api/client'
import type { SandboxStatus } from '@/types/hermes'

import {
  invalidateSandboxStatus,
  publishSandboxStatus,
  refreshSandboxStatus,
  sandboxState,
  toggleSandbox
} from './sandbox'

const api = vi.fn()
const alpha = { connectionId: 'remote-a', profile: 'alpha' }
const beta = { connectionId: 'remote-b', profile: 'beta' }

const status = (enabled: boolean): SandboxStatus =>
  ({
    enabled,
    available: true,
    platform_supported: true,
    policy: { readonly_paths: [], readwrite_paths: [], network: false }
  }) as unknown as SandboxStatus

const deferred = <T>() => {
  let resolve!: (value: T) => void

  const promise = new Promise<T>(r => {
    resolve = r
  })

  return { resolve, promise }
}

beforeEach(() => {
  window.hermesDesktop = { api } as unknown as typeof window.hermesDesktop
  api.mockReset()
  setApiRequestConnection('ambient-connection')
  setApiRequestProfile('ambient-profile')

  for (const owner of [alpha, beta]) {
    sandboxState(owner).set({ status: null, confirmed: false, busy: false, error: null })
    invalidateSandboxStatus(owner)
  }
})

describe('sandbox ownership and stale response fencing', () => {
  it('keeps identical profile names on different connections separate', async () => {
    const peer = { ...alpha, connectionId: 'remote-peer' }
    const old = deferred<SandboxStatus>()
    api.mockReturnValueOnce(old.promise)
    const pending = refreshSandboxStatus(alpha)
    api.mockResolvedValueOnce(status(false))
    await refreshSandboxStatus(peer)
    old.resolve(status(true))
    await pending
    expect(sandboxState(peer).get().status?.enabled).toBe(false)
    expect(sandboxState(alpha).get().status?.enabled).toBe(true)
  })

  it('discards a response from a retired connection generation', async () => {
    publishSandboxStatus(status(true), alpha)
    const old = deferred<SandboxStatus>()
    api.mockReturnValueOnce(old.promise)
    const pending = refreshSandboxStatus(alpha)
    invalidateSandboxStatus(alpha)
    old.resolve(status(true))
    await pending
    expect(sandboxState(alpha).get().confirmed).toBe(false)
  })

  it('retains configuration but withdraws protection on transport and transition failures', async () => {
    publishSandboxStatus(status(true), alpha)
    api.mockRejectedValueOnce(new Error('offline'))
    await refreshSandboxStatus(alpha)
    expect(sandboxState(alpha).get()).toMatchObject({ confirmed: false, status: { enabled: true } })
    api.mockRejectedValueOnce(new Error('could not retire old process'))
    await expect(toggleSandbox(alpha)).rejects.toThrow('could not retire')
    expect(sandboxState(alpha).get()).toMatchObject({ confirmed: false, busy: false })
  })
  it('pins reads and writes to the explicit owner rather than ambient scope', async () => {
    api.mockResolvedValue(status(true))
    await refreshSandboxStatus(beta)
    expect(api.mock.calls[0][0]).toMatchObject(beta)
    await toggleSandbox(beta)
    expect(api.mock.calls[1][0]).toMatchObject({ ...beta, body: { enabled: false } })
  })
  it('does not let a read begun before a mutation repaint protection', async () => {
    publishSandboxStatus(status(true), alpha)
    const old = deferred<SandboxStatus>()
    api.mockReturnValueOnce(old.promise)
    const pending = refreshSandboxStatus(alpha)
    api.mockResolvedValueOnce(status(false))
    await toggleSandbox(alpha)
    old.resolve(status(true))
    await pending
    expect(sandboxState(alpha).get().status?.enabled).toBe(false)
  })
})
