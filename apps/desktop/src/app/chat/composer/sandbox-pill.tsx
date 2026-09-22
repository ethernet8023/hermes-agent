import { useStore } from '@nanostores/react'
import { useCallback, useEffect, useRef, useState } from 'react'

import { currentSandboxOwner, type SandboxOwner, sandboxOwnerKey } from '@/api/sandbox'
import { useSessionView } from '@/app/chat/session-view'
import { Button } from '@/components/ui/button'
import { Popover, PopoverAnchor, PopoverContent } from '@/components/ui/popover'
import { useI18n } from '@/i18n'
import { displayPath } from '@/lib/display-path'
import { ShieldLock } from '@/lib/icons'
import { cn } from '@/lib/utils'
import { notifyError } from '@/store/notifications'
import { ensureGatewayAgent } from '@/store/profile'
import { requestRoute } from '@/store/recovery-requests'
import { refreshSandboxStatus, sandboxState, toggleSandbox } from '@/store/sandbox'
import { setSettingsScope } from '@/store/settings-scope'

import { ACTIVE_ICON_BTN, GHOST_ICON_BTN } from './control-classes'
import { useSessionSandboxOwner } from './use-sandbox-owner'

const SANDBOX_SETTINGS_ROUTE = '/settings?tab=config:safety'
const OPEN_DELAY_MS = 150
const CLOSE_DELAY_MS = 250

/**
 * Composer sandbox indicator: whether commands from THIS conversation run inside a Windows
 * (MXC) container, with a hover card that says what that means for the folder in front of the
 * user and offers the way into the policy. Rendered only where the backend reports the sandbox as
 * available or configured on; the verdict comes from the owner-scoped sandbox cache,
 * the cache of the backend's status route, so the pill and the Settings panel can never disagree.
 */
export function SandboxPill({ disabled }: { disabled: boolean }) {
  const owner = useSessionSandboxOwner()

  return owner ? <ScopedSandboxPill disabled={disabled} owner={owner} /> : null
}

function ScopedSandboxPill({ disabled, owner }: { disabled: boolean; owner: SandboxOwner }) {
  const copy = useI18n().t.composer.sandbox
  const { status, busy, confirmed } = useStore(sandboxState(owner))
  const view = useSessionView()
  const cwd = useStore(view.$cwd)
  const [open, setOpen] = useState(false)

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
      void refreshSandboxStatus(owner)
    }
  }, [open, owner])

  useEffect(() => {
    if (!sandboxState(owner).get().status) {
      void refreshSandboxStatus(owner)
    }
  }, [owner])

  // Present only where the Settings panel would read "Available": a Windows host with the MXC kit
  // and the sandbox shell in place. Elsewhere there is nothing to turn on, so nothing to show.
  if (!status || (!status.enabled && (!status.platform_supported || (!status.available && !status.shell_missing)))) {
    return null
  }

  const enabled = status.enabled
  const protectedNow = enabled && status.available && confirmed
  const title = enabled ? copy.titleOn : copy.titleOff

  const hoverProps = {
    onMouseEnter: () => schedule(true, OPEN_DELAY_MS),
    onMouseLeave: () => schedule(false, CLOSE_DELAY_MS)
  }

  // Click flips policy; hover or keyboard focus reveals the explanation without taking focus.
  const toggle = async () => {
    if (busy) {
      return
    }

    try {
      await toggleSandbox(owner)
    } catch (err) {
      notifyError(err, enabled ? copy.turnOffFailed : copy.turnOnFailed)
    }
  }

  const openSettings = async () => {
    try {
      if (sandboxOwnerKey(currentSandboxOwner()) !== sandboxOwnerKey(owner)) {
        await ensureGatewayAgent(owner.connectionId, owner.profile ?? 'default')
      }

      if (sandboxOwnerKey(currentSandboxOwner()) !== sandboxOwnerKey(owner)) {
        return
      }

      setSettingsScope(owner.profile ?? 'default')
      setOpen(false)
      requestRoute(SANDBOX_SETTINGS_ROUTE)
    } catch (err) {
      notifyError(err, copy.openSettings)
    }
  }

  return (
    <Popover onOpenChange={setOpen} open={open}>
      <PopoverAnchor asChild>
        <Button
          aria-expanded={open}
          aria-haspopup="dialog"
          aria-label={title}
          aria-pressed={enabled}
          className={cn('relative', GHOST_ICON_BTN, enabled && ACTIVE_ICON_BTN)}
          data-state-sandbox={
            !confirmed ? 'unknown' : enabled && !status.available ? 'unavailable' : enabled ? 'on' : 'off'
          }
          data-testid="sandbox-pill"
          disabled={disabled || busy || (!enabled && !confirmed)}
          onClick={() => void toggle()}
          onFocus={() => schedule(true, 0)}
          onKeyDown={event => {
            if (event.key === 'Escape') {
              event.preventDefault()
              setOpen(false)
            }
          }}
          size="icon"
          type="button"
          variant="ghost"
          {...hoverProps}
        >
          {/* A closed blue-to-orange ring in the Nous colours: the sandbox being on is the one
              state in the composer worth more than a tint, and a ring stays whole when motion
              is off, where the travelling arc would freeze part-way. */}
          {protectedNow && <span aria-hidden className="sandbox-ring" data-testid="sandbox-pill-arc" />}
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
              {!confirmed
                ? copy.unknown
                : enabled && !status.available
                  ? copy.unavailable
                  : enabled
                    ? copy.on
                    : copy.off}
            </span>
          </div>
          <p className="text-xs text-muted-foreground">
            {enabled ? copy.descriptionOn(displayPath(cwd) || cwd) : copy.descriptionOff}
          </p>
          {status.reason && !status.available && <p className="text-xs text-muted-foreground">{status.reason}</p>}
          {enabled && (
            <p className="text-xs text-muted-foreground">
              {status.policy?.network ? copy.networkOn : copy.networkOff} {copy.isolated}
            </p>
          )}
          <Button
            className="justify-self-start"
            onClick={() => void openSettings()}
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
