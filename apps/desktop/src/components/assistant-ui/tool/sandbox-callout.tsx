import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { grantSandboxPath } from '@/hermes'
import { useI18n } from '@/i18n'
import { Loader2, ShieldLock } from '@/lib/icons'
import { appendComposerDraft } from '@/store/composer'
import { notify, notifyError } from '@/store/notifications'
import type { SandboxGrantMode } from '@/types/hermes'

import type { ToolSandboxInfo } from './fallback-model'

const WINDOWS_PATH = /^[A-Za-z]:[\\/]/

// A folder path can be granted; descriptive markers ("the workspace's parent folders …") only
// explain, since their remedy lives in Settings rather than in a policy grant.
function grantable(entry: string): boolean {
  return WINDOWS_PATH.test(entry)
}

/**
 * Shown on a tool card whose command the Windows sandbox refused. It states what was refused
 * and lets the user grant exactly that folder without leaving the conversation; the grant lands
 * in the policy immediately and a retry request is drafted into the composer so one Enter
 * resumes the task.
 */
export function SandboxDenialCallout({ sandbox }: { sandbox: ToolSandboxInfo }) {
  const { t } = useI18n()
  const copy = t.assistant.tool
  const [pending, setPending] = useState<string | null>(null)
  const [granted, setGranted] = useState<Record<string, SandboxGrantMode>>({})

  const grant = async (path: string, mode: SandboxGrantMode) => {
    setPending(`${mode}:${path}`)

    try {
      const result = await grantSandboxPath(path, mode)
      setGranted(prev => ({ ...prev, [path]: mode }))
      notify({ kind: 'success', title: copy.sandboxGrantedTitle, message: copy.sandboxGranted(result.granted) })
      appendComposerDraft(
        copy.sandboxRetryDraft(result.granted, mode === 'readwrite' ? copy.sandboxAllowReadWrite : copy.sandboxAllowRead)
      )
    } catch (err) {
      notifyError(err, copy.sandboxGrantFailed)
    } finally {
      setPending(null)
    }
  }

  return (
    <div
      className="grid gap-1.5 rounded-lg border border-amber-500/30 bg-amber-500/10 px-2.5 py-2 text-xs"
      data-testid="sandbox-denial"
    >
      <div className="flex items-center gap-1.5 font-medium">
        <ShieldLock className="size-3.5 shrink-0" />
        {copy.sandboxBlocked}
      </div>
      {sandbox.denied.map(entry => (
        <div className="grid gap-1" key={entry}>
          <p className="wrap-anywhere text-(--ui-text-secondary)">{copy.sandboxBlockedDetail(entry)}</p>
          {grantable(entry) && !granted[entry] && (
            <div className="flex flex-wrap gap-1.5">
              <Button
                disabled={pending !== null}
                onClick={() => void grant(entry, 'read')}
                size="sm"
                type="button"
                variant="outline"
              >
                {pending === `read:${entry}` && <Loader2 className="size-3 animate-spin" />}
                {copy.sandboxAllowRead}
              </Button>
              <Button
                disabled={pending !== null}
                onClick={() => void grant(entry, 'readwrite')}
                size="sm"
                type="button"
                variant="outline"
              >
                {pending === `readwrite:${entry}` && <Loader2 className="size-3 animate-spin" />}
                {copy.sandboxAllowReadWrite}
              </Button>
            </div>
          )}
        </div>
      ))}
    </div>
  )
}
