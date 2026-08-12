// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type {
  DesktopBackendAvailability,
  DesktopBootstrapEvent,
  DesktopBootstrapState,
  DesktopConnectionProbeResult
} from '@/global'

import { DesktopInstallOverlay } from './desktop-install-overlay'

function bootstrapState(overrides: Partial<DesktopBootstrapState> = {}): DesktopBootstrapState {
  return {
    active: false,
    manifest: null,
    stages: {},
    error: null,
    log: [],
    startedAt: null,
    completedAt: null,
    setupChoice: null,
    unsupportedPlatform: null,
    ...overrides
  }
}

interface MockOptions {
  /** Mode availability the electron registry reports. Omitted = IPC absent. */
  backends?: DesktopBackendAvailability[]
}

function installDesktopMock(state: DesktopBootstrapState, options: MockOptions = {}) {
  const bootstrapListeners = new Set<(event: DesktopBootstrapEvent) => void>()

  const desktop = {
    getBootstrapState: vi.fn().mockResolvedValue(state),
    onBootstrapEvent: vi.fn((listener: (event: DesktopBootstrapEvent) => void) => {
      bootstrapListeners.add(listener)

      return () => bootstrapListeners.delete(listener)
    }),
    getBackendAvailability: options.backends ? vi.fn().mockResolvedValue(options.backends) : undefined,
    continueBootstrapLocal: vi.fn().mockResolvedValue({ ok: true }),
    probeConnectionConfig: vi.fn(),
    testConnectionConfig: vi.fn(),
    applyConnectionConfig: vi.fn(),
    oauthLoginConnectionConfig: vi.fn(),
    sshConfigHosts: vi.fn().mockResolvedValue({ hosts: [] }),
    sshResolveHost: vi.fn(),
    cloud: {
      status: vi.fn().mockResolvedValue({ signedIn: false }),
      login: vi.fn(),
      logout: vi.fn(),
      discover: vi.fn(),
      agentSignIn: vi.fn()
    },
    openExternal: vi.fn(),
    emitBootstrapEvent: (event: DesktopBootstrapEvent) => {
      for (const listener of bootstrapListeners) {
        listener(event)
      }
    }
  }

  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: desktop
  })

  return desktop
}

const SETUP_CHOICE = { platform: 'linux', activeRoot: '/home/me/.hermes/hermes-agent' }

// Resolve the instant a node commits, via MutationObserver rather than
// waitFor's polling timer. findBy* only settles on a timer tick, by which
// point React has already drained its passive effects — that hides any bug
// living in the window between paint and effect.
function whenPresent(text: string): Promise<HTMLElement> {
  return new Promise(resolve => {
    const existing = screen.queryByText(text)

    if (existing) {
      resolve(existing)

      return
    }

    const observer = new MutationObserver(() => {
      const node = screen.queryByText(text)

      if (node) {
        observer.disconnect()
        resolve(node)
      }
    })

    observer.observe(document.body, { childList: true, subtree: true, characterData: true })
  })
}

/** Drill into a mode's card from the first-run grid, once it has painted. */
async function openMode(title: string): Promise<void> {
  fireEvent.click(await whenPresent(title))
}

/** Fill the remote URL and let the debounced auth probe settle. */
async function enterRemoteUrl(url = 'https://gateway.example.com/hermes'): Promise<void> {
  fireEvent.change(await screen.findByPlaceholderText('https://gateway.example.com/hermes'), { target: { value: url } })

  await act(async () => {
    await new Promise(resolve => setTimeout(resolve, 550))
  })
}

beforeEach(() => {
  vi.restoreAllMocks()
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  Reflect.deleteProperty(window, 'hermesDesktop')
})

