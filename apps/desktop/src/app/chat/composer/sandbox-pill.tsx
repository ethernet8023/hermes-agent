import { useStore } from '@nanostores/react'
import { useCallback, useEffect, useRef, useState } from 'react'

import { useSessionView } from '@/app/chat/session-view'
import { Button } from '@/components/ui/button'
import { Popover, PopoverAnchor, PopoverContent } from '@/components/ui/popover'
import { useI18n } from '@/i18n'
import { displayPath } from '@/lib/display-path'
import { ShieldLock } from '@/lib/icons'
import { cn } from '@/lib/utils'
import { notifyError } from '@/store/notifications'
import { requestRoute } from '@/store/recovery-requests'
import { $sandboxStatus, refreshSandboxStatus, toggleSandbox } from '@/store/sandbox'

import { ACTIVE_ICON_BTN, GHOST_ICON_BTN } from './control-classes'

const SANDBOX_SETTINGS_ROUTE = '/settings?tab=config:safety'
const OPEN_DELAY_MS = 150
const CLOSE_DELAY_MS = 250

/**
 * Composer sandbox indicator: whether commands from THIS conversation run inside a Windows
 * (MXC) container, with a hover card that says what that means for the folder in front of the
 * user and offers the way into the policy. Rendered only where the backend reports the sandbox as
 * available (a Windows host with the MXC kit and shell); the verdict comes from `$sandboxStatus`,
 * the cache of the backend's status route, so the pill and the Settings panel can never disagree.
 */
export function SandboxPill({ disabled }: { disabled: boolean }) {
  const copy = useI18n().t.composer.sandbox
  const status = useStore($sandboxStatus)
  const view = useSessionView()
  const cwd = useStore(view.$cwd)
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const timer = useRef<number | null>(null)

  const schedule = useCallback((next: boolean, delay: number) => {
    if (timer.current !== null) {
      window.clearTimeout(timer.current)
    }

    timer.current = window.setTimeout(() => setOpen(next), delay)
  }, [])

  useEffect(
    () => () => {
      if (timer.current !== null) {
        window.clearTimeout(timer.current)
      }
    },
    []
  )

  useEffect(() => {
    if (open) {
      void refreshSandboxStatus()
    }
  }, [open])

  // Present only where the Settings panel would read "Available": a Windows host with the MXC kit
  // and the sandbox shell in place. Elsewhere there is nothing to turn on, so nothing to show.
  if (!status?.platform_supported || !status.available) {
    return null
  }

  const enabled = status.enabled
  const title = enabled ? copy.titleOn : copy.titleOff

  const hoverProps = {
    onMouseEnter: () => schedule(true, OPEN_DELAY_MS),
    onMouseLeave: () => schedule(false, CLOSE_DELAY_MS)
  }

  // Click flips the sandbox; the card is hover-only, so it explains without getting in the way.
  const toggle = async () => {
    if (busy) {
      return
    }

    setBusy(true)

    try {
      await toggleSandbox()
    } catch (err) {
      notifyError(err, enabled ? copy.turnOffFailed : copy.turnOnFailed)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Popover onOpenChange={setOpen} open={open}>
      <PopoverAnchor asChild>
        <Button
          aria-label={title}
          aria-pressed={enabled}
          className={cn('relative', GHOST_ICON_BTN, enabled && ACTIVE_ICON_BTN)}
          data-state-sandbox={enabled ? 'on' : 'off'}
          data-testid="sandbox-pill"
          disabled={disabled || busy}
          onClick={() => void toggle()}
          size="icon"
          type="button"
          variant="ghost"
          {...hoverProps}
        >
          {/* A closed blue-to-orange ring in the Nous colours: the sandbox being on is the one
              state in the composer worth more than a tint, and a ring stays whole when motion
              is off, where the travelling arc would freeze part-way. */}
          {enabled && <span aria-hidden className="sandbox-ring" data-testid="sandbox-pill-arc" />}
          <ShieldLock className="size-3.5" />
        </Button>
      </PopoverAnchor>
      <PopoverContent
        align="end"
        className="w-72 p-3"
        onCloseAutoFocus={event => event.preventDefault()}
        onOpenAutoFocus={event => event.preventDefault()}
        side="top"
        sideOffset={8}
        {...hoverProps}
      >
        <div className="grid gap-2">
          <div className="flex items-center justify-between gap-2">
            <span className="text-sm font-medium">{copy.heading}</span>
            <span
              className={cn(
                'rounded px-1.5 py-0.5 text-[0.65rem] font-medium uppercase tracking-wide',
                enabled ? 'bg-primary/10 text-primary' : 'bg-muted text-muted-foreground'
              )}
              data-testid="sandbox-pill-state"
            >
              {enabled ? copy.on : copy.off}
            </span>
          </div>
          <p className="text-xs text-muted-foreground">
            {enabled ? copy.descriptionOn(displayPath(cwd) || cwd) : copy.descriptionOff}
          </p>
          {enabled && (
            <p className="text-xs text-muted-foreground">
              {status.policy?.network ? copy.networkOn : copy.networkOff} {copy.isolated}
            </p>
          )}
          <Button
            className="justify-self-start"
            onClick={() => {
              setOpen(false)
              requestRoute(SANDBOX_SETTINGS_ROUTE)
            }}
            size="sm"
            type="button"
            variant="outline"
          >
            {copy.openSettings}
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  )
}
