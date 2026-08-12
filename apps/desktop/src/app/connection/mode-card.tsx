// app/connection/mode-card.tsx — the shared card grid.
//
// Lifted out of Settings → Gateway so first-run renders the SAME cards
// instead of its own two-button chooser. Availability comes from the
// electron backend registry: an unavailable mode renders disabled with its
// reason in place of the description, never hidden — "this build has no
// local backend" is information the user needs, and a silently missing card
// reads as a bug.

import { Tip } from '@/components/ui/tooltip'
import type { DesktopBackendAvailability } from '@/global'
import { Check, HelpCircle } from '@/lib/icons'
import { selectableCardClass } from '@/lib/selectable-card'
import { cn } from '@/lib/utils'

import type { ConnectionCardContext, ConnectionMode, ConnectionModeCard } from './types'

import { CONNECTION_MODE_MODULES } from './index'

interface ModeCardProps {
  active: boolean
  card: ConnectionModeCard
  description: string
  disabled: boolean
  onSelect: () => void
}

function ModeCard({ active, card, description, disabled, onSelect }: ModeCardProps) {
  const Icon = card.icon

  return (
    <button
      className={cn(
        'flex h-full min-h-0 w-full flex-col p-3 text-left disabled:cursor-not-allowed disabled:opacity-50',
        selectableCardClass({ active, prominent: true })
      )}
      disabled={disabled}
      onClick={onSelect}
      type="button"
    >
      <div className="flex items-center gap-1.5">
        <Icon className="size-3.5 shrink-0 text-muted-foreground" />
        <span className="min-w-0 text-[length:var(--conversation-text-font-size)] font-medium">{card.title}</span>
        {card.hint ? (
          <Tip label={card.hint}>
            <span
              className="grid size-3.5 shrink-0 cursor-help place-items-center text-(--ui-text-tertiary) hover:text-(--ui-text-secondary)"
              onClick={event => event.stopPropagation()}
            >
              <HelpCircle className="size-3.5" />
            </span>
          </Tip>
        ) : null}
        {active ? <Check className="ml-auto size-3.5 shrink-0 text-primary" /> : null}
      </div>
      <p className="mt-1.5 flex-1 text-[length:var(--conversation-caption-font-size)] leading-(--conversation-caption-line-height) text-(--ui-text-tertiary)">
        {description}
      </p>
    </button>
  )
}

export interface ConnectionModeCardsProps {
  context: ConnectionCardContext
  /** The mode whose card reads as selected. */
  selected: ConnectionMode | null
  availabilityFor: (mode: ConnectionMode) => DesktopBackendAvailability
  /** Env vars own the connection: every card is inert. */
  envOverride?: boolean
  onSelect: (mode: ConnectionMode) => void
}

export function ConnectionModeCards({
  availabilityFor,
  context,
  envOverride = false,
  onSelect,
  selected
}: ConnectionModeCardsProps) {
  const unavailableLabel = (availability: DesktopBackendAvailability): null | string => {
    if (availability.available) {
      return null
    }

    return availability.reason === 'light-artifact'
      ? context.copy.modeUnavailableLight
      : context.copy.modeUnavailableSsh
  }

  return (
    <div className="grid auto-rows-fr grid-cols-1 gap-2 sm:grid-cols-2 min-[72rem]:grid-cols-4">
      {CONNECTION_MODE_MODULES.map(module => {
        const availability = availabilityFor(module.mode)
        const card = module.card(context)

        return (
          <ModeCard
            active={selected === module.mode}
            card={card}
            description={unavailableLabel(availability) ?? card.description}
            disabled={envOverride || !availability.available}
            key={module.mode}
            onSelect={() => onSelect(module.mode)}
          />
        )
      })}
    </div>
  )
}
