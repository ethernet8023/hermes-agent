// @vitest-environment jsdom
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { setApiRequestConnection, setApiRequestProfile } from '@/api/client'
import { saveHermesConfig, saveHermesConfigRecord } from '@/api/config'
import type { HermesApiRequest } from '@/global'
import { publishSandboxStatus, sandboxState } from '@/store/sandbox'
import type { SandboxStatus } from '@/types/hermes'

import { InlinePreviewDirective } from './inline-preview-directive'
import { ModelOutputOwnerProvider } from './model-output-policy'

const original = window.hermesDesktop
const owner = { connectionId: 'policy-owner', profile: 'policy-profile' }
const status = (enabled: boolean) => ({ enabled }) as SandboxStatus

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  window.hermesDesktop = original
})

it.each(['config', 'record'] as const)(
  'revokes at the start of a generic %s write and reconfirms the pinned owner',
  async kind => {
    publishSandboxStatus(status(false), owner)
    setApiRequestConnection(owner.connectionId)
    setApiRequestProfile(owner.profile)
    let finish!: () => void

    const api = vi.fn((request: HermesApiRequest): Promise<unknown> =>
      request.method === 'PUT'
        ? new Promise(resolve => {
            finish = () => resolve({ ok: true })
          })
        : Promise.resolve(status(true))
    )

    window.hermesDesktop = { ...original, api } as typeof original

    const write =
      kind === 'config'
        ? saveHermesConfig({ terminal: { backend: 'mxc' } }, owner.profile)
        : saveHermesConfigRecord({ terminal: { backend: 'mxc' } }, owner)

    expect(sandboxState(owner).get().confirmed).toBe(false)
    setApiRequestConnection('new-foreground')
    setApiRequestProfile('other')
    finish()
    await write
    expect(sandboxState(owner).get()).toMatchObject({ confirmed: true, status: { enabled: true } })

    for (const [request] of api.mock.calls) {
      expect(request).toMatchObject(owner)
    }
  }
)

it('refreshes observed owners while focused and revokes an already mounted executable document', async () => {
  vi.useFakeTimers()
  publishSandboxStatus(status(false), owner)
  let enabled = false

  const api = vi.fn(async (request: HermesApiRequest) =>
    request.path.startsWith('/api/sandbox/status')
      ? status(enabled)
      : { text: '<script>document.body.textContent="live"</script>' }
  )

  window.hermesDesktop = { ...original, api } as typeof original

  const view = render(
    <ModelOutputOwnerProvider value={owner}>
      <InlinePreviewDirective attrs={{ file: '/document.html' }} streaming={false} />
    </ModelOutputOwnerProvider>
  )

  await act(async () => {})
  expect(view.container.querySelector('iframe')).not.toBeNull()
  enabled = true
  await act(async () => {
    await vi.advanceTimersByTimeAsync(4000)
  })
  expect(view.container.querySelector('iframe')).toBeNull()

  for (const [request] of api.mock.calls) {
    expect(request).toMatchObject(owner)
  }
})