describe('DesktopInstallOverlay first-run setup', () => {
  it('offers every connection mode without installer progress', async () => {
    installDesktopMock(bootstrapState({ setupChoice: SETUP_CHOICE }))

    render(<DesktopInstallOverlay />)

    expect(await screen.findByText('Set up Hermes Desktop')).toBeTruthy()
    expect(screen.getByText('Local gateway')).toBeTruthy()
    expect(screen.getByText('Hermes Cloud')).toBeTruthy()
    expect(screen.getByText('Remote gateway')).toBeTruthy()
    expect(screen.getByText('Connect via SSH')).toBeTruthy()
    expect(screen.queryByText(/steps complete/i)).toBeNull()
    expect(screen.queryByText(/Fetching installer manifest/i)).toBeNull()
  })

  it('continues local bootstrap only when the local mode is applied', async () => {
    const desktop = installDesktopMock(bootstrapState({ setupChoice: SETUP_CHOICE }))

    render(<DesktopInstallOverlay />)

    // Opening the card must not start anything: the commit is the panel's
    // action, so a user can look at local and still back out.
    await openMode('Local gateway')
    expect(desktop.continueBootstrapLocal).not.toHaveBeenCalled()

    fireEvent.click(screen.getByText('Install Hermes locally').closest('button') as HTMLButtonElement)
    expect(desktop.continueBootstrapLocal).toHaveBeenCalledTimes(1)
    // Local installs through the bootstrap gate, never through a connection
    // apply — an apply would take the teardown path and hang the gated start.
    expect(desktop.applyConnectionConfig).not.toHaveBeenCalled()

    act(() => {
      desktop.emitBootstrapEvent({ type: 'manifest', protocolVersion: 1, stages: [] })
    })

    await waitFor(() => expect(screen.queryByText('Install Hermes locally')).toBeNull())
    expect(screen.getByText(/Fetching installer manifest/i)).toBeTruthy()
  })

  it('surfaces a recoverable error when the local-bootstrap bridge is unavailable', async () => {
    const desktop = installDesktopMock(bootstrapState({ setupChoice: SETUP_CHOICE }))

    desktop.continueBootstrapLocal = undefined as never
    render(<DesktopInstallOverlay />)

    await openMode('Local gateway')
    const install = screen.getByText('Install Hermes locally').closest('button') as HTMLButtonElement
    fireEvent.click(install)

    expect(
      await screen.findByText('Local installation could not start. Restart Hermes Desktop and try again.')
    ).toBeTruthy()
    expect(install.disabled).toBe(false)
  })

  it('returns to the mode grid from a mode panel', async () => {
    installDesktopMock(bootstrapState({ setupChoice: SETUP_CHOICE }))

    render(<DesktopInstallOverlay />)

    await openMode('Remote gateway')
    expect(await screen.findByText('Remote URL')).toBeTruthy()

    fireEvent.click(screen.getByText('Back'))

    expect(await screen.findByText('Set up Hermes Desktop')).toBeTruthy()
    expect(screen.getByText('Local gateway')).toBeTruthy()
    expect(screen.getByText('Connect via SSH')).toBeTruthy()
  })

  it('keeps a typed remote URL when the user visits another mode and returns', async () => {
    installDesktopMock(bootstrapState({ setupChoice: SETUP_CHOICE }))

    render(<DesktopInstallOverlay />)

    await openMode('Remote gateway')
    fireEvent.change(await screen.findByPlaceholderText('https://gateway.example.com/hermes'), {
      target: { value: 'https://half-typed.example' }
    })

    fireEvent.click(screen.getByText('Back'))
    await openMode('Connect via SSH')
    expect(await screen.findByText('Host')).toBeTruthy()

    fireEvent.click(screen.getByText('Back'))
    await openMode('Remote gateway')

    // Drafts are per mode and outlive the panel, so the half-typed URL is
    // still there rather than reset by the detour.
    expect((await screen.findByPlaceholderText('https://gateway.example.com/hermes')).getAttribute('value')).toBe(
      'https://half-typed.example'
    )
  })

  it('requires a successful token connection test before applying remote config', async () => {
    const desktop = installDesktopMock(bootstrapState({ setupChoice: SETUP_CHOICE }))

    desktop.probeConnectionConfig.mockResolvedValue({
      authMode: 'token',
      baseUrl: 'https://gateway.example.com/hermes',
      error: null,
      providers: [],
      reachable: true,
      version: '0.17.0'
    })
    desktop.testConnectionConfig.mockResolvedValue({
      baseUrl: 'https://gateway.example.com/hermes',
      ok: true,
      version: '0.17.0'
    })
    desktop.applyConnectionConfig.mockImplementation(async () => {
      desktop.emitBootstrapEvent({ type: 'dismissed' })

      return { mode: 'remote' }
    })

    render(<DesktopInstallOverlay />)

    await openMode('Remote gateway')
    await enterRemoteUrl()

    const apply = screen.getByText('Apply and reconnect').closest('button') as HTMLButtonElement
    expect(apply.disabled).toBe(true)

    fireEvent.change(await screen.findByPlaceholderText('Paste session token'), {
      target: { value: 'session-secret' }
    })
    fireEvent.click(screen.getByText('Test remote'))

    await waitFor(() => {
      expect(desktop.testConnectionConfig).toHaveBeenCalledWith({
        mode: 'remote',
        remoteAuthMode: 'token',
        remoteToken: 'session-secret',
        remoteUrl: 'https://gateway.example.com/hermes'
      })
    })

    await screen.findByText('Connected to https://gateway.example.com/hermes · Hermes 0.17.0')
    expect(apply.disabled).toBe(false)

    fireEvent.click(apply)

    await waitFor(() => {
      expect(desktop.applyConnectionConfig).toHaveBeenCalledWith({
        mode: 'remote',
        profile: undefined,
        remoteAuthMode: 'token',
        remoteToken: 'session-secret',
        remoteUrl: 'https://gateway.example.com/hermes'
      })
    })
    await waitFor(() => expect(screen.queryByText('Remote URL')).toBeNull())
  })

  it('ignores a completed probe after the gateway URL becomes invalid', async () => {
    const desktop = installDesktopMock(bootstrapState({ setupChoice: SETUP_CHOICE }))

    let resolveProbe: ((result: DesktopConnectionProbeResult) => void) | undefined

    const pendingProbe = new Promise<DesktopConnectionProbeResult>(resolve => {
      resolveProbe = resolve
    })

    desktop.probeConnectionConfig.mockReturnValue(pendingProbe)

    render(<DesktopInstallOverlay />)

    await openMode('Remote gateway')
    const urlInput = await screen.findByPlaceholderText('https://gateway.example.com/hermes')
    fireEvent.change(urlInput, { target: { value: 'https://gateway.example.com/hermes' } })

    await act(async () => {
      await new Promise(resolve => setTimeout(resolve, 550))
    })
    expect(desktop.probeConnectionConfig).toHaveBeenCalledTimes(1)

    fireEvent.change(urlInput, { target: { value: 'not-a-url' } })
    await act(async () => {
      resolveProbe?.({
        authMode: 'token',
        baseUrl: 'https://gateway.example.com/hermes',
        error: null,
        providers: [],
        reachable: true,
        version: '0.17.0'
      })
      await pendingProbe
    })

    expect(screen.queryByPlaceholderText('Paste session token')).toBeNull()
    expect((screen.getByText('Test remote').closest('button') as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByText('Apply and reconnect').closest('button') as HTMLButtonElement).disabled).toBe(true)
  })

  it('does not enable Apply when credentials change during a connection test', async () => {
    const desktop = installDesktopMock(bootstrapState({ setupChoice: SETUP_CHOICE }))

    desktop.probeConnectionConfig.mockResolvedValue({
      authMode: 'token',
      baseUrl: 'https://gateway.example.com/hermes',
      error: null,
      providers: [],
      reachable: true,
      version: '0.17.0'
    })

    let resolveTest: ((result: { baseUrl: string; ok: boolean; version: string }) => void) | undefined

    const pendingTest = new Promise<{ baseUrl: string; ok: boolean; version: string }>(resolve => {
      resolveTest = resolve
    })

    desktop.testConnectionConfig.mockReturnValue(pendingTest)

    render(<DesktopInstallOverlay />)

    await openMode('Remote gateway')
    await enterRemoteUrl()

    const tokenInput = await screen.findByPlaceholderText('Paste session token')
    const apply = screen.getByText('Apply and reconnect').closest('button') as HTMLButtonElement

    fireEvent.change(tokenInput, { target: { value: 'token-a' } })
    fireEvent.click(screen.getByText('Test remote'))
    await waitFor(() => expect(desktop.testConnectionConfig).toHaveBeenCalledTimes(1))

    fireEvent.change(tokenInput, { target: { value: 'token-b' } })

    await act(async () => {
      resolveTest?.({ baseUrl: 'https://gateway.example.com/hermes', ok: true, version: '0.17.0' })
      await pendingTest
    })

    expect(screen.queryByText('Connected to https://gateway.example.com/hermes · Hermes 0.17.0')).toBeNull()
    expect(apply.disabled).toBe(true)
  })

  it('restores remote apply controls when applying the tested connection fails', async () => {
    const desktop = installDesktopMock(bootstrapState({ setupChoice: SETUP_CHOICE }))

    desktop.probeConnectionConfig.mockResolvedValue({
      authMode: 'token',
      baseUrl: 'https://gateway.example.com/hermes',
      error: null,
      providers: [],
      reachable: true,
      version: '0.17.0'
    })
    desktop.testConnectionConfig.mockResolvedValue({
      baseUrl: 'https://gateway.example.com/hermes',
      ok: true,
      version: '0.17.0'
    })
    desktop.applyConnectionConfig.mockRejectedValue(new Error('remote apply failed'))

    render(<DesktopInstallOverlay />)

    await openMode('Remote gateway')
    await enterRemoteUrl()

    fireEvent.change(await screen.findByPlaceholderText('Paste session token'), {
      target: { value: 'session-secret' }
    })
    fireEvent.click(screen.getByText('Test remote'))
    await screen.findByText('Connected to https://gateway.example.com/hermes · Hermes 0.17.0')

    const apply = screen.getByText('Apply and reconnect').closest('button') as HTMLButtonElement
    fireEvent.click(apply)

    expect(await screen.findByText('remote apply failed')).toBeTruthy()
    expect(apply.disabled).toBe(false)
    expect(screen.getByText('Remote URL')).toBeTruthy()
  })

  it('signs in, tests, and applies a password-style remote gateway', async () => {
    const desktop = installDesktopMock(bootstrapState({ setupChoice: SETUP_CHOICE }))

    desktop.probeConnectionConfig.mockResolvedValue({
      authMode: 'oauth',
      baseUrl: 'https://gateway.example.com/hermes',
      error: null,
      providers: [{ displayName: 'Username & Password', name: 'password', supportsPassword: true }],
      reachable: true,
      version: '0.17.0'
    })
    desktop.oauthLoginConnectionConfig.mockResolvedValue({
      baseUrl: 'https://gateway.example.com/hermes',
      connected: true,
      ok: true
    })
    desktop.testConnectionConfig.mockResolvedValue({
      baseUrl: 'https://gateway.example.com/hermes',
      ok: true,
      version: null
    })
    desktop.applyConnectionConfig.mockResolvedValue({ mode: 'remote' })

    render(<DesktopInstallOverlay />)

    await openMode('Remote gateway')
    await enterRemoteUrl()

    expect(screen.queryByText('Sign in with Username & Password')).toBeNull()
    fireEvent.click(await screen.findByText('Sign in'))

    await waitFor(() => {
      expect(desktop.oauthLoginConnectionConfig).toHaveBeenCalledWith('https://gateway.example.com/hermes')
    })

    // First run persists nothing before Apply, so backing out of a sign-in
    // must leave no saved config behind.
    expect(desktop.applyConnectionConfig).not.toHaveBeenCalled()

    fireEvent.click(screen.getByText('Test remote'))

    await waitFor(() => {
      expect(desktop.testConnectionConfig).toHaveBeenCalledWith({
        mode: 'remote',
        remoteAuthMode: 'oauth',
        remoteToken: undefined,
        remoteUrl: 'https://gateway.example.com/hermes'
      })
    })

    await screen.findByText('Connected to https://gateway.example.com/hermes')
    const apply = screen.getByText('Apply and reconnect').closest('button') as HTMLButtonElement
    expect(apply.disabled).toBe(false)
    fireEvent.click(apply)

    await waitFor(() => {
      expect(desktop.applyConnectionConfig).toHaveBeenCalledWith({
        mode: 'remote',
        profile: undefined,
        remoteAuthMode: 'oauth',
        remoteToken: undefined,
        remoteUrl: 'https://gateway.example.com/hermes'
      })
    })
  })

  it('applies an ssh connection from first-run setup', async () => {
    const desktop = installDesktopMock(bootstrapState({ setupChoice: SETUP_CHOICE }))

    desktop.applyConnectionConfig.mockResolvedValue({ mode: 'ssh' })

    render(<DesktopInstallOverlay />)

    await openMode('Connect via SSH')

    const apply = screen.getByText('Apply and reconnect').closest('button') as HTMLButtonElement

    // No host yet: there is nothing to connect to.
    expect(apply.disabled).toBe(true)

    fireEvent.change(await screen.findByRole('textbox', { name: 'Host' }), { target: { value: 'build-box' } })

    await waitFor(() => expect(apply.disabled).toBe(false))
    fireEvent.click(apply)

    // ssh commits through the same connection apply as remote and cloud —
    // all three resume the gated startup rather than installing anything.
    await waitFor(() => {
      expect(desktop.applyConnectionConfig).toHaveBeenCalledWith(
        expect.objectContaining({ mode: 'ssh', sshHost: 'build-box' })
      )
    })
    expect(desktop.continueBootstrapLocal).not.toHaveBeenCalled()
  })

  it('offers remote connection from the unsupported packaged install screen', async () => {
    const desktop = installDesktopMock(
      bootstrapState({
        unsupportedPlatform: {
          platform: 'darwin',
          activeRoot: '/Users/me/.hermes/hermes-agent',
          installCommand: 'curl -fsSL https://example.invalid/install.sh | sh',
          docsUrl: 'https://example.invalid/docs'
        }
      })
    )

    render(<DesktopInstallOverlay />)

    expect(await screen.findByText('Hermes needs a one-time install')).toBeTruthy()

    fireEvent.click(screen.getByText('Connect existing'))

    await openMode('Remote gateway')

    desktop.probeConnectionConfig.mockResolvedValue({
      authMode: 'token',
      baseUrl: 'https://gateway.example.com/hermes',
      error: null,
      providers: [],
      reachable: true,
      version: '0.17.0'
    })
    desktop.testConnectionConfig.mockResolvedValue({
      baseUrl: 'https://gateway.example.com/hermes',
      ok: true,
      version: '0.17.0'
    })
    desktop.applyConnectionConfig.mockImplementation(async () => {
      desktop.emitBootstrapEvent({ type: 'dismissed' })

      return { mode: 'remote' }
    })

    await enterRemoteUrl()

    fireEvent.change(await screen.findByPlaceholderText('Paste session token'), {
      target: { value: 'session-secret' }
    })
    fireEvent.click(screen.getByText('Test remote'))
    await screen.findByText('Connected to https://gateway.example.com/hermes · Hermes 0.17.0')
    fireEvent.click(screen.getByText('Apply and reconnect'))

    await waitFor(() => expect(screen.queryByText('Remote URL')).toBeNull())
    expect(screen.queryByText('Hermes needs a one-time install')).toBeNull()
  })
})

