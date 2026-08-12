// app/connection/modes/remote/panel.tsx — URL, auth detection, credentials.
//
// The single implementation both surfaces render. It leans on
// useRemoteConnectionSetup for the probe debounce, auth-mode resolution and
// oauth sign-in (the same hook Settings and first-run each drove separately
// before), and keeps only presentation here.
//
// Surface differences arrive through props, not branches on the caller:
//   - the saved-token preview only exists once something is saved, so it is
//     keyed off savedConfig rather than off which surface is hosting;
//   - the oauth pre-save is a Settings behaviour (the login window reads the
//     persisted URL) and first-run must persist nothing until Apply, so it
//     comes in as beforeOAuthLogin.

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import type { DesktopAuthProvider } from '@/global'
import { AlertCircle, Check, Loader2, LogIn } from '@/lib/icons'
import { deriveRemoteAuthProviderShape } from '@/lib/desktop-remote-auth'
import { useRemoteConnectionSetup } from '@/lib/use-remote-connection-setup'
import { cn } from '@/lib/utils'

import { CONTROL_TEXT } from '../../../settings/constants'
import { ListRow } from '../../../settings/primitives'
import type { ConnectionPanelProps } from '../../types'
import type { RemoteDraft } from './index'

export function RemotePanel({ draft, onDraftChange, surface }: ConnectionPanelProps<RemoteDraft>) {
  const { copy, envOverride, savedConfig, scope } = surface
  const savedRemote = savedConfig && savedConfig.mode === 'remote' ? savedConfig : null
  const hasSavedCredentials = Boolean(savedRemote?.remoteTokenSet || savedRemote?.remoteOauthConnected)

  const setup = useRemoteConnectionSetup({
    copy: {
      enterUrlFirst: copy.enterUrlFirst,
      probeError: copy.probeError,
      signInIncomplete: copy.signInIncomplete
    },
    hasSavedCredentials,
    onError: surface.onError,
    payloadExtras: { profile: scope ?? undefined },
    remoteToken: draft.token,
    remoteUrl: draft.url,
    savedAuthMode: draft.authMode
  })

  const providers: DesktopAuthProvider[] = setup.probe?.providers ?? []
  const { isPassword, providerLabel } = deriveRemoteAuthProviderShape(providers, copy.identityProvider)

  return (
    <div className="mt-5 grid gap-1">
      <ListRow
        action={
          <Input
            autoComplete="url"
            className={cn('h-8', CONTROL_TEXT)}
            disabled={envOverride}
            onChange={event => {
              setup.invalidateTest()
              onDraftChange({ url: event.target.value })
            }}
            placeholder="https://gateway.example.com/hermes"
            value={draft.url}
          />
        }
        description={copy.remoteUrlDesc}
        title={copy.remoteUrlTitle}
      />

      {setup.probeStatus === 'probing' ? (
        <div className="flex items-center gap-2 py-3 text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">
          <Loader2 className="size-4 animate-spin" />
          {copy.probing}
        </div>
      ) : null}

      {setup.probeStatus === 'error' ? (
        <div className="flex items-start gap-2 py-3 text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">
          <AlertCircle className="mt-0.5 size-4 shrink-0" />
          {setup.probe?.error || copy.probeError}
        </div>
      ) : null}

      {setup.authResolved && setup.authMode === 'oauth' ? (
        <ListRow
          action={
            setup.oauthConnected || savedRemote?.remoteOauthConnected ? (
              <div className="flex items-center gap-2 text-sm text-primary">
                <Check className="size-4" />
                {copy.signedIn}
              </div>
            ) : (
              <Button
                disabled={setup.signingIn || envOverride || !setup.trimmedUrl}
                onClick={() => void setup.signIn()}
              >
                {setup.signingIn ? <Loader2 className="animate-spin" /> : <LogIn />}
                {isPassword ? copy.signIn : copy.signInWith(providerLabel)}
              </Button>
            )
          }
          description={
            setup.oauthConnected || savedRemote?.remoteOauthConnected
              ? isPassword
                ? copy.authSignedInPassword
                : copy.authSignedInOauth
              : isPassword
                ? copy.authNeedsPassword
                : copy.authNeedsOauth(providerLabel)
          }
          title={copy.authTitle}
        />
      ) : null}

      {setup.authResolved && setup.authMode === 'token' ? (
        <ListRow
          action={
            <Input
              autoComplete="off"
              className={cn('h-8 font-mono', CONTROL_TEXT)}
              disabled={envOverride}
              onChange={event => {
                setup.invalidateTest()
                onDraftChange({ token: event.target.value })
              }}
              placeholder={
                savedRemote?.remoteTokenSet
                  ? copy.existingToken(savedRemote.remoteTokenPreview ?? copy.savedToken)
                  : copy.pasteSessionToken
              }
              type="password"
              value={draft.token}
            />
          }
          description={copy.tokenDesc}
          title={copy.tokenTitle}
        />
      ) : null}
    </div>
  )
}
