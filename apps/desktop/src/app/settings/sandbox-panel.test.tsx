// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { en } from '@/i18n/en'
import type { SandboxStatus } from '@/types/hermes'

import { SandboxPanel } from './sandbox-panel'

const mocks = vi.hoisted(() => ({
  getStatus: vi.fn(),
  notifyError: vi.fn(),
  pickFolder: vi.fn(),
  prepare: vi.fn(),
  update: vi.fn()
}))

vi.mock('@/hermes', () => ({
  getSandboxStatus: (...args: unknown[]) => mocks.getStatus(...args),
  prepareSandboxWorkspace: (...args: unknown[]) => mocks.prepare(...args),
  updateSandboxPolicy: (...args: unknown[]) => mocks.update(...args)
}))

vi.mock('@/i18n', () => ({
  useI18n: () => ({ t: en })
}))

vi.mock('@/store/notifications', () => ({
  notify: vi.fn(),
  notifyError: (...args: unknown[]) => mocks.notifyError(...args)
}))

vi.mock('@/store/projects', () => ({
  pickProjectFolder: () => mocks.pickFolder()
}))

const DEMO = 'C:\\Demo'
const DOCUMENTS = 'C:\\Users\\me\\Documents'
const PROJECTS = 'C:\\Users\\me\\Projects'

function status(overrides: Partial<SandboxStatus> = {}): SandboxStatus {
  return {
    platform_supported: true,
    available: true,
    degraded: false,
    reason: null,
    warnings: [],
    tier: 'base-container',
    wxc_exec_path: 'C:\\mxc-kit\\bin\\wxc-exec.exe',
    shell_path: 'C:\\hermes\\bin\\busybox-sh.exe',
    shell_missing: false,
    os_build: '10.0.28120',
    enabled: false,
    policy: { readwrite_paths: [], readonly_paths: [], network: false },
    containers_started: 0,
    workspace: DEMO,
    workspace_ancestors: { ready: true, missing: [], needs_admin: [], admin_command: '' },
    ...overrides
  }
}

describe('SandboxPanel', () => {
  beforeEach(() => {
    mocks.getStatus.mockResolvedValue(status())
    mocks.update.mockImplementation(async (update: Record<string, unknown>) =>
      status({
        enabled: true,
        policy: {
          readwrite_paths: (update.readwrite_paths as string[]) ?? [],
          readonly_paths: (update.readonly_paths as string[]) ?? [],
          network: (update.network as boolean) ?? false
        }
      })
    )
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('renders nothing where the platform can never run MXC', async () => {
    mocks.getStatus.mockResolvedValue(status({ platform_supported: false, available: false, reason: 'not Windows' }))
    const { container } = render(<SandboxPanel />)

    await waitFor(() => expect(screen.queryByTestId('sandbox-panel-loading')).toBeNull())
    expect(container.innerHTML).toBe('')
  })

  it('explains in plain language why the sandbox is unavailable and keeps the toggle off', async () => {
    mocks.getStatus.mockResolvedValue(status({ available: false, reason: 'wxc-exec.exe (the MXC kit) was not found.' }))
    render(<SandboxPanel />)

    const reason = await screen.findByTestId('sandbox-reason')
    expect(reason.textContent).toContain('wxc-exec.exe (the MXC kit) was not found.')
    expect(screen.getByRole('switch', { name: en.settings.sandbox.toggleLabel })).toHaveProperty('disabled', true)
  })

  it('lets the user opt in when only the shell still needs provisioning', async () => {
    mocks.getStatus.mockResolvedValue(status({ available: false, shell_missing: true, reason: 'shell not installed yet' }))
    render(<SandboxPanel />)

    const toggle = await screen.findByRole('switch', { name: en.settings.sandbox.toggleLabel })
    expect(toggle).toHaveProperty('disabled', false)
    expect(screen.queryByTestId('sandbox-reason')).toBeNull()
    expect(screen.getByText(en.settings.sandbox.shellNote)).toBeTruthy()

    await act(async () => {
      fireEvent.click(toggle)
    })

    expect(mocks.update).toHaveBeenCalledWith({ enabled: true })
  })

  it('shows the live policy once enabled and writes folder and network changes through the policy route', async () => {
    mocks.getStatus.mockResolvedValue(
      status({ enabled: true, policy: { readwrite_paths: [], readonly_paths: [DOCUMENTS], network: false } })
    )
    render(<SandboxPanel workspace={DEMO} />)

    expect(await screen.findByText(DOCUMENTS)).toBeTruthy()
    expect(screen.getByText(en.settings.sandbox.modeRead)).toBeTruthy()
    expect(mocks.getStatus).toHaveBeenCalledWith({ workspace: DEMO })

    mocks.pickFolder.mockResolvedValue(PROJECTS)
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: en.settings.sandbox.addReadWrite }))
    })
    expect(mocks.update).toHaveBeenCalledWith({ readwrite_paths: [PROJECTS], readonly_paths: [DOCUMENTS] })

    await act(async () => {
      fireEvent.click(screen.getByRole('switch', { name: en.settings.sandbox.networkLabel }))
    })
    expect(mocks.update).toHaveBeenCalledWith({ network: true })
  })

  it('never renders a per-workspace git preparation step, whatever the ancestors report', async () => {
    // Git under a profile folder needs an administrator-owned ancestor prepared; that is a
    // machine setup step documented in the user guide, not a card in Settings.
    mocks.getStatus.mockResolvedValue(
      status({
        enabled: true,
        workspace_ancestors: {
          ready: false,
          missing: ['C:\\', 'C:\\Users'],
          needs_admin: ['C:\\', 'C:\\Users'],
          admin_command: 'icacls "C:\\" /grant ...'
        }
      })
    )
    render(<SandboxPanel />)

    await screen.findByTestId('sandbox-workspace-rule')
    expect(screen.queryByText(/icacls/)).toBeNull()
    expect(mocks.prepare).not.toHaveBeenCalled()
  })

  it('states the per-conversation rule instead of showing one tab\'s folder as policy', async () => {
    mocks.getStatus.mockResolvedValue(status({ enabled: true, workspace: `${PROJECTS}\\demo` }))
    render(<SandboxPanel />)

    expect(await screen.findByTestId('sandbox-workspace-rule')).toBeTruthy()
    expect(screen.queryByText(`${PROJECTS}\\demo`)).toBeNull()
  })
})
