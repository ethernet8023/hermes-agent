import { describe, expect, it } from 'vitest'

import { buildToolView, sandboxInfo, type ToolPart } from './fallback-model'

const part = (overrides: Partial<ToolPart>): ToolPart => ({
  args: { command: 'echo hi' },
  isError: false,
  result: {},
  toolCallId: 'call_1',
  toolName: 'terminal',
  type: 'tool-call',
  ...overrides
})

const NOTE = [
  'cat: can\'t open \'C:/Users/me/Documents/taxes.pdf\': Permission denied',
  '',
  '[Sandbox] Windows MXC denied access outside the sandbox policy:',
  '  denied: C:\\Users\\me\\Documents\\taxes.pdf',
  '  read/write: C:\\Demo',
  '  read-only: (none)',
  '  network: off'
].join('\n')

describe('sandbox-aware tool views', () => {
  it('surfaces a clean sandboxed command as an ordinary success with the container badge data', () => {
    const view = buildToolView(
      part({ result: { output: 'hi', exit_code: 0, sandbox: { backend: 'mxc', container: 'hermes-1', denied: [] } } }),
      ''
    )

    expect(view.status).toBe('success')
    expect(view.sandbox).toEqual({ backend: 'mxc', container: 'hermes-1', denied: [] })
  })

  it('paints a refused command amber with the policy subtitle and keeps the refused paths', () => {
    const view = buildToolView(
      part({
        result: {
          output: NOTE,
          exit_code: 1,
          sandbox: { backend: 'mxc', container: 'hermes-2', denied: ['C:\\Users\\me\\Documents\\taxes.pdf'] }
        }
      }),
      ''
    )

    expect(view.status).toBe('warning')
    expect(view.subtitle).toBe('Blocked by sandbox policy')
    expect(view.sandbox?.denied).toEqual(['C:\\Users\\me\\Documents\\taxes.pdf'])
  })

  it('recovers the refused paths from the plain-text note when a file tool carries no structured field', () => {
    const info = sandboxInfo(part({ toolName: 'read_file', result: `Error: ${NOTE}` }), { error: `Error: ${NOTE}` })

    expect(info).toEqual({ backend: 'mxc', denied: ['C:\\Users\\me\\Documents\\taxes.pdf'] })
  })

  it('stays silent for ordinary results, including ones that merely mention permissions', () => {
    expect(sandboxInfo(part({}), { output: 'grep: Permission denied on /var/log/x' })).toBeUndefined()
    expect(buildToolView(part({ result: { output: 'ok', exit_code: 0 } }), '').sandbox).toBeUndefined()
  })
})
