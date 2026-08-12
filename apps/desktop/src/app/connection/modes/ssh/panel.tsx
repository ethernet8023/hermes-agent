// app/connection/modes/ssh/panel.tsx — host picker plus connection details.
//
// The host dropdown is populated from ~/.ssh/config through the desktop
// bridge, with a Custom escape hatch for a raw host or IP. Selecting a known
// host resolves its user/port/identity file and fills only the blanks.

import { useEffect, useRef, useState } from 'react'

import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { cn } from '@/lib/utils'

import { CONTROL_TEXT } from '../../../settings/constants'
import { ListRow } from '../../../settings/primitives'
import { ConnectionActions } from '../../connection-actions'
import type { ConnectionConfigPanelProps } from '../../types'

import { enrichSelectedSshHost, selectSshHost } from './host-selection'

import { type SshDraft, sshMode } from './index'

const SSH_HOST_CUSTOM = '__custom__'

export function SshPanel({ draft, onDraftChange, surface }: ConnectionConfigPanelProps<SshDraft>) {
  const { commit, copy, envOverride, kind, scope } = surface
  const [suggestions, setSuggestions] = useState<string[]>([])
  const [customHost, setCustomHost] = useState(false)
  const [testing, setTesting] = useState(false)
  const resolveSeq = useRef(0)
  const testSeq = useRef(0)

  const hasHost = Boolean(draft.host.trim())

  // An ssh failure is reported as a tagged reason rather than a message, so
  // the panel maps it to copy. `unknown` covers a reachable host that failed
  // for a reason the bridge could not classify.
  const sshErrorMessage = (reason: null | string | undefined): string => {
    const errors: Record<string, string> = {
      'auth-failed': copy.sshErrAuth,
      'hermes-not-found': copy.sshErrNotInstalled,
      'host-key-changed': copy.sshErrHostKey,
      timeout: copy.sshErrTimeout,
      unreachable: copy.sshErrUnreachable,
      'unsupported-platform': copy.sshErrPlatform,
      'update-required': copy.sshErrUpdateRequired
    }

    return (reason && errors[reason]) || copy.sshErrUnknown
  }

  const runTest = async (): Promise<void> => {
    const seq = ++testSeq.current
    setTesting(true)

    try {
      const result = await window.hermesDesktop.testConnectionConfig(sshMode.toPayload(draft, scope))

      if (seq !== testSeq.current) {
        return
      }

      if (!result.reachable) {
        surface.onError(sshErrorMessage(result.sshError) || result.error || copy.sshErrUnknown)

        return
      }

      surface.onSuccess?.(copy.sshReachable(result.host || draft.host, result.remotePlatform || '?'))
    } catch (err) {
      if (seq === testSeq.current) {
        surface.onError(err instanceof Error ? err.message : String(err || copy.sshErrUnknown))
      }
    } finally {
      if (seq === testSeq.current) {
        setTesting(false)
      }
    }
  }

  useEffect(() => {
    if (!window.hermesDesktop?.sshConfigHosts) {
      return
    }

    let cancelled = false

    void window.hermesDesktop
      .sshConfigHosts()
      .then(result => {
        if (!cancelled) {
          setSuggestions(result.hosts)
        }
      })
      .catch(() => {
        if (!cancelled) {
          setSuggestions([])
        }
      })

    return () => void (cancelled = true)
  }, [])

  useEffect(() => {
    // One-directional: a saved host absent from the suggestions must render
    // the free-text input. Never force custom OFF here — that would snap a
    // just-clicked Custom (empty host) input back to the dropdown and make a
    // raw IP impossible to type. onBlur is the way back.
    if (draft.host && !suggestions.includes(draft.host)) {
      setCustomHost(true)
    }
  }, [draft.host, suggestions])

  const resolveHost = async (host: string): Promise<void> => {
    if (!host || !window.hermesDesktop?.sshResolveHost) {
      return
    }

    const seq = ++resolveSeq.current

    try {
      const resolved = await window.hermesDesktop.sshResolveHost(host)

      if (seq !== resolveSeq.current) {
        return
      }

      onDraftChange(enrichSelectedSshHost(draft, host, resolved))
    } catch {
      // A failed lookup just leaves the fields for the user to fill in.
    }
  }

  const selectHost = (value: string): void => {
    if (value === SSH_HOST_CUSTOM) {
      setCustomHost(true)
      onDraftChange(selectSshHost(draft, ''))

      return
    }

    setCustomHost(false)
    onDraftChange(selectSshHost(draft, value))
    void resolveHost(value)
  }

  return (
    <div className="mt-5 grid gap-1">
      {suggestions.length > 0 && !customHost ? (
        <ListRow
          action={
            <Select onValueChange={selectHost} value={suggestions.includes(draft.host) ? draft.host : SSH_HOST_CUSTOM}>
              <SelectTrigger className={cn('h-8', CONTROL_TEXT)}>
                <SelectValue placeholder={copy.sshHostPick} />
              </SelectTrigger>
              <SelectContent>
                {suggestions.map(host => (
                  <SelectItem key={host} value={host}>
                    {host}
                  </SelectItem>
                ))}
                <SelectItem value={SSH_HOST_CUSTOM}>{copy.sshHostCustom}</SelectItem>
              </SelectContent>
            </Select>
          }
          description={copy.sshHostPickDesc}
          title={copy.sshHostPickTitle}
        />
      ) : (
        <ListRow
          action={
            <Input
              aria-label={copy.sshHostTitle}
              autoFocus={customHost}
              className={cn('h-8', CONTROL_TEXT)}
              onBlur={() => {
                // An empty host on blur with suggestions available means the
                // user backed out of Custom; return them to the dropdown.
                if (!draft.host.trim() && suggestions.length > 0) {
                  setCustomHost(false)

                  return
                }

                void resolveHost(draft.host)
              }}
              onChange={event => onDraftChange(selectSshHost(draft, event.target.value))}
              value={draft.host}
            />
          }
          description={copy.sshHostDesc}
          title={copy.sshHostTitle}
        />
      )}

      <ListRow
        action={
          <Input
            className={cn('h-8', CONTROL_TEXT)}
            onChange={event => onDraftChange({ user: event.target.value })}
            placeholder={copy.sshUserPlaceholder}
            value={draft.user}
          />
        }
        description={copy.sshUserDesc}
        title={copy.sshUserTitle}
      />

      <ListRow
        action={
          <Input
            className={cn('h-8', CONTROL_TEXT)}
            inputMode="numeric"
            onChange={event => onDraftChange({ port: event.target.value ? Number(event.target.value) : null })}
            placeholder="22"
            value={draft.port ?? ''}
          />
        }
        description={copy.sshPortDesc}
        title={copy.sshPortTitle}
      />

      <ListRow
        action={
          <Input
            className={cn('h-8 font-mono', CONTROL_TEXT)}
            onChange={event => onDraftChange({ keyPath: event.target.value })}
            value={draft.keyPath}
          />
        }
        description={copy.sshKeyDesc}
        title={copy.sshKeyTitle}
      />

      <ListRow
        action={
          <Input
            className={cn('h-8 font-mono', CONTROL_TEXT)}
            onChange={event => onDraftChange({ remoteHermesPath: event.target.value })}
            placeholder={copy.sshHermesPathPlaceholder}
            value={draft.remoteHermesPath}
          />
        }
        description={copy.sshHermesPathDesc}
        title={copy.sshHermesPathTitle}
      />

      {scope !== null ? (
        <ListRow
          action={
            <Input
              className={cn('h-8 font-mono', CONTROL_TEXT)}
              onChange={event => onDraftChange({ remoteProfile: event.target.value })}
              placeholder={scope}
              value={draft.remoteProfile}
            />
          }
          description={copy.sshRemoteProfileDesc}
          title={copy.sshRemoteProfileTitle}
        />
      ) : null}

      <ConnectionActions
        applyLabel={kind === 'first-run' ? copy.remoteApplyAction : undefined}
        canApply={hasHost}
        commit={commit}
        copy={copy}
        disabled={envOverride}
        test={{ busy: testing, canRun: hasHost, label: copy.sshTestConnection, run: () => void runTest() }}
      />
    </div>
  )
}
