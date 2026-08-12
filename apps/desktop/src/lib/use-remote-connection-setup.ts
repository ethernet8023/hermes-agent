// useRemoteConnectionSetup — THE logic for configuring a remote gateway
// connection, used by the remote mode's config panel (app/connection/modes/
// remote) on both hosts that render it: first-run setup and Settings.
// One narrow job: take a URL + token the surface owns, probe the gateway's
// auth mode, gate Test/Apply on a tested payload, and run the oauth sign-in
// — with every seq guard in one place.
//
// The panel stays a thin skin and the hosts keep their real differences:
//  - error/success PRESENTATION (inline rows vs notify toasts) via the
//    onError/onSuccess callbacks;
//  - oauth PRE-SAVE: Settings persists url+mode before the login window
//    (the saved config is what the window reads), first-run deliberately
//    persists nothing until Apply — injected as beforeOAuthLogin.

import { useCallback, useEffect, useRef, useState } from 'react'

import type { DesktopConnectionConfigInput, DesktopConnectionProbeResult } from '@/global'
import { coerceRemoteUrlScheme } from '@/lib/remote-url'

export type RemoteAuthMode = 'oauth' | 'token'
export type RemoteProbeStatus = 'done' | 'error' | 'idle' | 'probing'

export interface RemoteConnectionSetupCopy {
  /** signIn() with an empty URL. */
  enterUrlFirst: string
  /** oauth window closed without completing. */
  signInIncomplete: string
  /** probe failed with no error detail. */
  probeError: string
}

export interface RemoteConnectionSetupOptions {
  /** The URL as typed; the hook derives the coerced/trimmed form. */
  remoteUrl: string
  /** The session token as typed (token-auth gateways). */
  remoteToken: string
  /** Localized failure strings (both surfaces pass their t.install/gateway copy). */
  copy: RemoteConnectionSetupCopy
  /** Probe only while relevant (Settings: only in remote mode). Default true. */
  enabled?: boolean
  /**
   * The saved config's auth mode, used as the fallback when the live probe
   * has not resolved — re-opening Settings must not flicker back to the
   * token control on an oauth gateway. First-run has no saved config and
   * omits this (falls back to 'token').
   */
  savedAuthMode?: RemoteAuthMode
  /**
   * True when the saved config already carries working credentials (a
   * persisted token or a completed oauth session). Treated as
   * auth-resolved while the probe is idle so the saved control renders
   * immediately. First-run has none and omits this.
   */
  hasSavedCredentials?: boolean
  /**
   * Extra payload fields merged into the tested/applied config
   * (Settings passes profile scope; first-run passes nothing).
   */
  payloadExtras?: Partial<DesktopConnectionConfigInput>
  /** Runs before the oauth login window opens (Settings pre-saves here). */
  beforeOAuthLogin?: (trimmedUrl: string) => Promise<void>
  /** Presentation of failures — inline row or toast, the surface decides. */
  onError: (message: string) => void
  /** Cleared-state hook: fires whenever prior results are invalidated. */
  onInvalidate?: () => void
}

export interface RemoteConnectionSetup {
  /** Coerced, trimmed URL actually used for probe/test/apply. */
  trimmedUrl: string
  probeStatus: RemoteProbeStatus
  probe: DesktopConnectionProbeResult | null
  authMode: RemoteAuthMode
  /** True once the probe resolved a definite auth mode. */
  authResolved: boolean
  oauthConnected: boolean
  signingIn: boolean
  testing: boolean
  /** True when the CURRENT payload matches the last successful test. */
  canApply: boolean
  canTest: boolean
  /** The connection payload for the current inputs. */
  payload: () => DesktopConnectionConfigInput
  /** Invalidate test/apply gating (call on any input change). */
  invalidateTest: () => void
  /** Run oauth sign-in; resolves true when the session connected. */
  signIn: () => Promise<boolean>
  /** Test the connection; resolves {baseUrl, version} or null on failure. */
  testRemote: () => Promise<null | { baseUrl: string; version: null | string }>
}

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err || 'Unknown error')
}

