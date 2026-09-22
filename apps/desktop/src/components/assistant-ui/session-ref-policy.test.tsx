// @vitest-environment jsdom
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { setApiRequestConnection, setApiRequestProfile } from '@/api/client'
import type { HermesApiRequest } from '@/global'
import { __resetSessionLinkTitleCache } from '@/lib/session-link-title'
import { publishSandboxStatus } from '@/store/sandbox'
import { $sessions } from '@/store/session'
import type { SandboxStatus } from '@/types/hermes'

import { MarkdownTextContent } from './markdown-text'
import { ModelOutputOwnerProvider } from './model-output-policy'

it('keeps a legacy primary lookup pinned rather than inheriting the foreground connection', async () => {
  const legacy = { connectionId: null, profile: 'shared' }
  publishSandboxStatus(status(false), legacy)
  setApiRequestConnection('foreign-foreground')

  const api = vi.fn(async (r: HermesApiRequest) =>
    r.path.startsWith('/api/sandbox/status') ? status(false) : { title: 'primary title' }
  )

  window.hermesDesktop = { ...original, api } as typeof original

  const view = render(
    <ModelOutputOwnerProvider value={legacy}>
      <MarkdownTextContent isRunning={false} text="[session](#session/shared/legacy-id)" />
    </ModelOutputOwnerProvider>
  )

  await waitFor(() => expect(view.container.textContent).toContain('primary title'))
  expect(api.mock.calls.find(([r]) => r.path.startsWith('/api/sessions/'))?.[0].connectionId).toBeUndefined()
})

const original = window.hermesDesktop
const a = { connectionId: 'title-a', profile: 'shared' }
const b = { connectionId: 'title-b', profile: 'shared' }
const status = (enabled: boolean) => ({ enabled }) as SandboxStatus

const message = (owner: typeof a, text = '[context](#session/shared/same-id)') => (
  <ModelOutputOwnerProvider value={owner}>
    <MarkdownTextContent isRunning={false} text={text} />
  </ModelOutputOwnerProvider>
)

afterEach(() => {
  cleanup()
  __resetSessionLinkTitleCache()
  $sessions.set([])
  window.hermesDesktop = original
})

it('does not mount a model-selected session resolver under MXC', async () => {
  publishSandboxStatus(status(true), b)
  const api = vi.fn(async () => ({ title: 'private' }))
  window.hermesDesktop = { ...original, api } as typeof original
  const view = render(message(b))
  await act(async () => {})
  expect(api).not.toHaveBeenCalled()
  expect(view.container.textContent).not.toContain('private')
  expect(view.container.textContent).toContain('context')
})

it('pins permitted title IO and caches to owner A→B→A and refuses foreign profiles', async () => {
  publishSandboxStatus(status(false), a)
  publishSandboxStatus(status(false), b)
  setApiRequestConnection('ambient')
  setApiRequestProfile('ambient')

  const api = vi.fn(async (r: HermesApiRequest) =>
    r.path.startsWith('/api/sandbox/status') ? status(false) : { title: `private-${r.connectionId}` }
  )

  window.hermesDesktop = { ...original, api } as typeof original
  const view = render(message(a))
  await waitFor(() => expect(view.container.textContent).toContain('private-title-a'))
  view.rerender(message(b))
  expect(view.container.textContent).not.toContain('private-title-a')
  await waitFor(() => expect(view.container.textContent).toContain('private-title-b'))
  view.rerender(message(a))
  await waitFor(() => expect(view.container.textContent).toContain('private-title-a'))
  const reads = api.mock.calls.filter(([r]) => r.path.startsWith('/api/sessions/'))
  expect(reads.map(([r]) => r.connectionId)).toEqual(['title-a', 'title-b'])
  expect(reads.every(([r]) => r.profile === 'shared')).toBe(true)
  api.mockClear()
  view.rerender(message(a, '[foreign](#session/foreign/hidden)'))
  await act(async () => {})
  expect(api.mock.calls.filter(([r]) => r.path.startsWith('/api/sessions/'))).toHaveLength(0)
})
