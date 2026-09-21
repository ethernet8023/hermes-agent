import { useCallback, useEffect, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Switch } from '@/components/ui/switch'
import { getSandboxStatus, updateSandboxPolicy } from '@/hermes'
import { useI18n } from '@/i18n'
import { AlertTriangle, Loader2, Plus, RefreshCw, ShieldLock, Trash2 } from '@/lib/icons'
import { notifyError } from '@/store/notifications'
import { pickProjectFolder } from '@/store/projects'
import { publishSandboxStatus } from '@/store/sandbox'
import type { SandboxGrantMode, SandboxStatus } from '@/types/hermes'

import { ListRow, Pill } from './primitives'

// The Windows sandbox (MXC) control surface. Every value shown here comes from the backend's
// status route and every edit goes back through its policy route, so the panel never keeps a
// policy of its own: what the user sees is exactly what the next sandboxed command gets.

const SANDBOX_STATUS_TICK_MS = 4000

function statusPill(status: SandboxStatus, labels: { available: string; degraded: string; unavailable: string }) {
  if (!status.available) {
    return <Pill tone="muted">{labels.unavailable}</Pill>
  }

  return <Pill tone="primary">{status.degraded ? labels.degraded : labels.available}</Pill>
}

function FolderRow({
  path,
  mode,
  modeLabel,
  removeLabel,
  disabled,
  onRemove
}: {
  path: string
  mode: SandboxGrantMode
  modeLabel: string
  removeLabel: string
  disabled: boolean
  onRemove: () => void
}) {
  return (
    <div
      className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-background/55 px-2.5 py-2"
      data-mode={mode}
    >
      <div className="flex min-w-0 items-center gap-2">
        <span className="min-w-0 truncate font-mono text-xs" title={path}>
          {path}
        </span>
        <Pill tone={mode === 'readwrite' ? 'primary' : 'muted'}>{modeLabel}</Pill>
      </div>
      <Button aria-label={`${removeLabel} ${path}`} disabled={disabled} onClick={onRemove} size="sm" variant="ghost">
        <Trash2 className="size-3.5" />
      </Button>
    </div>
  )
}

/**
 * Safety > Windows sandbox. Renders nothing on hosts that can never run MXC (macOS, Linux) so the
 * Safety section stays uncluttered; on Windows it explains exactly why the sandbox is unavailable,
 * or offers the opt-in toggle plus the live policy (workspace, extra folders, network).
 */
