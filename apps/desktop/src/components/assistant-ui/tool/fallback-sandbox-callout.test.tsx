// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@assistant-ui/react', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useAuiState: (select: (state: unknown) => unknown) =>
    select({ message: { id: 'msg-1', status: { type: 'complete' } }, thread: { isRunning: false } })
}))

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  grantSandboxPath: vi.fn()
}))

const { ToolFallback } = await import('./fallback')

const REFUSAL = [
  'Access to C:\\Users\\me\\mxc was refused:',
  'ls: C:/Users/me/mxc: Permission denied',
  '[Sandbox] Windows MXC denied access outside the sandbox policy:',
  '  denied: C:\\Users\\me\\mxc',
  '  read/write: C:\\Demo'
].join('\n')

function renderRow(result: unknown, toolName = 'search_files') {
  const props = {
    args: { path: 'C:\\Users\\me\\mxc', pattern: '*' },
    result,
    toolCallId: 'call-1',
    toolName
  } as unknown as ComponentProps<typeof ToolFallback>

  render(<ToolFallback {...props} />)
}

const expandCard = () => {
  const toggle = screen.getAllByRole('button').find(button => button.hasAttribute('aria-expanded'))
  expect(toggle).toBeTruthy()
  fireEvent.click(toggle as Element)
}

afterEach(() => {
  cleanup()
})

describe('sandbox refusal on a tool card', () => {
  // Refusals are frequent and often incidental (a probe of a parent folder), so the grant
  // callout lives with the rest of the output behind the disclosure; the collapsed card shows
  // the badge and the "blocked" subtitle only.
  it('keeps the grant callout behind the disclosure for a refused file tool', () => {
    renderRow({ error: REFUSAL })

    expect(screen.queryByTestId('sandbox-denial')).toBeNull()
    expect(screen.getByTestId('sandbox-pill').textContent).toBe('MXC')

    expandCard()

    const callout = screen.getByTestId('sandbox-denial')
    expect(callout.textContent).toContain('C:\\Users\\me\\mxc')
    expect(screen.getByRole('button', { name: 'Allow reading' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Allow read & write' })).toBeTruthy()
  })

  it('shows the callout with the output for a refused terminal command carrying the structured field', () => {
    renderRow(
      {
        output: 'cp: cannot create C:/Users/me/Documents/x: Permission denied',
        exit_code: 1,
        sandbox: { backend: 'mxc', container: 'hermes-1', denied: ['C:\\Users\\me\\Documents\\x'] }
      },
      'terminal'
    )

    // Terminal cards open by default, so the callout is already in view with the output.
    expect(screen.getByTestId('sandbox-denial').textContent).toContain('C:\\Users\\me\\Documents\\x')
  })

  it('renders no callout for a clean sandboxed command', () => {
    renderRow({ output: 'ok', exit_code: 0, sandbox: { backend: 'mxc', container: 'hermes-2', denied: [] } }, 'terminal')

    expect(screen.queryByTestId('sandbox-denial')).toBeNull()
    expect(screen.getByTestId('sandbox-pill')).toBeTruthy()
  })
})
