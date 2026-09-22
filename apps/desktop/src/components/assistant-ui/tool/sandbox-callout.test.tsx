// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { atom } from 'nanostores'
import { useRef } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
vi.mock('@assistant-ui/react', async original => ({
  ...(await original<Record<string, unknown>>()),
  useAui: () => ({ composer: () => ({ setText: vi.fn() }) }),
  useComposerRuntime: () => ({ getState: () => ({ text: '' }), subscribe: () => () => undefined }),
  useAuiState: (select: (state: unknown) => unknown) => select({ composer: { text: '' } })
}))
vi.mock('@/store/notifications', () => ({ notify: vi.fn(), notifyError: vi.fn() }))
import { setApiRequestConnection, setApiRequestProfile } from '@/api/client'
import { requestComposerInsert } from '@/app/chat/composer/focus'
import { useComposerDraft } from '@/app/chat/composer/hooks/use-composer-draft'
import { ComposerScopeProvider, MAIN_COMPOSER_SCOPE } from '@/app/chat/composer/scope'
import { PRIMARY_SESSION_VIEW, SessionViewProvider } from '@/app/chat/session-view'
import { en } from '@/i18n/en'
import { stashSessionDraft, takeSessionDraft } from '@/store/composer'
import { $sessionTiles, recordSessionEventScope } from '@/store/session-states'

import { SandboxDenialCallout } from './sandbox-callout'

const api = vi.fn()
const previous = window.hermesDesktop
const target = 'C:/Outside'

const status = {
  enabled: true,
  available: true,
  platform_supported: true,
  policy: { readonly_paths: [target], readwrite_paths: [], network: false }
}

function Editor({ id, draftKey = id, runtimeId = id }: { id: string; draftKey?: string; runtimeId?: string | null }) {
  const queueEditRef = useRef(null)

  const draft = useComposerDraft({
    activeQueueSessionKey: draftKey,
    focusKey: id,
    inputDisabled: false,
    queueEditRef,
    sessionId: runtimeId
  })

  return <div contentEditable data-testid={id} ref={draft.editorRef} />
}

const view = { ...PRIMARY_SESSION_VIEW, $runtimeId: atom('origin-session'), $storedId: atom('origin-stored') }
beforeEach(() => {
  $sessionTiles.set([{ storedSessionId: 'origin-stored', runtimeId: 'origin-session' }])
  stashSessionDraft('origin-session', '', [])
  stashSessionDraft('other-session', '', [])
  api
    .mockReset()
    .mockImplementation(async request =>
      request.method === 'POST' ? { ...status, granted: target, mode: request.body.mode } : { target, recursive: true }
    )
  window.hermesDesktop = { ...previous, api }
  setApiRequestProfile('other-profile')
  setApiRequestConnection('other-connection')
  recordSessionEventScope({
    session_id: 'origin-session',
    connectionId: 'origin-connection',
    profile: 'origin-profile'
  })
})
afterEach(() => {
  cleanup()
  $sessionTiles.set([])
  window.hermesDesktop = previous
})

it('shows resolved recursive consent before granting and appends only to the originating live editor', async () => {
  render(
    <>
      <ComposerScopeProvider value={{ ...MAIN_COMPOSER_SCOPE, target: 'origin-composer' }}>
        <SessionViewProvider value={view}>
          <Editor id="origin-session" />
          <SandboxDenialCallout sandbox={{ backend: 'mxc', denied: ['C:/Outside/x.txt'] }} />
        </SessionViewProvider>
      </ComposerScopeProvider>
      <ComposerScopeProvider value={{ ...MAIN_COMPOSER_SCOPE, target: 'other-composer' }}>
        <Editor id="other-session" />
      </ComposerScopeProvider>
    </>
  )
  await waitFor(() =>
    expect(screen.getByTestId('sandbox-denial').textContent).toContain(`Includes all files and subfolders in ${target}`)
  )
  expect(api.mock.calls[0][0]).toMatchObject({ connectionId: 'origin-connection', profile: 'origin-profile' })
  expect(api.mock.calls[0][0].path).toContain('/api/sandbox/grant-target?')
  const other = screen.getByTestId('other-session')
  await act(async () => {
    await requestComposerInsert('Keep my existing draft.', {
      target: 'origin-composer',
      sessionId: 'origin-session',
      focus: false
    })
  })
  other.focus()
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: en.assistant.tool.sandboxAllowRead }))
  })
  await waitFor(() => expect(screen.getByTestId('origin-session').textContent).toContain(target))
  expect(screen.getByTestId('origin-session').textContent).toContain('Keep my existing draft.')
  expect(other.textContent).toBe('')
  expect(other.ownerDocument.activeElement).toBe(other)
  expect(api.mock.calls.find(([r]) => r.method === 'POST')?.[0]).toMatchObject({
    body: { path: target, mode: 'read' },
    connectionId: 'origin-connection',
    profile: 'origin-profile'
  })
  expect(screen.getByRole('button', { name: en.assistant.tool.sandboxAllowReadWrite })).toBeTruthy()
})

it('stashes a delayed retry for its original session when the composer has been rehomed', async () => {
  let finish!: (value: unknown) => void
  api.mockImplementation(request =>
    request.method === 'POST'
      ? new Promise(resolve => {
          finish = resolve
        })
      : Promise.resolve({ target, recursive: true })
  )
  const scope = { ...MAIN_COMPOSER_SCOPE, target: 'shared-main' }

  const initial = render(
    <ComposerScopeProvider value={scope}>
      <SessionViewProvider value={view}>
        <Editor draftKey="origin-stored" id="origin-session" />
        <SandboxDenialCallout sandbox={{ backend: 'mxc', denied: ['C:/Outside/x.txt'] }} />
      </SessionViewProvider>
    </ComposerScopeProvider>
  )

  const allow = await screen.findByRole('button', { name: en.assistant.tool.sandboxAllowRead })
  await act(async () => {
    await requestComposerInsert('Original draft.', { target: 'shared-main', sessionId: 'origin-session', focus: false })
  })
  fireEvent.click(allow)
  await waitFor(() => expect(finish).toBeTypeOf('function'))
  initial.rerender(
    <ComposerScopeProvider value={scope}>
      <Editor id="replacement-session" />
    </ComposerScopeProvider>
  )
  await act(async () => {
    finish({ ...status, granted: target, mode: 'read' })
  })
  await waitFor(() => expect(takeSessionDraft('origin-stored').text).toContain(target))
  expect(takeSessionDraft('origin-stored').text).toContain('Original draft.')
  expect(screen.getByTestId('replacement-session').textContent).toBe('')
})

it('does not confuse two cold sessions that both lack a runtime id', async () => {
  render(
    <ComposerScopeProvider value={{ ...MAIN_COMPOSER_SCOPE, target: 'cold-composer' }}>
      <Editor draftKey="replacement-stored" id="cold-replacement" runtimeId={null} />
    </ComposerScopeProvider>
  )
  let handled: boolean | undefined
  await act(async () => {
    handled = await requestComposerInsert('Retry for original.', {
      target: 'cold-composer',
      sessionId: null,
      sessionKey: 'original-stored',
      focus: false
    })
  })
  expect(handled).toBe(false)
  expect(screen.getByTestId('cold-replacement').textContent).toBe('')
})
