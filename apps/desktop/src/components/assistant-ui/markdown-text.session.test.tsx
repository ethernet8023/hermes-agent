import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { __resetSessionLinkTitleCache } from '@/lib/session-link-title'
import { $sessions } from '@/store/session'
import { confirmNonMxcOwner } from '@/test/sandbox'
import type { SessionInfo } from '@/types/hermes'

import { ModelOutputOwnerProvider } from './model-output-policy'

const owner = { connectionId: 'session-ref-owner', profile: 'work' }
const originalDesktop = window.hermesDesktop
beforeEach(() => {
  confirmNonMxcOwner(owner)
  window.hermesDesktop = { ...originalDesktop, api: vi.fn(async () => ({ enabled: false })) } as typeof originalDesktop
})

import { MarkdownTextContent } from './markdown-text'

function makeSession(overrides: Partial<SessionInfo> = {}): SessionInfo {
  return {
    ended_at: null,
    id: '20260101_abc123',
    input_tokens: 0,
    is_active: false,
    last_active: 1_000,
    message_count: 1,
    model: null,
    output_tokens: 0,
    preview: null,
    profile: 'work',
    connection_id: owner.connectionId,
    source: 'cli',
    started_at: 1_000,
    title: 'Branch plan',
    tool_call_count: 0,
    ...overrides
  }
}

afterEach(() => {
  cleanup()
  window.hermesDesktop = originalDesktop
  $sessions.set([])
  __resetSessionLinkTitleCache()
})

// End-to-end for the agent-authored path: a bare ref in assistant markdown has
// to survive preprocessMarkdown -> Streamdown -> MarkdownLink and come out as
// an inline link titled after the session, not as literal text.
describe('MarkdownTextContent session refs', () => {
  it('renders an agent-written @session ref as a link showing the session title', async () => {
    $sessions.set([makeSession()])

    render(
      <ModelOutputOwnerProvider value={owner}>
        <MarkdownTextContent isRunning={false} text="Context lives in @session:work/20260101_abc123 today." />
      </ModelOutputOwnerProvider>
    )

    const link = await screen.findByTitle('work/20260101_abc123')

    expect(link.tagName).toBe('A')
    expect(link.textContent).toBe('Branch plan')
    expect(screen.queryByText(/@session:/)).toBeNull()
  })

  it('falls back to a short id when the session is unknown', async () => {
    render(
      <ModelOutputOwnerProvider value={owner}>
        <MarkdownTextContent isRunning={false} text="See @session:work/20260101_abc123 for context." />
      </ModelOutputOwnerProvider>
    )

    const link = await screen.findByTitle('work/20260101_abc123')

    expect(link.textContent).toBe('20260101…')
  })

  it('leaves a ref inside inline code as literal text', () => {
    render(<MarkdownTextContent isRunning={false} text="Write `@session:work/20260101_abc123` to link a chat." />)

    expect(screen.getByText('@session:work/20260101_abc123')).toBeTruthy()
    expect(screen.queryByTitle('work/20260101_abc123')).toBeNull()
  })
})
