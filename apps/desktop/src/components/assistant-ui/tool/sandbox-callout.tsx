import { useStore } from '@nanostores/react'
import { useEffect, useState } from 'react'

import { getSandboxGrantTarget, grantSandboxPath, type SandboxOwner, sandboxOwnerKey } from '@/api/sandbox'
import { requestComposerInsert } from '@/app/chat/composer/focus'
import { useComposerScope } from '@/app/chat/composer/scope'
import { useSessionSandboxOwner } from '@/app/chat/composer/use-sandbox-owner'
import { useSessionView } from '@/app/chat/session-view'
import { Button } from '@/components/ui/button'
import { useI18n } from '@/i18n'
import { Loader2, ShieldLock } from '@/lib/icons'
import { stashSessionDraft, takeSessionDraft } from '@/store/composer'
import { notify, notifyError } from '@/store/notifications'
import { mutateSandbox, sandboxState } from '@/store/sandbox'
import type { SandboxGrantMode } from '@/types/hermes'

import type { ToolSandboxInfo } from './fallback-model'

const WINDOWS_PATH = /^[A-Za-z]:[\\/]/

// A folder path can be granted; descriptive markers ("the workspace's parent folders …") only
// explain; ancestor ACL preparation is not a policy grant and is never offered here.
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
  const owner = useSessionSandboxOwner()
  const copy = useI18n().t.assistant.tool

  return (
    <div
      className="grid gap-1.5 rounded-lg border border-(--ui-stroke-tertiary) px-2.5 py-2 text-xs"
      data-testid="sandbox-denial"
    >
      <div className="flex items-center gap-1.5 font-medium">
        <ShieldLock className="size-3.5 shrink-0" />
        {copy.sandboxBlocked}
      </div>
      {sandbox.denied.map(entry => (
        <div className="grid gap-1" key={entry}>
          <p className="wrap-anywhere text-(--ui-text-secondary)">{copy.sandboxBlockedDetail(entry)}</p>
          {owner && grantable(entry) && (
            <SandboxGrantRow entry={entry} key={`${sandboxOwnerKey(owner)}:${entry}`} owner={owner} />
          )}
        </div>
      ))}
    </div>
  )
}

function SandboxGrantRow({ entry, owner }: { entry: string; owner: SandboxOwner }) {
  const { t } = useI18n()
  const copy = t.assistant.tool
  const [pending, setPending] = useState<SandboxGrantMode | null>(null)
  const [granted, setGranted] = useState<SandboxGrantMode | null>(null)
  const [target, setTarget] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const { busy } = useStore(sandboxState(owner))
  const view = useSessionView()
  const { target: composerTarget } = useComposerScope()
  useEffect(() => {
    let current = true
    void getSandboxGrantTarget(entry, owner)
      .then(preview => {
        if (current && preview.recursive === true) {
          setTarget(preview.target)
        }
      })
      .catch(err => {
        if (current) {
          setError(String(err))
        }
      })

    return () => {
      current = false
    }
  }, [entry, owner])

  const grant = async (mode: SandboxGrantMode) => {
    if (!target) {
      return
    }

    const sessionId = view.$runtimeId.get()
    const storedId = view.$storedId.get()
    setPending(mode)

    try {
      const result = await mutateSandbox(owner, () => grantSandboxPath(target, mode, owner))
      setGranted(mode)
      notify({ kind: 'success', title: copy.sandboxGrantedTitle, message: copy.sandboxGranted(result.granted) })

      const text = copy.sandboxRetryDraft(
        result.granted,
        mode === 'readwrite' ? copy.sandboxAllowReadWrite : copy.sandboxAllowRead
      )

      const handled = await requestComposerInsert(text, {
        target: composerTarget,
        sessionId,
        sessionKey: sessionId ? undefined : storedId,
        focus: false
      })

      if (!handled && storedId) {
        const draft = takeSessionDraft(storedId)
        stashSessionDraft(storedId, `${draft.text}${draft.text ? '\n\n' : ''}${text}`, draft.attachments)
      }
    } catch (err) {
      notifyError(err, copy.sandboxGrantFailed)
    } finally {
      setPending(null)
    }
  }

  return (
    <div className="grid gap-1">
      {error && <p role="status">{error}</p>}
      {target && (
        <>
          <p className="wrap-anywhere text-(--ui-text-secondary)">{copy.sandboxRecursiveScope(target)}</p>
          {granted && (
            <p role="status">
              {copy.sandboxGranted(target)} · {granted === 'read' ? copy.sandboxAllowRead : copy.sandboxAllowReadWrite}
            </p>
          )}
          {granted !== 'readwrite' && (
            <div className="flex flex-wrap gap-1.5">
              <Button
                disabled={busy || pending !== null || granted === 'read'}
                onClick={() => void grant('read')}
                size="sm"
                type="button"
                variant="outline"
              >
                {pending === 'read' && <Loader2 className="size-3 animate-spin" />}
                {copy.sandboxAllowRead}
              </Button>
              <Button
                disabled={busy || pending !== null}
                onClick={() => void grant('readwrite')}
                size="sm"
                type="button"
                variant="outline"
              >
                {pending === 'readwrite' && <Loader2 className="size-3 animate-spin" />}
                {copy.sandboxAllowReadWrite}
              </Button>
            </div>
          )}
        </>
      )}
    </div>
  )
}
