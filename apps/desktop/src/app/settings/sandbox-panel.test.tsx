// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { en } from '@/i18n/en'
import { $confirmRequest, settleConfirm } from '@/store/confirm'
import { publishSandboxStatus, sandboxState } from '@/store/sandbox'
const owner = { connectionId: 'remote-b', profile: 'beta' }
import type { SandboxStatus } from '@/types/hermes'

import { SandboxPanel } from './sandbox-panel'

const mocks = vi.hoisted(() => ({
  getStatus: vi.fn(),
  notifyError: vi.fn(),
  pickFolder: vi.fn(),
  prepare: vi.fn(),
  preview: vi.fn(),
  grant: vi.fn(),
  update: vi.fn()
}))

vi.mock('@/api/sandbox', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getSandboxStatus: (...args: unknown[]) => mocks.getStatus(...args),
  getSandboxGrantTarget: (...args: unknown[]) => mocks.preview(...args),
  grantSandboxPath: (...args: unknown[]) => mocks.grant(...args),
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
    sandboxState(owner).set({ status: null, confirmed: false, busy: false, error: null })
    mocks.getStatus.mockResolvedValue(status())
    mocks.preview.mockImplementation(async path => ({ target: path, recursive: true }))
    mocks.grant.mockImplementation(async path => ({ ...status({ enabled: true }), granted: path, mode: 'readwrite' }))
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

  it('targets the explicit Settings owner and subscribes to shared changes', async () => {
    render(<SandboxPanel owner={owner} />)
    const toggle = await screen.findByRole('switch', { name: en.settings.sandbox.toggleLabel })
    expect(mocks.getStatus).toHaveBeenCalledWith(owner, expect.anything())
    await act(async () => {
      publishSandboxStatus(status({ enabled: true }), owner)
    })
    expect(toggle.getAttribute('aria-checked')).toBe('true')
  })

  it('shows an unknown status with a recovery read after the first request fails', async () => {
    mocks.getStatus.mockRejectedValueOnce(new Error('connection lost'))
    render(<SandboxPanel owner={owner} />)
    expect(await screen.findByText(en.settings.sandbox.statusUnknown)).toBeTruthy()
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: en.settings.sandbox.recheck }))
    })
    expect(await screen.findByRole('switch', { name: en.settings.sandbox.toggleLabel })).toBeTruthy()
  })

  it('allows disabling an enabled unavailable backend', async () => {
    mocks.getStatus.mockResolvedValue(status({ enabled: true, available: false, reason: 'missing kit' }))
    render(<SandboxPanel owner={owner} />)
    const toggle = await screen.findByRole('switch', { name: en.settings.sandbox.toggleLabel })
    expect(toggle).toHaveProperty('disabled', false)
    await act(async () => {
      fireEvent.click(toggle)
    })
    expect(mocks.update).toHaveBeenCalledWith({ enabled: false }, owner)
  })

  it('renders nothing where the platform can never run MXC', async () => {
    mocks.getStatus.mockResolvedValue(status({ platform_supported: false, available: false, reason: 'not Windows' }))
    const { container } = render(<SandboxPanel owner={owner} />)

    await waitFor(() => expect(screen.queryByTestId('sandbox-panel-loading')).toBeNull())
    expect(container.innerHTML).toBe('')
  })

  it('explains in plain language why the sandbox is unavailable and keeps the toggle off', async () => {
    mocks.getStatus.mockResolvedValue(status({ available: false, reason: 'wxc-exec.exe (the MXC kit) was not found.' }))
    render(<SandboxPanel owner={owner} />)

    const reason = await screen.findByTestId('sandbox-reason')
    expect(reason.textContent).toContain('wxc-exec.exe (the MXC kit) was not found.')
    expect(screen.getByRole('switch', { name: en.settings.sandbox.toggleLabel })).toHaveProperty('disabled', true)
  })

  it('lets the user opt in when only the shell still needs provisioning', async () => {
    mocks.getStatus.mockResolvedValue(
      status({ available: false, shell_missing: true, reason: 'shell not installed yet' })
    )
    render(<SandboxPanel owner={owner} />)

    const toggle = await screen.findByRole('switch', { name: en.settings.sandbox.toggleLabel })
    expect(toggle).toHaveProperty('disabled', false)
    expect(screen.getByTestId('sandbox-reason').textContent).toContain('shell not installed yet')
    expect(screen.getByText(en.settings.sandbox.shellNote)).toBeTruthy()

    await act(async () => {
      fireEvent.click(toggle)
    })

    expect(mocks.update).toHaveBeenCalledWith({ enabled: true }, owner)
  })

  it('shows the live policy once enabled and writes folder and network changes through the policy route', async () => {
    mocks.getStatus.mockResolvedValue(
      status({ enabled: true, policy: { readwrite_paths: [], readonly_paths: [DOCUMENTS], network: false } })
    )
    render(<SandboxPanel owner={owner} />)

    expect(await screen.findByText(DOCUMENTS)).toBeTruthy()
    expect(screen.getByText(en.settings.sandbox.modeRead)).toBeTruthy()
    expect(mocks.getStatus).toHaveBeenCalledWith(owner, {})

    mocks.pickFolder.mockResolvedValue(PROJECTS)
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: en.settings.sandbox.addReadWrite }))
    })
    expect($confirmRequest.get()?.description).toBe(en.assistant.tool.sandboxRecursiveScope(PROJECTS))
    expect(mocks.grant).not.toHaveBeenCalled()
    await act(async () => settleConfirm(true))
    expect(mocks.grant).toHaveBeenCalledWith(PROJECTS, 'readwrite', owner)
    expect(mocks.update).not.toHaveBeenCalled()

    await act(async () => {
      fireEvent.click(screen.getByRole('switch', { name: en.settings.sandbox.networkLabel }))
    })
    expect(mocks.update).toHaveBeenCalledWith({ network: true }, owner)
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
    render(<SandboxPanel owner={owner} />)

    await screen.findByTestId('sandbox-workspace-rule')
    expect(screen.queryByText(/icacls/)).toBeNull()
    expect(mocks.prepare).not.toHaveBeenCalled()
  })

  it("states the per-conversation rule instead of showing one tab's folder as policy", async () => {
    mocks.getStatus.mockResolvedValue(status({ enabled: true, workspace: `${PROJECTS}\\demo` }))
    render(<SandboxPanel owner={owner} />)

    expect(await screen.findByTestId('sandbox-workspace-rule')).toBeTruthy()
    expect(screen.queryByText(`${PROJECTS}\\demo`)).toBeNull()
  })
})
