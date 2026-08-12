import { useStore } from '@nanostores/react'
import { useEffect, useMemo, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import type { DesktopBackendAvailability, DesktopConnectionConfig } from '@/global'
import { useI18n } from '@/i18n'
import { AlertCircle, FileText, Globe } from '@/lib/icons'
import { cn } from '@/lib/utils'
import { notify, notifyError } from '@/store/notifications'
import { $profiles, refreshActiveProfile } from '@/store/profile'

import {
  connectionCopy,
  type ConnectionMode,
  ConnectionModeCards,
  type ConnectionSurface,
  moduleFor,
  useConnectionDrafts
} from '../connection'

import { EmptyState, ListRow, Pill, SettingsContent, SettingsSkeleton } from './primitives'

// Settings → Gateway: the post-bootstrap host for the connection modes in
// app/connection. This file owns what is genuinely Settings' own — profile
// scope, the env-override banner, save-for-restart alongside apply, toast
// presentation, and the diagnostics row — while every mode's card and form
// comes from the shared registry that first-run setup also renders.
//
// `embedded` trims the page chrome for reuse inside the boot-failure recovery
// card: the outer title/intro, the "Save for next restart" action, and the
// Diagnostics row are redundant there (the card owns its header and a single
// reconnect action), so only the connection controls render.

function ScopeChip({ active, label, onSelect }: { active: boolean; label: string; onSelect: () => void }) {
  return (
    <button
      className={cn(
        'rounded-full border px-3 py-1 text-[length:var(--conversation-caption-font-size)] transition',
        active
          ? 'border-(--ui-stroke-secondary) bg-(--ui-bg-tertiary) text-(--ui-text-primary)'
          : 'border-(--ui-stroke-tertiary) bg-(--ui-bg-quinary) text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover)'
      )}
      onClick={onSelect}
      type="button"
    >
      {label}
    </button>
  )
}

export function GatewaySettings({ embedded = false }: { embedded?: boolean } = {}) {
  const { t } = useI18n()
  const g = t.settings.gateway
  const copy = useMemo(() => connectionCopy(t), [t])

  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [config, setConfig] = useState<DesktopConnectionConfig | null>(null)
  const [mode, setMode] = useState<ConnectionMode>('local')
  const [scope, setScope] = useState<null | string>(null)
  const [modeAvailabilities, setModeAvailabilities] = useState<DesktopBackendAvailability[]>([])
  const profiles = useStore($profiles)
  const saveSeq = useRef(0)

  const { draftFor, updateDraft } = useConnectionDrafts(config)

  const availabilityFor = (target: ConnectionMode): DesktopBackendAvailability =>
    modeAvailabilities.find(entry => entry.mode === target) ?? { mode: target, available: true }

  // Which modes this artifact + machine offer (electron backend registry).
  // Unavailable modes render disabled with their reason. Empty until the IPC
  // answers; failure or an older Electron leaves every mode offered.
  useEffect(() => {
    let cancelled = false

    window.hermesDesktop
      ?.getBackendAvailability?.()
      .then((list: DesktopBackendAvailability[] | undefined) => {
        if (!cancelled && Array.isArray(list)) {
          setModeAvailabilities(list)
        }
      })
      .catch(() => {
        // Older Electron build without the IPC — every mode stays offered.
      })

    return () => void (cancelled = true)
  }, [])

  useEffect(() => {
    void refreshActiveProfile()
  }, [])

  useEffect(() => {
    let cancelled = false
    const desktop = window.hermesDesktop

    if (!desktop?.getConnectionConfig) {
      setLoading(false)

      return () => void (cancelled = true)
    }

    setLoading(true)

    desktop
      .getConnectionConfig(scope)
      .then(loaded => {
        if (cancelled) {
          return
        }

        // Replacing the config object reseeds every mode's draft, so a token
        // typed under one profile cannot leak into the next.
        setConfig(loaded)
        setMode(loaded.mode)
      })
      .catch(err => notifyError(err, g.failedLoad))
      .finally(() => {
        if (!cancelled) {
          setLoading(false)
        }
      })

    return () => void (cancelled = true)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reload on scope change only; copy is stable
  }, [scope])

  // The 'default' profile uses the global ("All profiles") connection, so the
  // per-profile scopes are the named, non-default profiles.
  const namedProfiles = useMemo(() => profiles.filter(profile => profile.name !== 'default'), [profiles])

  const commitConfig = async (apply: boolean): Promise<void> => {
    const seq = ++saveSeq.current
    const payload = moduleFor(mode).toPayload(draftFor(mode), scope)
    setSaving(true)

    try {
      const next = apply
        ? await window.hermesDesktop.applyConnectionConfig(payload)
        : await window.hermesDesktop.saveConnectionConfig(payload)

      if (seq !== saveSeq.current) {
        return
      }

      setConfig(next)
      notify({
        kind: 'success',
        title: apply ? g.restartingTitle : g.savedTitle,
        message: apply ? g.restartingMessage : g.savedMessage
      })
    } catch (err) {
      if (seq === saveSeq.current) {
        notifyError(err, apply ? g.applyFailed : g.saveFailed)
      }
    } finally {
      if (seq === saveSeq.current) {
        setSaving(false)
      }
    }
  }

  const surface: ConnectionSurface = {
    availability: availabilityFor(mode),
    // Settings persists the URL and mode before the login window opens,
    // because that window reads the saved config to know where to go.
    beforeOAuthLogin: async (trimmedUrl: string) => {
      const saved = await window.hermesDesktop.saveConnectionConfig({
        mode,
        profile: scope ?? undefined,
        remoteAuthMode: 'oauth',
        remoteUrl: trimmedUrl
      })

      setConfig(saved)
    },
    commit: {
      apply: () => commitConfig(true),
      busy: saving,
      // The boot-failure card owns a single reconnect action, so deferring a
      // save there would offer a button that resolves nothing.
      save: embedded ? undefined : () => commitConfig(false)
    },
    copy,
    envOverride: Boolean(config?.envOverride),
    kind: 'settings',
    onError: (message: string) => notify({ kind: 'warning', title: g.incompleteTitle, message }),
    onSuccess: (message: string) => notify({ kind: 'success', title: g.reachableTitle, message }),
    savedConfig: config,
    scope
  }

  if (loading) {
    return (
      <SettingsSkeleton
        sections={[
          { heading: true, rows: 3 },
          { heading: true, rows: 3 }
        ]}
      />
    )
  }

  if (!window.hermesDesktop?.getConnectionConfig) {
    return <EmptyState description={g.unavailableDesc} title={g.unavailableTitle} />
  }

  const activeModule = moduleFor(mode)

  return (
    <SettingsContent bare={embedded}>
      {embedded ? null : (
        <div className="mb-5">
          <div className="flex items-center gap-2 text-[length:var(--conversation-text-font-size)] font-medium">
            <Globe className="size-4 text-muted-foreground" />
            {g.title}
            {config?.envOverride ? <Pill tone="primary">{g.envOverride}</Pill> : null}
          </div>
          <p className="mt-2 max-w-2xl text-[length:var(--conversation-caption-font-size)] leading-(--conversation-caption-line-height) text-(--ui-text-tertiary)">
            {g.intro}
          </p>
        </div>
      )}

      {namedProfiles.length > 0 ? (
        <div className="mb-5 grid gap-2">
          <div className="text-[length:var(--conversation-caption-font-size)] font-medium text-(--ui-text-secondary)">
            {g.appliesTo}
          </div>
          <div className="flex flex-wrap gap-1.5">
            <ScopeChip active={scope === null} label={g.allProfiles} onSelect={() => setScope(null)} />
            {namedProfiles.map(profile => (
              <ScopeChip
                active={scope === profile.name}
                key={profile.name}
                label={profile.name}
                onSelect={() => setScope(profile.name)}
              />
            ))}
          </div>
          <p className="text-[length:var(--conversation-caption-font-size)] leading-(--conversation-caption-line-height) text-(--ui-text-tertiary)">
            {scope === null ? g.defaultConnection : g.profileConnection(scope)}
          </p>
        </div>
      ) : null}

      {config?.envOverride ? (
        <div className="mb-5 flex items-start gap-2 rounded-xl border border-destructive/30 bg-destructive/10 px-3 py-2.5 text-[length:var(--conversation-caption-font-size)] text-destructive">
          <AlertCircle className="mt-0.5 size-4 shrink-0" />
          <div>
            <div className="font-medium">{g.envOverrideTitle}</div>
            <div className="mt-1 leading-5">{g.envOverrideDesc}</div>
          </div>
        </div>
      ) : null}

      <div className="mb-5 grid gap-2">
        <div className="text-[length:var(--conversation-caption-font-size)] font-medium text-(--ui-text-secondary)">
          {g.modeTitle}
        </div>
        <ConnectionModeCards
          availabilityFor={availabilityFor}
          context={{ copy, kind: 'settings', scope }}
          envOverride={Boolean(config?.envOverride)}
          onSelect={setMode}
          selected={mode}
        />
      </div>

      {config?.envOverride
        ? null
        : activeModule.renderPanel({
            draft: draftFor(mode),
            onDraftChange: patch => updateDraft(mode, patch),
            surface
          })}

      {embedded ? null : (
        <div className="mt-6 grid gap-1">
          <ListRow
            action={
              <Button onClick={() => void window.hermesDesktop?.revealLogs()} size="sm" variant="textStrong">
                <FileText />
                {g.openLogs}
              </Button>
            }
            description={g.diagnosticsDesc}
            title={g.diagnostics}
          />
        </div>
      )}
    </SettingsContent>
  )
}
