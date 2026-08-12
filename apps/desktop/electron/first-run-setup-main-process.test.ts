import assert from 'node:assert/strict'

import { test, vi } from 'vitest'

import { applyConnectionChange } from './connection-apply'
import { createFirstRunSetupGate } from './first-run-setup-gate'
import { runPrimaryBackendStartup } from './primary-backend-startup'
import { rehomePrimaryConnection } from './primary-connection-rehome'

test('a first-run bootstrap-needed remote apply connects without ensuring or bootstrapping locally', async () => {
  const gate = createFirstRunSetupGate({ stuckAfterMs: 0 })

  const bootstrapBackend = {
    activeRoot: '/tmp/hermes-home/hermes-agent',
    kind: 'bootstrap-needed',
    platform: 'linux'
  }

  const candidateRemote = {
    authMode: 'token',
    baseUrl: 'https://gateway.example.com/hermes',
    source: 'settings',
    token: 'secret',
    wsUrl: 'wss://gateway.example.com/hermes/api/ws?token=secret'
  }

  let savedRemote: typeof candidateRemote | null = null

  const resolveRemote = vi.fn(async () => savedRemote)
  const connectRemote = vi.fn(async remote => ({ ...remote, mode: 'remote' as const }))
  const runBootstrap = vi.fn()

  const ensureLocalRuntime = vi.fn(async backend => {
    await runBootstrap()

    return { ...backend, command: 'hermes' }
  })

  const teardownPrimaryBackend = vi.fn(async () => {})
  const cancelSshBootstrap = vi.fn(async () => {})
  const teardownSsh = vi.fn(async () => {})
  const clearLocalBootstrapFailure = vi.fn()
  const notifyConnectionApplied = vi.fn()
  const waitForLocalStart = vi.fn(async () => {})
  const prepareLocalBackend = vi.fn(async () => bootstrapBackend)

  const pendingConnection = runPrimaryBackendStartup({
    bootProgress: vi.fn(async () => {}),
    connectRemote,
    ensureLocalRuntime,
    localMode: {
      availability: { mode: 'local' as const, available: true as const },
      setupBackend: bootstrapBackend
    },
    log: vi.fn(),
    prepareLocalBackend,
    resolveRemote,
    waitForDecision: gate.wait,
    waitForLocalStart
  })

  await vi.waitFor(() => assert.equal(gate.hasWaiter(), true))

  // Mirrors the IPC handler's production ordering: persist the tested config,
  // then re-home. The pending start must re-resolve this saved value.
  savedRemote = candidateRemote

  await applyConnectionChange({
    cancelAndWait: cancelSshBootstrap,
    isPrimary: true,
    rehomePrimary: () =>
      rehomePrimaryConnection({
        clearLocalBootstrapFailure,
        mode: 'remote',
        notifyConnectionApplied,
        resumeFirstRunRemote: gate.abandonForRemoteApply,
        teardownPrimaryBackend
      }),
    scope: '',
    sendApplied: notifyConnectionApplied,
    stopPool: vi.fn(),
    teardownPrimary: teardownPrimaryBackend,
    teardownSsh
  })

  assert.deepEqual(await pendingConnection, {
    kind: 'remote',
    connection: { ...candidateRemote, mode: 'remote' }
  })
  assert.deepEqual(resolveRemote.mock.calls, [[], []])
  assert.deepEqual(connectRemote.mock.calls, [[candidateRemote]])
  assert.deepEqual(waitForLocalStart.mock.calls, [[]])
  assert.deepEqual(prepareLocalBackend.mock.calls, [[]])
  assert.equal(ensureLocalRuntime.mock.calls.length, 0)
  assert.equal(runBootstrap.mock.calls.length, 0)
  assert.deepEqual(cancelSshBootstrap.mock.calls, [['']])
  assert.deepEqual(teardownSsh.mock.calls, [['']])
  assert.equal(teardownPrimaryBackend.mock.calls.length, 0)
  assert.equal(clearLocalBootstrapFailure.mock.calls.length, 1)
  assert.equal(notifyConnectionApplied.mock.calls.length, 0)
})