export function SandboxPanel({ workspace }: { workspace?: string } = {}) {
  const { t } = useI18n()
  const copy = t.settings.sandbox
  const [status, setStatus] = useState<SandboxStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const activeRef = useRef(false)

  const apply = useCallback((next: SandboxStatus) => {
    publishSandboxStatus(next)

    if (activeRef.current) {
      setStatus(next)
    }
  }, [])

  const refresh = useCallback(
    async (options: { refresh?: boolean } = {}) => {
      try {
        apply(await getSandboxStatus({ workspace, ...options }))
      } catch (err) {
        if (activeRef.current) {
          notifyError(err, copy.updateFailed)
        }
      } finally {
        if (activeRef.current) {
          setLoading(false)
        }
      }
    },
    [apply, copy.updateFailed, workspace]
  )

  // eslint-disable-next-line no-restricted-syntax -- legitimate non-atom ref write (see eslint rule comment)
  useEffect(() => {
    activeRef.current = true
    void refresh()

    return () => void (activeRef.current = false)
  }, [refresh])

  // The container tally is a running count of work happening in other panes, so while the
  // sandbox is on and this panel is visible it re-reads the status on a slow tick; a hidden
  // window pauses the tick and a returning one refreshes at once.
  const enabled = status?.enabled ?? false
  useEffect(() => {
    if (!enabled) {
      return
    }

    const tick = () => {
      if (document.visibilityState === 'visible') {
        void refresh()
      }
    }

    const timer = window.setInterval(tick, SANDBOX_STATUS_TICK_MS)
    document.addEventListener('visibilitychange', tick)

    return () => {
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', tick)
    }
  }, [enabled, refresh])

  const mutate = useCallback(
    async (work: () => Promise<SandboxStatus>, failureTitle: string) => {
      setBusy(true)

      try {
        apply(await work())
      } catch (err) {
        if (activeRef.current) {
          notifyError(err, failureTitle)
          void refresh()
        }
      } finally {
        if (activeRef.current) {
          setBusy(false)
        }
      }
    },
    [apply, refresh]
  )

  const setEnabled = (enabled: boolean) => void mutate(() => updateSandboxPolicy({ enabled }), copy.enableFailed)

  const setNetwork = (network: boolean) => void mutate(() => updateSandboxPolicy({ network }), copy.updateFailed)

  const addFolder = async (mode: SandboxGrantMode) => {
    if (!status) {
      return
    }

    const dir = await pickProjectFolder()

    if (!dir) {
      return
    }

    const readwrite = status.policy.readwrite_paths.filter(p => p.toLowerCase() !== dir.toLowerCase())
    const readonly = status.policy.readonly_paths.filter(p => p.toLowerCase() !== dir.toLowerCase())

    if (mode === 'readwrite') {
      readwrite.push(dir)
    } else {
      readonly.push(dir)
    }

    void mutate(() => updateSandboxPolicy({ readwrite_paths: readwrite, readonly_paths: readonly }), copy.updateFailed)
  }

  const removeFolder = (path: string) => {
    if (!status) {
      return
    }

    void mutate(
      () =>
        updateSandboxPolicy({
          readwrite_paths: status.policy.readwrite_paths.filter(p => p !== path),
          readonly_paths: status.policy.readonly_paths.filter(p => p !== path)
        }),
      copy.updateFailed
    )
  }

  if (loading) {
    return (
      <div className="flex items-center gap-2 px-1 text-xs text-muted-foreground" data-testid="sandbox-panel-loading">
        <Loader2 className="size-3.5 animate-spin" />
      </div>
    )
  }

  if (!status || !status.platform_supported) {
    return null
  }

  const grants: { path: string; mode: SandboxGrantMode }[] = [
    ...(status.policy?.readwrite_paths ?? []).map(path => ({ path, mode: 'readwrite' as const })),
    ...(status.policy?.readonly_paths ?? []).map(path => ({ path, mode: 'read' as const }))
  ]

  return (
    <section className="grid gap-2" data-testid="sandbox-panel">
      <div className="flex flex-wrap items-center justify-between gap-2 px-1">
        <div className="flex min-w-0 items-center gap-2">
          <ShieldLock className="size-4 shrink-0" />
          <span className="text-sm font-medium">{copy.heading}</span>
          {statusPill(status, {
            available: copy.statusAvailable,
            degraded: copy.statusDegraded,
            unavailable: copy.statusUnavailable
          })}
        </div>
        <Button onClick={() => void refresh({ refresh: true })} size="sm" variant="text">
          <RefreshCw className="size-3.5" />
          {copy.recheck}
        </Button>
      </div>

      <p className="px-1 text-[0.72rem] text-muted-foreground">{copy.description}</p>

      {!status.available && !status.shell_missing && status.reason && (
        <p className="px-1 text-[0.72rem] text-muted-foreground" data-testid="sandbox-reason">
          <AlertTriangle className="mr-1 inline size-3" />
          {status.reason}
        </p>
      )}

      {(status.warnings ?? []).map(warning => (
        <p className="px-1 text-[0.7rem] text-muted-foreground" key={warning}>
          <AlertTriangle className="mr-1 inline size-3" />
          {warning}
        </p>
      ))}

      <ListRow
        action={
          <Switch
            aria-label={copy.toggleLabel}
            checked={status.enabled}
            disabled={busy || (!status.available && !status.shell_missing)}
            onCheckedChange={setEnabled}
          />
        }
        description={copy.toggleDescription}
        hint={status.shell_missing && !status.enabled ? copy.shellNote : undefined}
        title={copy.toggleLabel}
      />

      {status.enabled && (
        <>
          <p className="px-1 text-[0.72rem] text-muted-foreground" data-testid="sandbox-workspace-rule">
            {copy.workspaceRule}
          </p>
          <p className="px-1 text-[0.72rem] text-muted-foreground">{copy.isolationRule}</p>

          <ListRow
            below={
              <div className="mt-2 grid gap-1.5">
                {grants.length === 0 ? (
                  <p className="text-[0.72rem] text-muted-foreground">{copy.foldersEmpty}</p>
                ) : (
                  grants.map(grant => (
                    <FolderRow
                      disabled={busy}
                      key={`${grant.mode}:${grant.path}`}
                      mode={grant.mode}
                      modeLabel={grant.mode === 'readwrite' ? copy.modeReadWrite : copy.modeRead}
                      onRemove={() => removeFolder(grant.path)}
                      path={grant.path}
                      removeLabel={copy.remove}
                    />
                  ))
                )}
                <div className="flex flex-wrap gap-2">
                  <Button disabled={busy} onClick={() => void addFolder('read')} size="sm" variant="ghost">
                    <Plus className="size-3.5" />
                    {copy.addReadOnly}
                  </Button>
                  <Button disabled={busy} onClick={() => void addFolder('readwrite')} size="sm" variant="ghost">
                    <Plus className="size-3.5" />
                    {copy.addReadWrite}
                  </Button>
                </div>
              </div>
            }
            title={copy.foldersTitle}
            wide
          />

          <ListRow
            action={
              <Switch
                aria-label={copy.networkLabel}
                checked={status.policy.network}
                disabled={busy}
                onCheckedChange={setNetwork}
              />
            }
            description={copy.networkDescription}
            title={copy.networkLabel}
          />

          <p className="px-1 text-[0.7rem] text-muted-foreground" data-testid="sandbox-containers">
            {copy.containers(status.containers_started)}
          </p>
        </>
      )}
    </section>
  )
}