// The reported bug: a light artifact ships no local runtime, and first-run
// used to answer that by dropping the mode choice entirely and showing a
// remote-only form — so cloud and ssh, which a light build supports, were
// unreachable at setup.
describe('DesktopInstallOverlay first-run setup on a light artifact', () => {
  const LIGHT_BACKENDS: DesktopBackendAvailability[] = [
    { mode: 'local', available: false, reason: 'light-artifact' },
    { mode: 'remote', available: true },
    { mode: 'cloud', available: true },
    { mode: 'ssh', available: true }
  ]

  it('disables only the local card and states why', async () => {
    installDesktopMock(bootstrapState({ setupChoice: SETUP_CHOICE }), { backends: LIGHT_BACKENDS })

    render(<DesktopInstallOverlay />)

    const local = (await screen.findByText('Local gateway')).closest('button') as HTMLButtonElement
    expect(local.disabled).toBe(true)
    expect(
      screen.getByText('This Hermes build has no local backend — connect to a remote gateway instead.')
    ).toBeTruthy()

    // Every mode this build CAN use stays reachable — the regression that
    // started this work was cloud and ssh disappearing here.
    for (const title of ['Hermes Cloud', 'Remote gateway', 'Connect via SSH']) {
      expect((screen.getByText(title).closest('button') as HTMLButtonElement).disabled).toBe(false)
    }
  })

  it('reaches the ssh panel and back on a build with no local runtime', async () => {
    installDesktopMock(bootstrapState({ setupChoice: SETUP_CHOICE }), { backends: LIGHT_BACKENDS })

    render(<DesktopInstallOverlay />)

    await openMode('Connect via SSH')
    expect(await screen.findByText('Host')).toBeTruthy()

    fireEvent.click(screen.getByText('Back'))
    expect(await screen.findByText('Set up Hermes Desktop')).toBeTruthy()
  })

  it('does not start a local bootstrap it cannot run', async () => {
    const desktop = installDesktopMock(bootstrapState({ setupChoice: SETUP_CHOICE }), { backends: LIGHT_BACKENDS })

    render(<DesktopInstallOverlay />)

    fireEvent.click(await screen.findByText('Local gateway'))

    // The card is disabled, so the click resolves to nothing: no panel, and
    // above all no installer on an artifact that ships no runtime.
    expect(screen.queryByText('Install Hermes locally')).toBeNull()
    expect(desktop.continueBootstrapLocal).not.toHaveBeenCalled()
  })
})