test('a primary apply without an active first-run gate tears down before reconnect notification', async () => {
  const order: string[] = []
  const clearLocalBootstrapFailure = vi.fn(() => order.push('clear-failure'))

  const teardownPrimaryBackend = vi.fn(async () => {
    order.push('teardown')
  })

  const notifyConnectionApplied = vi.fn(() => order.push('notify'))

  assert.deepEqual(
    await rehomePrimaryConnection({
      clearLocalBootstrapFailure,
      mode: 'remote',
      notifyConnectionApplied,
      resumeFirstRunRemote: () => false,
      teardownPrimaryBackend
    }),
    { resumedFirstRunRemote: false }
  )
  assert.deepEqual(teardownPrimaryBackend.mock.calls, [[{ soft: true }]])
  assert.deepEqual(order, ['clear-failure', 'teardown', 'notify'])
})

// Cloud and ssh persist the same remote-shaped block and dial the backend
// through the same path as remote, so a first-run apply of ANY non-local mode
// has to resume the gated start. Keying the resume on 'remote' alone parked
// cloud/ssh first-run applies in waitForDecision forever.
for (const mode of ['remote', 'cloud', 'ssh']) {
  test(`a first-run ${mode} apply resumes the gated start instead of tearing it down`, async () => {
    const gate = createFirstRunSetupGate({ stuckAfterMs: 0 })

    const savedRemote = {
      authMode: 'token',
      baseUrl: 'https://gateway.example.com/hermes',
      source: 'settings',
      token: 'secret'
    }

    const connectRemote = vi.fn(async remote => ({ ...remote, mode: 'remote' as const }))
    const ensureLocalRuntime = vi.fn(async backend => ({ ...backend, command: 'hermes' }))
    const teardownPrimaryBackend = vi.fn(async () => {})
    const notifyConnectionApplied = vi.fn()
    let resolved = false

    const pendingConnection = runPrimaryBackendStartup({
      bootProgress: vi.fn(async () => {}),
      connectRemote,
      ensureLocalRuntime,
      localMode: {
        availability: { mode: 'local' as const, available: true as const },
        setupBackend: { activeRoot: '/tmp/hermes-home', kind: 'bootstrap-needed', platform: 'linux' }
      },
      log: vi.fn(),
      prepareLocalBackend: vi.fn(async () => ({
        activeRoot: '/tmp/hermes-home',
        kind: 'bootstrap-needed',
        platform: 'linux'
      })),
      // Nothing is saved until the apply writes it — mirrors production ordering.
      resolveRemote: vi.fn(async () => (resolved ? savedRemote : null)),
      waitForDecision: gate.wait,
      waitForLocalStart: vi.fn(async () => {})
    })

    await vi.waitFor(() => assert.equal(gate.hasWaiter(), true))
    resolved = true

    const rehomed = await rehomePrimaryConnection({
      clearLocalBootstrapFailure: vi.fn(),
      mode,
      notifyConnectionApplied,
      resumeFirstRunRemote: gate.abandonForRemoteApply,
      teardownPrimaryBackend
    })

    assert.equal(rehomed.resumedFirstRunRemote, true)
    assert.deepEqual(await pendingConnection, {
      kind: 'remote',
      connection: { ...savedRemote, mode: 'remote' }
    })
    // The resumed start owns the connection: no teardown, no reconnect nudge,
    // and the local runtime this artifact may not even have is never touched.
    assert.equal(teardownPrimaryBackend.mock.calls.length, 0)
    assert.equal(notifyConnectionApplied.mock.calls.length, 0)
    assert.equal(ensureLocalRuntime.mock.calls.length, 0)
  })
}

test('a local apply leaves the first-run gate alone and takes the teardown path', async () => {
  const gate = createFirstRunSetupGate({ stuckAfterMs: 0 })
  const resumeFirstRunRemote = vi.fn(() => gate.abandonForRemoteApply())
  const teardownPrimaryBackend = vi.fn(async () => {})
  const notifyConnectionApplied = vi.fn()

  assert.deepEqual(
    await rehomePrimaryConnection({
      clearLocalBootstrapFailure: vi.fn(),
      mode: 'local',
      notifyConnectionApplied,
      resumeFirstRunRemote,
      teardownPrimaryBackend
    }),
    { resumedFirstRunRemote: false }
  )
  // Local is the one mode the gate must NOT be resumed for: it is settled by
  // the setup surface's own continue-local decision, not by an apply.
  assert.equal(resumeFirstRunRemote.mock.calls.length, 0)
  assert.deepEqual(teardownPrimaryBackend.mock.calls, [[{ soft: true }]])
  assert.equal(notifyConnectionApplied.mock.calls.length, 1)
})
