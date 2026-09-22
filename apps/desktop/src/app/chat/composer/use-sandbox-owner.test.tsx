// @vitest-environment jsdom
import { useStore } from '@nanostores/react'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { atom } from 'nanostores'
import { afterEach, expect, it, vi } from 'vitest'

import { setApiRequestConnection, setApiRequestProfile } from '@/api/client'
import { PRIMARY_SESSION_VIEW, SessionViewProvider } from '@/app/chat/session-view'
import { SandboxPanel } from '@/app/settings/sandbox-panel'
import { en } from '@/i18n/en'
import { publishSandboxStatus } from '@/store/sandbox'
import { $sessionTiles, recordSessionEventScope } from '@/store/session-states'
import { $settingsRequestProfile, $settingsScopeOverride } from '@/store/settings-scope'
import type { SandboxStatus } from '@/types/hermes'

import { SandboxPill } from './sandbox-pill'
import { useSessionSandboxOwner, useSettingsSandboxOwner } from './use-sandbox-owner'

it('requires a proven runtime/durable pairing for an otherwise unknown stored owner', () => {
  const owner = { connectionId: 'paired-device', profile: 'paired-profile' }
  recordSessionEventScope({ session_id: 'paired-runtime', ...owner })
  const stored = atom<string | null>('unknown-selection')

  function Probe() {
    return <span>{JSON.stringify(useSessionSandboxOwner())}</span>
  }

  const view = render(
    <SessionViewProvider value={{ ...PRIMARY_SESSION_VIEW, $runtimeId: atom('paired-runtime'), $storedId: stored }}>
      <Probe />
    </SessionViewProvider>
  )

  expect(view.container.textContent).toBe('null')
  act(() => $sessionTiles.set([{ storedSessionId: 'unknown-selection', runtimeId: 'paired-runtime' }]))
  expect(view.container.textContent).toBe(JSON.stringify(owner))
  act(() => stored.set(null))
  expect(view.container.textContent).toBe(JSON.stringify(owner))
  $sessionTiles.set([])
})

vi.mock('@/store/notifications', () => ({ notify: vi.fn(), notifyError: vi.fn() }))
const previous = window.hermesDesktop

const status = (enabled: boolean) =>
  ({
    enabled,
    platform_supported: true,
    available: true,
    warnings: [],
    policy: { readonly_paths: [], readwrite_paths: [], network: false }
  }) as unknown as SandboxStatus

function SettingsScopeHarness() {
  const profile = useStore($settingsRequestProfile)
  const owner = useSettingsSandboxOwner(profile)

  return <SandboxPanel owner={owner} />
}

afterEach(() => {
  cleanup()
  $settingsScopeOverride.set(null)
  window.hermesDesktop = previous
})

it('uses Settings Applies-to even when the foreground chat names a different profile', async () => {
  const api = vi.fn(async () => status(false))
  window.hermesDesktop = { ...previous, api } as typeof window.hermesDesktop
  setApiRequestConnection('settings-device')
  setApiRequestProfile('foreground-alpha')
  $settingsScopeOverride.set('settings-beta')
  render(<SettingsScopeHarness />)
  const toggle = await screen.findByRole('switch', { name: en.settings.sandbox.toggleLabel })
  await act(async () => fireEvent.click(toggle))
  expect(api.mock.calls).not.toHaveLength(0)

  for (const [request] of api.mock.calls as unknown as Array<[Record<string, unknown>]>) {
    expect(request).toMatchObject({ connectionId: 'settings-device', profile: 'settings-beta' })
  }
})

it('rehomes a reused composer to its proven session owner rather than the ambient source', async () => {
  const a = { connectionId: 'session-device-a', profile: 'alpha' }
  const b = { connectionId: 'session-device-b', profile: 'beta' }
  recordSessionEventScope({ session_id: 'owned-a', ...a })
  recordSessionEventScope({ session_id: 'owned-b', ...b })
  publishSandboxStatus(status(true), a)
  publishSandboxStatus(status(false), b)
  const api = vi.fn(async () => status(true))
  window.hermesDesktop = { ...previous, api } as typeof window.hermesDesktop
  setApiRequestConnection('unrelated-device')
  setApiRequestProfile('unrelated-profile')
  const runtime = atom<string | null>('owned-a')
  render(
    <SessionViewProvider value={{ ...PRIMARY_SESSION_VIEW, $runtimeId: runtime, $storedId: atom(null) }}>
      <SandboxPill disabled={false} />
    </SessionViewProvider>
  )
  expect(screen.getByTestId('sandbox-pill').getAttribute('aria-pressed')).toBe('true')
  await act(async () => runtime.set('owned-b'))
  expect(screen.getByTestId('sandbox-pill').getAttribute('aria-pressed')).toBe('false')
  await act(async () => fireEvent.click(screen.getByTestId('sandbox-pill')))
  expect(api.mock.calls[0]).toEqual([expect.objectContaining({ ...b, body: { enabled: true } })])
})
