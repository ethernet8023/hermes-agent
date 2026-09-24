import { useStore } from '@nanostores/react'
import { useEffect } from 'react'

import { useSessionView } from '@/app/chat/session-view'
import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { useI18n } from '@/i18n'
import {
  $localSetupCardLive,
  $localSetupEligibility,
  $localSetupOffer,
  acceptLocalSetupOffer,
  dismissLocalSetupOffer,
  readLocalSetupEligibility,
  requestLocalSetupMenu
} from '@/store/local-setup-offer'

interface LocalSetupCardProps {
  busy: boolean
  guidedChat: boolean
}

/**
 * "This could run on your computer" strip above the input, after the first
 * finished turn once onboarding is over (store/local-setup-offer.ts). The offer
 * is about the machine, not the chat, so it sits in the primary composer (not
 * split tiles) and survives a relaunch until ✕ or setup. Only while that session
 * is idle: an automatic follow-up turn hides it, the next idle brings it back.
 * Offers, never hijacks: no focus move; "Show me" opens the model menu on click.
 */
export function LocalSetupCard({ busy, guidedChat }: LocalSetupCardProps) {
  const primary = useSessionView().kind === 'primary'
  const live = useStore($localSetupCardLive)
  const offer = useStore($localSetupOffer)
  const fit = useStore($localSetupEligibility)?.fit
  const { t } = useI18n()
  const copy = t.composer.localSetup
  // A card shown before a relaunch or reconnect has no eligibility cached yet: read it once for this state.
  useEffect(() => {
    if (offer.state === 'shown') {
      void readLocalSetupEligibility()
    }
  }, [offer.state])

  if (!primary || !live || !fit || busy || guidedChat) {
    return null
  }

  return (
    <div
      className="flex items-center justify-between gap-2 rounded-lg border border-[color-mix(in_srgb,var(--dt-composer-ring)_32%,transparent)] bg-accent/18 px-2 py-1.5"
      data-slot="composer-local-setup"
      role="status"
    >
      <div className="flex min-w-0 items-center gap-2">
        <Codicon className="shrink-0 text-(--ui-accent)" name="chip" size="0.85rem" />
        <div className="min-w-0 text-[0.72rem] leading-snug">
          <div className="font-medium text-foreground">{copy.title}</div>
          <div className="truncate text-muted-foreground/88">{copy.text(fit.model.display_name)}</div>
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-1">
        <Button
          className="h-6 rounded-md px-2 text-[0.68rem]"
          onClick={() => {
            acceptLocalSetupOffer()
            requestLocalSetupMenu()
          }}
          type="button"
          variant="secondary"
        >
          {copy.action}
        </Button>
        <Button
          aria-label={t.common.close}
          className="h-6 rounded-md px-2 text-[0.68rem]"
          onClick={dismissLocalSetupOffer}
          type="button"
          variant="ghost"
        >
          ×
        </Button>
      </div>
    </div>
  )
}
