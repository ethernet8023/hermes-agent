// app/connection/modes/local/panel.tsx — nothing to configure, just commit.
//
// Local has no fields, but it still needs a body: the action row belongs to
// the mode (it decides when a draft is committable) and the drill-down
// surface needs something to render when the local card is chosen. So this
// is a one-line explanation plus the row.
//
// The row's meaning is the surface's: in Settings it saves a local
// connection and soft-rehomes, at first run it starts the installer.

import { ConnectionActions } from '../../connection-actions'
import type { ConnectionConfigPanelProps } from '../../types'

import type { LocalDraft } from './index'

export function LocalPanel({ surface }: ConnectionConfigPanelProps<LocalDraft>) {
  const { commit, copy, envOverride, kind } = surface

  return (
    <div className="mt-5 grid gap-1">
      {/* The card above already states what local mode is, so repeating it
          here would print the same sentence twice. First run is the
          exception: its card says "install locally" while this explains what
          the install actually does. */}
      {kind === 'first-run' ? (
        <p className="text-[length:var(--conversation-caption-font-size)] leading-(--conversation-caption-line-height) text-(--ui-text-tertiary)">
          {copy.localInstallDesc}
        </p>
      ) : null}

      <ConnectionActions
        applyLabel={kind === 'first-run' ? copy.localInstallAction : undefined}
        canApply
        commit={commit}
        copy={copy}
        disabled={envOverride}
      />
    </div>
  )
}
