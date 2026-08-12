// app/connection/modes/cloud/panel.tsx — portal sign-in, org, agent picker.
//
// Cloud owns real machinery (portal session, org discovery, the silent
// per-agent cascade) and drives it through the desktop's cloud bridge. That
// is mode-specific capability, the same way the remote panel probes a URL —
// what stays out of here is persistence POLICY: picking an agent writes the
// draft and asks the surface to commit, so Settings soft-rehomes and
// first-run resumes its gate without this panel knowing which happened.

import { useEffect, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import type { DesktopCloudAgent, DesktopCloudOrg } from '@/global'
import { ExternalLink } from '@/lib/external-link'
import { AlertCircle, Check, Loader2, LogIn, RefreshCw } from '@/lib/icons'
import { cn } from '@/lib/utils'

import { ListRow, Pill } from '../../../settings/primitives'
import type { ConnectionConfigPanelProps } from '../../types'

import { type CloudDraft, savedCloudConnectionUrl } from './index'

type DiscoverStatus = 'idle' | 'loading' | 'done' | 'error'

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err || 'Unknown error')
}

export function CloudPanel({ draft, onDraftChange, surface }: ConnectionConfigPanelProps<CloudDraft>) {
  const { commit, copy, onError, savedConfig } = surface
  const [signedIn, setSignedIn] = useState(false)
  const [signingIn, setSigningIn] = useState(false)
  const [agents, setAgents] = useState<DesktopCloudAgent[]>([])
  const [orgs, setOrgs] = useState<DesktopCloudOrg[]>([])
  const [discover, setDiscover] = useState<DiscoverStatus>('idle')
  const [connectingId, setConnectingId] = useState<null | string>(null)
  // Discovery resolves the org asynchronously and a click can land in the
  // same tick, so connect reads the ref rather than a captured render value.
   
  const orgRef = useRef<string>(draft.org)
  const seq = useRef(0)

  const setOrg = (value: string): void => {
    orgRef.current = value
    onDraftChange({ org: value })
  }

  // The saved cloud instance, normalized so a host-casing difference cannot
  // break the connected highlight (the saved URL went through electron's
  // normalizeRemoteBaseUrl; a discovered dashboardUrl arrives raw).
  const connectedUrl = savedConfig ? savedCloudConnectionUrl(savedConfig) : ''

  const isConnected = (agent: DesktopCloudAgent): boolean =>
    Boolean(
      connectedUrl &&
        agent.dashboardUrl &&
        savedCloudConnectionUrl({ mode: 'cloud', remoteUrl: agent.dashboardUrl }) === connectedUrl
    )

  const runDiscover = async (org?: string): Promise<void> => {
    const cloud = window.hermesDesktop?.cloud

    if (!cloud) {
      return
    }

    const current = seq.current
    setDiscover('loading')

    try {
      const result = await cloud.discover(org)

      if (current !== seq.current) {
        return
      }

      if ('needsOrgSelection' in result && result.needsOrgSelection) {
        setOrgs(result.orgs)
        setAgents([])
        setDiscover('done')

        return
      }

      setAgents('agents' in result ? result.agents : [])

      // Record the org authoritatively from the response (it echoes the org
      // the list was scoped to), so a single-membership auto-resolve — where
      // no picker ran and no org was requested — still persists one.
      const resolved = 'org' in result && result.org ? (result.org.slug ?? result.org.id) : null

      if (resolved) {
        setOrg(resolved)
      } else if (org) {
        setOrg(org)
      }

      setDiscover('done')
    } catch (err) {
      if (current !== seq.current) {
        return
      }

      setAgents([])
      setDiscover('error')

      if (err && typeof err === 'object' && 'needsCloudLogin' in err) {
        setSignedIn(false)
      }

      onError(errorMessage(err))
    }
  }

  // Read the portal session on mount and auto-discover when already signed
  // in, so the picker is populated without a click. Restores the saved org so
  // we reopen into that org's agents rather than the picker.
  useEffect(() => {
    const cloud = window.hermesDesktop?.cloud

    if (!cloud) {
      return
    }

    let cancelled = false

    cloud
      .status()
      .then(status => {
        if (cancelled) {
          return
        }

        setSignedIn(status.signedIn)

        if (status.signedIn) {
          void runDiscover(orgRef.current || undefined)
        }
      })
      .catch(() => {
        if (!cancelled) {
          setSignedIn(false)
        }
      })

    return () => void (cancelled = true)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- mount-only: discovery is re-run explicitly by its own controls
  }, [])

  const signIn = async (): Promise<void> => {
    const cloud = window.hermesDesktop?.cloud

    if (!cloud) {
      return
    }

    setSigningIn(true)

    try {
      const result = await cloud.login()
      setSignedIn(result.signedIn)

      if (result.signedIn) {
        await runDiscover()
      }
    } catch (err) {
      onError(errorMessage(err))
    } finally {
      setSigningIn(false)
    }
  }

  const signOut = async (): Promise<void> => {
    const cloud = window.hermesDesktop?.cloud

    if (!cloud) {
      return
    }

    setSigningIn(true)

    try {
      await cloud.logout()
      setSignedIn(false)
      setAgents([])
      setOrgs([])
      setOrg('')
      setDiscover('idle')
    } catch (err) {
      onError(errorMessage(err))
    } finally {
      setSigningIn(false)
    }
  }

  // Drive the silent per-agent cascade, then hand the chosen URL to the
  // surface. The panel decides WHICH agent; the surface decides what
  // committing to it means.
  const connectAgent = async (agent: DesktopCloudAgent): Promise<void> => {
    const cloud = window.hermesDesktop?.cloud

    if (!cloud || !agent.dashboardUrl) {
      return
    }

    setConnectingId(agent.id)

    try {
      const result = await cloud.agentSignIn(agent.dashboardUrl)

      if (!result.connected) {
        onError(copy.cloudConnectFailed)

        return
      }

      onDraftChange({ agentUrl: agent.dashboardUrl, org: orgRef.current })
      await commit.apply()
      surface.onSuccess?.(copy.cloudConnectedTo(agent.name))
    } catch (err) {
      if (err && typeof err === 'object' && 'needsCloudLogin' in err) {
        setSignedIn(false)
      }

      onError(errorMessage(err))
    } finally {
      setConnectingId(null)
    }
  }

  return (
    <div className="mt-5 grid gap-1">
      <ListRow
        action={
          signedIn ? (
            <div className="flex items-center gap-2">
              <Pill tone="primary">
                <Check className="size-3" /> {copy.cloudSignedIn}
              </Pill>
              <Button disabled={signingIn} onClick={() => void signOut()} variant="outline">
                {signingIn ? <Loader2 className="animate-spin" /> : null}
                {copy.signOut}
              </Button>
            </div>
          ) : (
            <Button disabled={signingIn} onClick={() => void signIn()}>
              {signingIn ? <Loader2 className="animate-spin" /> : <LogIn />}
              {copy.cloudSignIn}
            </Button>
          )
        }
        description={signedIn ? copy.cloudSignedInDesc : copy.cloudNeedsSignIn}
        title={copy.cloudSignInTitle}
      />

      {signedIn ? (
        orgs.length > 0 && !draft.org ? (
          <div className="mt-3">
            <div className="mb-2 text-[length:var(--conversation-caption-font-size)] font-medium text-(--ui-text-secondary)">
              {copy.cloudOrgPickerTitle}
            </div>
            <div className="grid gap-1">
              {orgs.map(org => (
                <ListRow
                  action={
                    <Button
                      onClick={() => {
                        const ref = org.slug ?? org.id
                        setOrg(ref)
                        void runDiscover(ref)
                      }}
                      size="sm"
                    >
                      {copy.cloudOrgSelect}
                    </Button>
                  }
                  description={copy.cloudOrgRole(org.role)}
                  key={org.id}
                  title={org.name}
                />
              ))}
            </div>
          </div>
        ) : (
          <div className="mt-3">
            <div className="mb-2 flex items-center justify-between">
              <div className="text-[length:var(--conversation-caption-font-size)] font-medium text-(--ui-text-secondary)">
                {copy.cloudAgentsTitle}
              </div>
              <div className="flex items-center gap-2">
                {draft.org ? (
                  // Clearing the org and re-discovering gives a multi-org user
                  // the picker back and harmlessly re-resolves for a single-org
                  // one. Shown whenever an org is set, including after a
                  // restore-open that never populated the org list.
                  <Button
                    onClick={() => {
                      setOrg('')
                      setAgents([])
                      void runDiscover()
                    }}
                    size="sm"
                    variant="text"
                  >
                    {copy.cloudOrgChange}
                  </Button>
                ) : null}
                <Button
                  disabled={discover === 'loading'}
                  onClick={() => void runDiscover(draft.org || undefined)}
                  size="sm"
                  variant="text"
                >
                  {discover === 'loading' ? <Loader2 className="animate-spin" /> : <RefreshCw />}
                  {copy.cloudRefresh}
                </Button>
              </div>
            </div>

            {discover === 'loading' ? (
              <div className="flex items-center gap-2 py-3 text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">
                <Loader2 className="size-4 animate-spin" />
                {copy.cloudLoadingAgents}
              </div>
            ) : agents.length === 0 ? (
              <div className="flex items-start gap-2 py-3 text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">
                <AlertCircle className="mt-0.5 size-4 shrink-0" />
                <span>
                  {copy.cloudNoAgents.before}
                  <ExternalLink href="https://portal.nousresearch.com/agents" showExternalIcon={false}>
                    {copy.cloudNoAgents.linkText}
                  </ExternalLink>
                  {copy.cloudNoAgents.after}
                </span>
              </div>
            ) : (
              <div className="grid gap-1">
                {agents.map(agent => {
                  const connected = isConnected(agent)

                  return (
                    <div
                      className={cn('rounded-md px-2', connected && 'bg-primary/5 ring-1 ring-primary/25')}
                      key={agent.id}
                    >
                      <ListRow
                        action={
                          connected ? (
                            <Pill tone="primary">
                              <Check className="mr-1 inline size-3" />
                              {copy.cloudConnectedPill}
                            </Pill>
                          ) : (
                            <Button
                              disabled={!agent.dashboardUrl || connectingId !== null || commit.busy}
                              onClick={() => void connectAgent(agent)}
                              size="sm"
                            >
                              {connectingId === agent.id ? <Loader2 className="animate-spin" /> : null}
                              {agent.dashboardUrl
                                ? connectingId === agent.id
                                  ? copy.cloudConnecting
                                  : copy.cloudConnect
                                : copy.cloudAgentProvisioning}
                            </Button>
                          )
                        }
                        description={copy.cloudStatusLabel(agent.dashboardGatewayState)}
                        title={agent.name}
                      />
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        )
      ) : null}
    </div>
  )
}
