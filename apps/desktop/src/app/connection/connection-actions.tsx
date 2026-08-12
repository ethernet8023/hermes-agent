// app/connection/connection-actions.tsx — the action row a mode panel ends
// with.
//
// Whether a mode is ready to commit is MODE state (a remote needs a passing
// test, an ssh host needs to be filled in), while what committing means is
// SURFACE state (Settings saves and soft-rehomes; first-run resumes the
// startup gate). So the panel renders this row and decides the gating, and
// the surface supplies the functions behind it.
//
// A mode that commits from inside its own body instead — cloud applies the
// moment an agent is picked — simply does not render this.

import { Button } from '@/components/ui/button'
import { Loader2 } from '@/lib/icons'

import type { ConnectionCommit, ConnectionCopy } from './types'

export interface ConnectionTestAction {
  label: string
  run: () => void
  busy: boolean
  canRun: boolean
}

export interface ConnectionActionsProps {
  commit: ConnectionCommit
  copy: ConnectionCopy
  /** Mode-owned gating: is the current draft committable? */
  canApply: boolean
  /** Env override, or any surface-level reason to freeze the row. */
  disabled?: boolean
  /** Omitted by modes with nothing to probe. */
  test?: ConnectionTestAction
  /** The label on the primary action ("Apply and reconnect" at first run). */
  applyLabel?: string
}

export function ConnectionActions({
  applyLabel,
  canApply,
  commit,
  copy,
  disabled = false,
  test
}: ConnectionActionsProps) {
  return (
    <div className="mt-6 flex flex-wrap items-center justify-end gap-4">
      {test ? (
        <Button
          className="mr-auto"
          disabled={disabled || test.busy || !test.canRun}
          onClick={test.run}
          size="sm"
          variant="text"
        >
          {test.busy ? <Loader2 className="animate-spin" /> : null}
          {test.label}
        </Button>
      ) : null}

      {commit.save ? (
        <Button disabled={disabled || commit.busy} onClick={() => void commit.save?.()} size="sm" variant="textStrong">
          {copy.saveForRestart}
        </Button>
      ) : null}

      <Button disabled={disabled || commit.busy || !canApply} onClick={() => void commit.apply()} size="sm">
        {commit.busy ? <Loader2 className="animate-spin" /> : null}
        {applyLabel ?? copy.saveAndReconnect}
      </Button>
    </div>
  )
}
