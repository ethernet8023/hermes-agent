// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { atom } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { type SessionView, SessionViewProvider } from '@/app/chat/session-view'
import { en } from '@/i18n/en'
import { $routeRequest } from '@/store/recovery-requests'
import { invalidateSandboxStatus, publishSandboxStatus, sandboxState } from '@/store/sandbox'
import { $sessionTiles, recordSessionEventScope } from '@/store/session-states'
const owner = { connectionId: 'sandbox-test-owner', profile: 'alpha' }
const seed = (value: SandboxStatus) => publishSandboxStatus(value, owner)
beforeEach(() => {
  recordSessionEventScope({ session_id: 'rt', ...owner })
  $sessionTiles.set([{ storedSessionId: 'st', runtimeId: 'rt' }])
})
import { setApiRequestConnection, setApiRequestProfile } from '@/api/client'
import { ensureGatewayAgent } from '@/store/profile'
import type { SandboxStatus } from '@/types/hermes'

import { SandboxPill } from './sandbox-pill'
vi.mock('@/store/profile', async original => ({
  ...(await original<Record<string, unknown>>()),
  ensureGatewayAgent: vi.fn(async (connection: string, profile: string) => {
    setApiRequestConnection(connection)
    setApiRequestProfile(profile)
  })
}))

vi.mock('@/api/sandbox', async original => ({
  ...(await original<Record<string, unknown>>()),
  getSandboxStatus: vi.fn(() => new Promise(() => undefined)),
  updateSandboxPolicy: vi.fn()
}))

import { updateSandboxPolicy } from '@/api/sandbox'
const updateMock = vi.mocked(updateSandboxPolicy)

const hover = async (element: HTMLElement) => {
  fireEvent.mouseEnter(element)
  await waitFor(() => expect(screen.queryByTestId('sandbox-pill-state')).not.toBeNull())
}

const status = (over: Partial<SandboxStatus> = {}): SandboxStatus =>
  ({
    platform_supported: true,
    available: true,
    enabled: true,
    degraded: false,
    reason: null,
    warnings: [],
    shell_missing: false,
    workspace: 'C:\\Users\\me\\Hermes',
    containers_started: 0,
    policy: { readwrite_paths: [], readonly_paths: [], network: false },
    ...over
  }) as SandboxStatus

const view = (cwd: string): SessionView => ({
  kind: 'tile',
  $awaitingResponse: atom(false),
  $busy: atom(false),
  $cwd: atom(cwd),
  $fast: atom(false),
  $lastVisibleIsUser: atom(false),
  $messages: atom([]),
  $messagesEmpty: atom(true),
  $model: atom('m'),
  $provider: atom('p'),
  $reasoningEffort: atom(''),
  $runtimeId: atom('rt'),
  $storedId: atom('st'),
  $turnStartedAt: atom<number | null>(null)
})

const mount = (cwd = 'C:\\Users\\me\\Hermes') =>
  render(
    <SessionViewProvider value={view(cwd)}>
      <SandboxPill disabled={false} />
    </SessionViewProvider>
  )

afterEach(() => {
  cleanup()
  $sessionTiles.set([])
  sandboxState(owner).set({ status: null, confirmed: false, busy: false, error: null })
  invalidateSandboxStatus(owner)
})

describe('SandboxPill', () => {
  it('offers keyboard explanation without changing policy or moving focus', async () => {
    seed(status())
    mount()
    const pill = screen.getByTestId('sandbox-pill')
    await act(async () => pill.focus())
    const settings = await screen.findByRole('button', { name: en.composer.sandbox.openSettings })
    expect(pill.ownerDocument.activeElement).toBe(pill)
    expect(pill.getAttribute('aria-expanded')).toBe('true')
    settings.focus()
    expect(pill.ownerDocument.activeElement).toBe(settings)
  })

  it('keeps recovery available without displaying a confirmed ring', async () => {
    seed(status({ available: false, reason: 'missing kit' }))
    updateMock.mockResolvedValueOnce(status({ enabled: false, available: false }))
    mount()
    const pill = screen.getByTestId('sandbox-pill')
    expect(pill).toHaveProperty('disabled', false)
    expect(screen.queryByTestId('sandbox-pill-arc')).toBeNull()
    await act(async () => fireEvent.click(pill))
    expect(updateMock).toHaveBeenCalledWith({ enabled: false }, owner)
  })
  it('is absent where the backend cannot sandbox at all, or the kit is not in place', () => {
    seed(status({ enabled: false, platform_supported: false }))
    const first = mount()
    expect(screen.queryByTestId('sandbox-pill')).toBeNull()
    first.unmount()

    seed(status({ enabled: false, available: false, reason: 'wxc-exec not found' }))
    mount()
    expect(screen.queryByTestId('sandbox-pill')).toBeNull()
  })

  it("names this conversation's folder and the network state while the sandbox is on", async () => {
    seed(status({ enabled: true }))
    mount('C:\\Users\\me\\Hermes')
    const pill = screen.getByTestId('sandbox-pill')
    expect(pill.getAttribute('data-state-sandbox')).toBe('on')
    expect(screen.getByTestId('sandbox-pill-arc')).toBeTruthy()

    await hover(pill)

    expect(screen.getByTestId('sandbox-pill-state').textContent).toBe(en.composer.sandbox.on)
    expect(screen.getByText(/Users[\\/]me[\\/]Hermes|~[\\/]Hermes/)).toBeTruthy()
    expect(screen.getByText(new RegExp(en.composer.sandbox.networkOff))).toBeTruthy()
  })

  it('explains the off state and routes to the sandbox settings', async () => {
    setApiRequestConnection('different-source')
    setApiRequestProfile('different-profile')
    seed(status({ enabled: false }))
    mount()
    const pill = screen.getByTestId('sandbox-pill')
    expect(pill.getAttribute('data-state-sandbox')).toBe('off')
    expect(screen.queryByTestId('sandbox-pill-arc')).toBeNull()

    await hover(pill)

    expect(screen.getByText(en.composer.sandbox.descriptionOff)).toBeTruthy()
    const before = $routeRequest.get()?.seq ?? 0

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: en.composer.sandbox.openSettings }))
    })

    const request = $routeRequest.get()
    expect(request?.seq).toBeGreaterThan(before)
    expect(request?.path).toBe('/settings?tab=config:safety')
    expect(ensureGatewayAgent).toHaveBeenCalledWith(owner.connectionId, owner.profile)
  })

  it('clicking the shield flips the sandbox through the policy route and every surface sees it', async () => {
    seed(status({ enabled: false }))
    updateMock.mockResolvedValueOnce(status({ enabled: true }))
    mount()

    await act(async () => {
      fireEvent.click(screen.getByTestId('sandbox-pill'))
    })

    expect(updateMock).toHaveBeenCalledWith({ enabled: true }, owner)
    expect(sandboxState(owner).get().status?.enabled).toBe(true)
    expect(screen.getByTestId('sandbox-pill').getAttribute('data-state-sandbox')).toBe('on')
  })
})
