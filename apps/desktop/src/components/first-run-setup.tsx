// components/first-run-setup.tsx — first-run connection setup.
//
// The pre-bootstrap host for the connection modes in app/connection. It
// renders the SAME cards and panels as Settings → Gateway; what differs is
// what committing means and how results are shown:
//
//   - local commits through the bootstrap gate (continueBootstrapLocal),
//     because at first run "use local" means "install the runtime", not
//     "point at the one already running";
//   - every other mode commits through applyConnectionConfig, which resumes
//     the gated startup (electron/primary-connection-rehome);
//   - errors and successes render inline, since there is no toast host yet;
//   - nothing is persisted before Apply, so backing out of an oauth sign-in
//     leaves no trace (no beforeOAuthLogin).
//
// Every mode is offered, availability-gated. A light artifact has no local
// backend, so that card renders disabled with its reason — the other three
// modes (cloud, remote, ssh) stay selectable, which is the whole point:
// before this, a light build offered a remote-only form and nothing else.

import { useState } from 'react'

import {
  connectionCopy,
  type ConnectionMode,
  ConnectionModeCards,
  type ConnectionSurface,
  moduleFor,
  useConnectionDrafts
} from '@/app/connection'
import { BrandMark } from '@/components/brand-mark'
import type { DesktopBackendAvailability } from '@/global'
import { useI18n } from '@/i18n'
import { AlertCircle, Check, ChevronLeft } from '@/lib/icons'

export interface FirstRunSetupProps {
  /** Where the local runtime would be installed, shown as a footnote. */
  activeRoot: string
  /**
   * Mode availability from the electron backend registry. Null when the IPC
   * is missing (an older Electron): every mode is then offered, and an
   * unavailable one fails at connect time instead of being pre-disabled.
   */
  backends: DesktopBackendAvailability[] | null
}

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err || 'Unknown error')
}

export function FirstRunSetup({ activeRoot, backends }: FirstRunSetupProps) {
  const { t } = useI18n()
  const copy = connectionCopy(t)
  const install = t.install

  // null = the card grid; a mode = its panel, with Back to return.
  const [openMode, setOpenMode] = useState<ConnectionMode | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<null | string>(null)
  const [success, setSuccess] = useState<null | string>(null)

  // Nothing is saved yet at first run, so every draft starts empty.
  const { draftFor, updateDraft } = useConnectionDrafts(null)

  const availabilityFor = (mode: ConnectionMode): DesktopBackendAvailability =>
    backends?.find(entry => entry.mode === mode) ?? { mode, available: true }

  const commit = async (mode: ConnectionMode): Promise<void> => {
    setBusy(true)
    setError(null)

    try {
      if (mode === 'local') {
        // The installer path: main.ts settles the setup gate with
        // continue-local and the bootstrap progress view takes over. An
        // applyConnectionConfig({mode:'local'}) here would NOT resume the
        // gate — it takes the teardown path — and the app would hang.
        const start = window.hermesDesktop?.continueBootstrapLocal

        if (typeof start !== 'function') {
          throw new Error(install.localStartUnavailable)
        }

        await start()

        return
      }

      // Remote, cloud and ssh all persist a remote-shaped block and resume
      // the gated startup through the rehome seam.
      await window.hermesDesktop.applyConnectionConfig(moduleFor(mode).toPayload(draftFor(mode), null))
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  const surfaceFor = (mode: ConnectionMode): ConnectionSurface => ({
    availability: availabilityFor(mode),
    commit: {
      apply: () => commit(mode),
      busy
      // No save: deferring a connection the app cannot start without would
      // leave first run with nothing to do.
    },
    copy,
    envOverride: false,
    kind: 'first-run',
    onError: (message: string) => {
      setSuccess(null)
      setError(message)
    },
    onSuccess: (message: string) => {
      setError(null)
      setSuccess(message)
    },
    savedConfig: null,
    scope: null
  })

  const shell = (children: React.ReactNode) => (
    <div className="fixed inset-0 z-(--z-setup) flex items-center justify-center bg-background/90 p-4 backdrop-blur-md">
      <div className="flex max-h-[90vh] w-full max-w-2xl flex-col overflow-y-auto rounded-xl border border-(--stroke-nous) bg-card p-8 shadow-nous">
        {children}
      </div>
    </div>
  )

  if (openMode) {
    const module = moduleFor(openMode)
    const card = module.card({ copy, kind: 'first-run', scope: null })

    return shell(
      <>
        <button
          className="-mt-2 mb-4 flex items-center gap-1.5 self-start text-xs text-muted-foreground transition-colors hover:text-foreground"
          onClick={() => {
            setOpenMode(null)
            setError(null)
            setSuccess(null)
          }}
          type="button"
        >
          <ChevronLeft className="size-3.5" />
          {copy.back}
        </button>

        <div className="flex items-start gap-4">
          <BrandMark className="size-11 shrink-0" />
          <div className="min-w-0">
            <h2 className="text-xl font-semibold tracking-tight">{card.title}</h2>
            <p className="mt-1.5 text-sm text-muted-foreground">{card.description}</p>
          </div>
        </div>

        {module.renderPanel({
          draft: draftFor(openMode),
          onDraftChange: patch => updateDraft(openMode, patch),
          surface: surfaceFor(openMode)
        })}

        {error ? (
          <div className="mt-4 flex items-start gap-2 text-sm text-destructive">
            <AlertCircle className="mt-0.5 size-4 shrink-0" />
            <span>{error}</span>
          </div>
        ) : null}

        {success ? (
          <div className="mt-4 flex items-center gap-2 text-sm text-primary">
            <Check className="size-4" />
            <span>{success}</span>
          </div>
        ) : null}

        {openMode === 'local' ? (
          <div className="mt-6 text-xs text-muted-foreground">
            {install.installTo} <code className="font-mono text-(--ui-text-secondary)">{activeRoot}</code>
          </div>
        ) : null}
      </>
    )
  }

  return shell(
    <>
      <div className="flex items-start gap-4">
        <BrandMark className="size-11 shrink-0" />
        <div className="min-w-0">
          <h2 className="text-xl font-semibold tracking-tight">{install.setupChoiceTitle}</h2>
          <p className="mt-1.5 text-sm text-muted-foreground">{install.setupChoiceDesc}</p>
        </div>
      </div>

      <div className="mt-6">
        <ConnectionModeCards
          availabilityFor={availabilityFor}
          context={{ copy, kind: 'first-run', scope: null }}
          onSelect={setOpenMode}
          selected={null}
        />
      </div>
    </>
  )
}
