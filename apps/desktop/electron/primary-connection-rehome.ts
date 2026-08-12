export interface PrimaryConnectionRehomeOptions {
  clearLocalBootstrapFailure: () => void
  mode: string
  notifyConnectionApplied: () => void
  resumeFirstRunRemote: () => boolean
  teardownPrimaryBackend: (options: { soft: boolean }) => Promise<void>
}

// Production seam shared by the connection-config IPC handler and the
// first-run integration test. An apply that resumes the active setup gate must
// keep that connection attempt alive; ordinary mode changes tear the current
// backend down before the renderer is told to reconnect.
//
// Every non-local mode resumes the gate, not just 'remote'. Cloud and ssh
// persist the same remote-shaped block and reach the backend through the same
// dial (backends/remote.ts), so they bypass the local runtime exactly as remote
// does. Keying on 'remote' alone left a first-run cloud/ssh apply parked in
// waitForDecision forever: the config was written and the renderer was told to
// reconnect, but the gated startHermes() was never resumed.
export async function rehomePrimaryConnection({
  clearLocalBootstrapFailure,
  mode,
  notifyConnectionApplied,
  resumeFirstRunRemote,
  teardownPrimaryBackend
}: PrimaryConnectionRehomeOptions): Promise<{ resumedFirstRunRemote: boolean }> {
  let resumedFirstRunRemote = false

  if (mode !== 'local') {
    resumedFirstRunRemote = resumeFirstRunRemote()
    clearLocalBootstrapFailure()
  }

  if (resumedFirstRunRemote) {
    return { resumedFirstRunRemote: true }
  }

  await teardownPrimaryBackend({ soft: true })
  notifyConnectionApplied()

  return { resumedFirstRunRemote: false }
}