export function useRemoteConnectionSetup({
  beforeOAuthLogin,
  copy,
  enabled = true,
  hasSavedCredentials = false,
  onError,
  onInvalidate,
  payloadExtras,
  remoteToken,
  remoteUrl,
  savedAuthMode
}: RemoteConnectionSetupOptions): RemoteConnectionSetup {
  const [probeStatus, setProbeStatus] = useState<RemoteProbeStatus>('idle')
  const [probe, setProbe] = useState<DesktopConnectionProbeResult | null>(null)
  const [oauthConnected, setOauthConnected] = useState<boolean>(false)
  const [signingIn, setSigningIn] = useState<boolean>(false)
  const [testing, setTesting] = useState<boolean>(false)
  const [lastTestedPayloadKey, setLastTestedPayloadKey] = useState<null | string>(null)
  const probeSeq = useRef<number>(0)
  const testSeq = useRef<number>(0)

  // Surfaces pass inline arrows for the callbacks; pin them in refs so
  // their per-render identity cannot re-trigger the probe effect (a fresh
  // identity each render would restart the debounce forever and the probe
  // would never settle).
  const onErrorRef = useRef<(message: string) => void>(onError)
  onErrorRef.current = onError
  const onInvalidateRef = useRef<(() => void) | undefined>(onInvalidate)
  onInvalidateRef.current = onInvalidate

  const trimmedUrl: string = coerceRemoteUrlScheme(remoteUrl)

  const invalidateTest = useCallback((): void => {
    testSeq.current += 1
    setTesting(false)
    setLastTestedPayloadKey(null)
    onInvalidateRef.current?.()
  }, [])

  // Auth-mode probe: as the URL changes, ask the gateway (public
  // /api/status) whether it gates with OAuth or a static session token,
  // debounced so typing doesn't spray requests.
  useEffect(() => {
    const seq = ++probeSeq.current

    if (!enabled || !trimmedUrl || !/^https?:\/\//i.test(trimmedUrl)) {
      setProbeStatus('idle')
      setProbe(null)
      setOauthConnected(false)

      return
    }

    const desktop = window.hermesDesktop

    if (!desktop?.probeConnectionConfig) {
      return
    }

    setProbeStatus('probing')

    const timer = window.setTimeout(() => {
      desktop
        .probeConnectionConfig(trimmedUrl)
        .then((result: DesktopConnectionProbeResult) => {
          if (seq !== probeSeq.current) {
            return
          }

          invalidateTest()
          setProbe(result)
          setProbeStatus(result.reachable ? 'done' : 'error')

          if (result.reachable && result.authMode !== 'oauth') {
            setOauthConnected(false)
          }
        })
        .catch((err: unknown) => {
          if (seq !== probeSeq.current) {
            return
          }

          setProbe(null)
          setProbeStatus('error')
          onErrorRef.current(errorMessage(err))
        })
    }, 500)

    return () => window.clearTimeout(timer)
  }, [enabled, invalidateTest, trimmedUrl])

  // Effective auth mode: a resolved probe wins; otherwise the saved
  // config's mode (Settings re-open, no flicker); otherwise token.
  const authMode: RemoteAuthMode =
    probeStatus === 'done' && probe && probe.authMode !== 'unknown'
      ? (probe.authMode as RemoteAuthMode)
      : (savedAuthMode ?? 'token')

  // The auth scheme is KNOWN when the live probe finished, or while idle
  // with saved credentials (their control renders immediately).
  const authResolved: boolean =
    (probeStatus === 'done' && probe?.authMode !== 'unknown') || (probeStatus === 'idle' && hasSavedCredentials)

  const canRetryProbe: boolean = Boolean(trimmedUrl && probeStatus === 'error')

  const canTest: boolean = Boolean(
    trimmedUrl &&
      (canRetryProbe ||
        (authResolved && (authMode === 'oauth' ? oauthConnected || hasSavedCredentials : remoteToken.trim())))
  )

  const payload = useCallback(
    (): DesktopConnectionConfigInput => ({
      mode: 'remote' as const,
      remoteAuthMode: authMode,
      remoteToken: authMode === 'token' ? remoteToken.trim() || undefined : undefined,
      remoteUrl: trimmedUrl,
      ...payloadExtras
    }),
    [authMode, payloadExtras, remoteToken, trimmedUrl]
  )

  const currentPayloadKey: string = JSON.stringify(payload())
  const payloadKeyRef = useRef<string>(currentPayloadKey)
  payloadKeyRef.current = currentPayloadKey
  const canApply: boolean = lastTestedPayloadKey === currentPayloadKey

  const signIn = async (): Promise<boolean> => {
    if (!trimmedUrl) {
      onError(copy.enterUrlFirst)

      return false
    }

    setSigningIn(true)

    try {
      await beforeOAuthLogin?.(trimmedUrl)

      const result = await window.hermesDesktop.oauthLoginConnectionConfig(trimmedUrl)
      invalidateTest()
      setOauthConnected(Boolean(result.connected))

      if (!result.connected) {
        onError(copy.signInIncomplete)
      }

      return Boolean(result.connected)
    } catch (err) {
      onError(errorMessage(err))

      return false
    } finally {
      setSigningIn(false)
    }
  }

  const testRemote = async (): Promise<null | { baseUrl: string; version: null | string }> => {
    const seq = ++testSeq.current
    const testedPayload = payload()
    const testedPayloadKey = JSON.stringify(testedPayload)

    setTesting(true)
    setLastTestedPayloadKey(null)

    try {
      if (!authResolved) {
        // Re-probe first: a failed/unknown probe means we don't yet know
        // which credential control to show, let alone how to test.
        const result = await window.hermesDesktop.probeConnectionConfig(trimmedUrl)

        if (seq !== testSeq.current || testedPayloadKey !== payloadKeyRef.current) {
          return null
        }

        setProbe(result)
        setProbeStatus(result.reachable ? 'done' : 'error')

        if (!result.reachable || result.authMode === 'unknown') {
          onError(result.error || copy.probeError)
        }

        return null
      }

      const result = await window.hermesDesktop.testConnectionConfig(testedPayload)

      if (seq !== testSeq.current || testedPayloadKey !== payloadKeyRef.current) {
        return null
      }

      setLastTestedPayloadKey(testedPayloadKey)

      return { baseUrl: result.baseUrl || trimmedUrl, version: result.version ?? null }
    } catch (err) {
      if (seq === testSeq.current && testedPayloadKey === payloadKeyRef.current) {
        onError(errorMessage(err))
      }

      return null
    } finally {
      if (seq === testSeq.current) {
        setTesting(false)
      }
    }
  }

  return {
    authMode,
    authResolved,
    canApply,
    canTest,
    invalidateTest,
    oauthConnected,
    payload,
    probe,
    probeStatus,
    signingIn,
    signIn,
    testing,
    testRemote,
    trimmedUrl
  }
}
