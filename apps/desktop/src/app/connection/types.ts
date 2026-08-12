// app/connection/types.ts — contracts for the desktop's connection-mode
// modules.
//
// One folder per mode lives under ./modes (local, remote, cloud, ssh); the
// registry in ./index.ts is the single ordered list every surface walks. The
// renderer mirror of electron/backends: a module is PURE — it imports no
// electron bridge, owns no persistence policy, and raises no toasts.
// Everything impure (how a commit is performed, how an error is shown)
// arrives through ConnectionSurface, implemented by the host surface.
//
// Two hosts implement it today:
//   - Settings → Gateway: cards plus the selected mode's panel inline,
//     scoped to a profile, committing through save/apply.
//   - First-run setup: cards drilling into one panel with a Back button, no
//     profile scope, committing through the first-run gate.
//
// Nothing here names a mode's fields. A mode declares its own draft type in
// its own folder and is the only code that can read or write it, so a new
// mode is a new folder plus a registry line — never an edit to a shared
// field bag. The surface holds one draft PER mode (keyed, not merged) so
// switching cards keeps a half-typed URL without letting the ssh panel reach
// the cloud org.

import { createElement, type ReactNode } from 'react'

import type { DesktopBackendAvailability, DesktopConnectionConfig, DesktopConnectionConfigInput } from '@/global'
import type { Translations } from '@/i18n'
import type { Monitor } from '@/lib/icons'

export const CONNECTION_MODES = ['local', 'remote', 'cloud', 'ssh'] as const

export type ConnectionMode = (typeof CONNECTION_MODES)[number]

/**
 * The copy a mode module may use: the gateway slice, plus the two strings
 * that live under boot.failure because the boot-failure surface needed them
 * first. Modules declare their need through this type and the host assembles
 * it with connectionCopy() — cheaper and less error-prone than duplicating
 * two keys across five locale files.
 */
export type ConnectionCopy = Translations['settings']['gateway'] & {
  /** The oauth window closed before authentication finished. */
  signInIncomplete: string
  /** Fallback name for an unidentified identity provider. */
  identityProvider: string
  /** First-run's label for committing local mode: it installs, not connects. */
  localInstallAction: string
  /** First-run's description of what installing locally does. */
  localInstallDesc: string
  /** First-run's label for committing a remote-like mode. */
  remoteApplyAction: string
  /** Leave a drilled-into mode and return to the card grid. */
  back: string
}

/** Assemble ConnectionCopy from a translations bundle. One place, both hosts. */
export function connectionCopy(t: Translations): ConnectionCopy {
  return {
    ...t.settings.gateway,
    signInIncomplete: t.boot.failure.signInIncompleteMessage,
    identityProvider: t.boot.failure.identityProvider,
    localInstallAction: t.install.installLocalTitle,
    localInstallDesc: t.install.installLocalDesc,
    remoteApplyAction: t.install.applyRemote,
    back: t.install.backToSetup
  }
}

/**
 * Which host is rendering. Panels are otherwise identical between the two;
 * this is for the few controls that genuinely exist on only one of them (a
 * saved-token preview means nothing before anything is saved; a
 * profile-scoped ssh field means nothing before profiles exist).
 */
export type ConnectionSurfaceKind = 'first-run' | 'settings'

/**
 * How a panel hands its result back. `apply` is the real difference between
 * the surfaces: Settings writes the config and soft-rehomes, while first-run
 * runs the installer for local and resumes the gated startup for every other
 * mode. A panel asks for the outcome and never learns which it got.
 */
export interface ConnectionCommit {
  /** Persist without reconnecting. Absent where there is nothing to defer. */
  save?: () => Promise<void>
  apply: () => Promise<void>
  busy: boolean
}

/**
 * The impure powers a mode panel may use, implemented by the host surface.
 * Panels never import the electron bridge and never decide how a failure is
 * presented.
 */
export interface ConnectionSurface {
  kind: ConnectionSurfaceKind
  /** null = the global/default connection; a name = that profile's override. */
  scope: null | string
  /** The saved config for this scope, or null before anything is saved. */
  savedConfig: DesktopConnectionConfig | null
  /** This mode's availability from the electron backend registry. */
  availability: DesktopBackendAvailability
  /** Env vars are driving the connection: every control renders read-only. */
  envOverride: boolean
  copy: ConnectionCopy
  commit: ConnectionCommit
  /** Presentation of failures — inline row or toast, the surface decides. */
  onError: (message: string) => void
  /** Presentation of successes. Surfaces that show nothing omit it. */
  onSuccess?: (message: string) => void
  /**
   * Runs before an oauth login window opens. Settings persists the URL and
   * mode here because the login window reads the saved config; first-run
   * deliberately persists nothing until Apply, so backing out of setup
   * leaves no trace. Omitted means "nothing to do first".
   */
  beforeOAuthLogin?: (trimmedUrl: string) => Promise<void>
}

