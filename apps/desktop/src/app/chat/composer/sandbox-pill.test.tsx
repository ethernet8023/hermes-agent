// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { atom } from 'nanostores'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { type SessionView, SessionViewProvider } from '@/app/chat/session-view'
import { en } from '@/i18n/en'
import { $routeRequest } from '@/store/recovery-requests'
import { $sandboxStatus } from '@/store/sandbox'
import type { SandboxStatus } from '@/types/hermes'

import { SandboxPill } from './sandbox-pill'

vi.mock('@/api/sandbox', () => ({
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
  $sandboxStatus.set(null)
})

describe('SandboxPill', () => {
  it('is absent where the backend cannot sandbox at all, or the kit is not in place', () => {
    $sandboxStatus.set(status({ platform_supported: false }))
    const first = mount()
    expect(screen.queryByTestId('sandbox-pill')).toBeNull()
    first.unmount()

    $sandboxStatus.set(status({ available: false, reason: 'wxc-exec not found' }))
    mount()
    expect(screen.queryByTestId('sandbox-pill')).toBeNull()
  })

  it("names this conversation's folder and the network state while the sandbox is on", async () => {
    $sandboxStatus.set(status({ enabled: true }))
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
    $sandboxStatus.set(status({ enabled: false }))
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
  })

  it('clicking the shield flips the sandbox through the policy route and every surface sees it', async () => {
    $sandboxStatus.set(status({ enabled: false }))
    updateMock.mockResolvedValueOnce(status({ enabled: true }))
    mount()

    await act(async () => {
      fireEvent.click(screen.getByTestId('sandbox-pill'))
    })

    expect(updateMock).toHaveBeenCalledWith({ enabled: true })
    expect($sandboxStatus.get()?.enabled).toBe(true)
    expect(screen.getByTestId('sandbox-pill').getAttribute('data-state-sandbox')).toBe('on')
  })
})
