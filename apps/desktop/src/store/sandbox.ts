import { atom } from 'nanostores'

import { getSandboxStatus, updateSandboxPolicy } from '@/api/sandbox'
import type { SandboxStatus } from '@/types/hermes'

// The Windows sandbox (MXC) verdict for the profile in view. The backend's status route is
// authoritative; this atom is a cache of it so the composer's sandbox pill can paint without a
// request per render. Refreshed at the seams the status snapshot already crosses (boot, window
// return) and published by the Sandbox panel after each write, so a toggle shows up at once.
export const $sandboxStatus = atom<SandboxStatus | null>(null)

export function publishSandboxStatus(status: SandboxStatus): void {
  $sandboxStatus.set(status)
}

/** Flip the sandbox for the profile in view; the returned verdict is published for every surface. */
export async function toggleSandbox(): Promise<SandboxStatus | null> {
  const current = $sandboxStatus.get()

  if (!current) {
    return null
  }

  const next = await updateSandboxPolicy({ enabled: !current.enabled })
  $sandboxStatus.set(next)

  return next
}

/** Re-read the verdict; failures leave the last known value in place and never throw. */
export async function refreshSandboxStatus(): Promise<SandboxStatus | null> {
  try {
    const status = await getSandboxStatus()
    $sandboxStatus.set(status)

    return status
  } catch {
    return $sandboxStatus.get()
  }
}