/** What a mode looks like in the card grid. */
export interface ConnectionModeCard {
  icon: typeof Monitor
  title: string
  description: string
  /** Optional tooltip for a caveat that should not bloat the description. */
  hint?: string
}

/** What a module needs to write its card, without a live surface. */
export interface ConnectionCardContext {
  kind: ConnectionSurfaceKind
  scope: null | string
  copy: ConnectionCopy
}

export interface ConnectionConfigPanelProps<Draft> {
  draft: Draft
  onDraftChange: (patch: Partial<Draft>) => void
  surface: ConnectionSurface
}

/**
 * A connection mode: identity, its own draft shape, and how it presents.
 * `Draft` is the mode's private state — declared in the mode's folder and
 * named nowhere else. Every mode has a config panel: even local, whose only
 * content is the commit action, because a drilled-into card must have a body
 * and the action's meaning is the surface's to decide.
 */
export interface ConnectionModeModule<M extends ConnectionMode, Draft> {
  mode: M
  /** A fresh draft: what the panel shows before anything is typed or loaded. */
  emptyDraft: () => Draft
  /** Rehydrate this mode's draft from a saved config (never from another's). */
  fromSaved: (config: DesktopConnectionConfig | null) => Draft
  /** Serialize to the IPC payload. The wire shape stops here. */
  toPayload: (draft: Draft, scope: null | string) => DesktopConnectionConfigInput
  card: (context: ConnectionCardContext) => ConnectionModeCard
  ConfigPanel: (props: ConnectionConfigPanelProps<Draft>) => ReactNode
}

/**
 * A module with its draft type erased, so modules with different drafts can
 * live in one ordered registry. Every method takes the opaque `ConnectionDraft`
 * handle that the draft store hands back for the same mode — pairing a draft
 * with the wrong module is the only way to misuse this, and the store keys by
 * mode precisely so that cannot happen.
 */
export type ConnectionDraft = { readonly __connectionDraft: unique symbol }

export interface ErasedConnectionModule {
  mode: ConnectionMode
  emptyDraft: () => ConnectionDraft
  fromSaved: (config: DesktopConnectionConfig | null) => ConnectionDraft
  toPayload: (draft: ConnectionDraft, scope: null | string) => DesktopConnectionConfigInput
  card: (context: ConnectionCardContext) => ConnectionModeCard
  renderPanel: (props: ConnectionConfigPanelProps<ConnectionDraft>) => ReactNode
}

/**
 * Erase a module's draft type for the registry. THE one place drafts are
 * cast: a module is fully typed where it is defined and where a caller
 * recovers it, and the store guarantees the draft passed back in is the one
 * this module produced. Written as a function so every mode is erased the
 * same way instead of each registry entry casting on its own.
 */
export function defineConnectionMode<M extends ConnectionMode, Draft>(
  module: ConnectionModeModule<M, Draft>
): ErasedConnectionModule {
  const asDraft = (draft: Draft): ConnectionDraft => draft as unknown as ConnectionDraft
  const asTyped = (draft: ConnectionDraft): Draft => draft as unknown as Draft

  return {
    mode: module.mode,
    emptyDraft: () => asDraft(module.emptyDraft()),
    fromSaved: config => asDraft(module.fromSaved(config)),
    toPayload: (draft, scope) => module.toPayload(asTyped(draft), scope),
    card: module.card,
    renderPanel: ({ draft, onDraftChange, surface }) => {
      // createElement, NOT Panel({...}): calling a component as a function
      // runs its hooks inside the CALLER's hook list, so switching to a mode
      // whose panel has a different number of hooks throws "rendered more
      // hooks than during the previous render". An element makes each panel
      // its own component with its own hook list and its own mount/unmount.
      return createElement(module.ConfigPanel as (props: ConnectionConfigPanelProps<unknown>) => ReactNode, {
        draft: asTyped(draft),
        onDraftChange: patch => onDraftChange(patch as Partial<ConnectionDraft>),
        surface,
        // Remounting on a mode switch is the point: a panel must not inherit
        // the previous mode's local state.
        key: module.mode
      } as ConnectionConfigPanelProps<unknown> & { key: string })
    }
  }
}
