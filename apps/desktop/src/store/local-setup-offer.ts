/**
 * The local-setup offer: "this machine can run a model locally" for every
 * machine that qualifies, delivered by state transitions, never by a timer.
 *
 * Three surfaces read this one record:
 *
 * - The handoff tour ends on the model pill (`signpost.ts`) when the machine
 *   qualifies. Every guided user who reaches the handoff sees it.
 * - A card above the primary composer after the first finished turn once
 *   onboarding is over (handoff or skip). It renders only while that session
 *   is idle, so an automatic follow-up turn hides it and the next idle shows it.
 * - A "Run locally" row at the top of the model menu, until setup is done.
 *
 * States move only on events: `armed` when onboarding ends, `shown` on the
 * first eligible `message.complete`, `dismissed` on ✕, `accepted` on the card's
 * button. Local setup completing ends it by eligibility: a machine with a
 * runtime and a staged model no longer qualifies, so every surface hides.
 *
 * The predecessor was a tip in the rotation: 5-10 min settle, a six-hour
 * cooldown shared with every tip, a quiet check, a 22 s linger, and a 7-day
 * reshow. It fired on rtxspark and vespyr with nobody watching and recorded
 * itself as shown, which hid it for a week.
 */

import { atom, computed } from 'nanostores'

import { getLocalCatalog, getLocalModelsStatus } from '@/hermes'
import { isOnboardingEnabled } from '@/lib/onboarding-enabled'
import { Codecs, persistentAtom } from '@/lib/persisted'
import type { LocalCatalogModel, LocalModelsStatus } from '@/types/hermes'

import { $localModelsEnabled } from './local-models-flag'
import { $onboardingGate, type OnboardingPhase } from './onboarding-gate'
import { $connection } from './session'

export type LocalSetupOfferState = 'accepted' | 'armed' | 'dismissed' | 'shown' | 'unarmed'

interface OfferRecord {
  armedBy: null | string
  /** ISO time of the last transition, for `state()` readouts. */
  at: null | string
  /** Runtime id of the session whose finished turn showed the card (diagnostics only). */
  sessionId: null | string
  state: LocalSetupOfferState
}

const OFFER_STATES: readonly LocalSetupOfferState[] = ['accepted', 'armed', 'dismissed', 'shown', 'unarmed']
const EMPTY: OfferRecord = { armedBy: null, at: null, sessionId: null, state: 'unarmed' }

/** Persisted as a flat string record so `Codecs.stringRecord` does the parsing at the storage boundary. */
function toRecord(stored: Record<string, string>): OfferRecord {
  const state = OFFER_STATES.find(candidate => candidate === stored.state)

  return state
    ? { armedBy: stored.armedBy ?? null, at: stored.at ?? null, sessionId: stored.sessionId ?? null, state }
    : EMPTY
}

function toStored(record: OfferRecord): Record<string, string> {
  return Object.fromEntries(Object.entries(record).filter((entry): entry is [string, string] => entry[1] !== null))
}

/** Desktop-global like the tip ledgers: the offer is about this machine, not a profile. */
const $stored = persistentAtom<Record<string, string>>('hermes.desktop.offers.local-setup.v1', {}, Codecs.stringRecord)

export const $localSetupOffer = computed($stored, toRecord)

function setRecord(record: OfferRecord): void {
  $stored.set(toStored(record))
}

/** The recommended catalog row that fits, when this machine qualifies. */
export interface LocalSetupFit {
  model: LocalCatalogModel
}

interface Eligibility {
  checkedAt: number
  fit: LocalSetupFit | null
  reason: string
  /** A failed read or a connection not up yet: shown in `state()` but never cached, so the next read retries. */
  transient?: boolean
}

/** Session cache: eligibility is a fact about the backend's machine, so a connection change drops it. */
export const $localSetupEligibility = atom<Eligibility | null>(null)

let eligibilityRead: Promise<Eligibility> | null = null

export function pickLocalSetupFit(
  connectionMode: null | string,
  status: LocalModelsStatus | null,
  catalog: readonly LocalCatalogModel[] | null
): Eligibility {
  const at = Date.now()

  if (connectionMode === null) {
    return { checkedAt: at, fit: null, reason: 'connection not established yet', transient: true }
  }

  if (connectionMode !== 'local') {
    return { checkedAt: at, fit: null, reason: `connection is ${connectionMode}, not local` }
  }

  if (!status || !catalog) {
    return { checkedAt: at, fit: null, reason: 'no local-models status or catalog' }
  }

  if (status.runtime_installed && status.models.length > 0) {
    return { checkedAt: at, fit: null, reason: 'local models already set up' }
  }

  const fitting = catalog.filter(model => model.fits)
  const model = fitting.find(candidate => candidate.recommended) ?? fitting[0]

  return model
    ? { checkedAt: at, fit: { model }, reason: `fits ${model.id}` }
    : { checkedAt: at, fit: null, reason: 'no catalog model fits this machine' }
}

/** One status + catalog read per connection; a transient miss is retried on the next read. */
export function readLocalSetupEligibility(): Promise<Eligibility> {
  const cached = $localSetupEligibility.get()

  return cached && !cached.transient ? Promise.resolve(cached) : refreshLocalSetupEligibility()
}

/** Read again, keeping the current answer on screen until the new one lands. */
export function refreshLocalSetupEligibility(): Promise<Eligibility> {
  if (!$localModelsEnabled.get()) {
    const off = { checkedAt: Date.now(), fit: null, reason: 'local models are off in this build' }
    $localSetupEligibility.set(off)

    return Promise.resolve(off)
  }

  eligibilityRead ??= Promise.all([getLocalModelsStatus(), getLocalCatalog()])
    .then(([status, catalog]) => pickLocalSetupFit($connection.get()?.mode ?? null, status, catalog.models))
    .catch((error: Error) => ({
      checkedAt: Date.now(),
      fit: null,
      reason: `eligibility read failed: ${error.message}`,
      transient: true
    }))
    .then(result => {
      $localSetupEligibility.set(result)
      eligibilityRead = null

      return result
    })

  return eligibilityRead
}

/** Drop the cached answer (connection changed; debug reset). */
export function invalidateLocalSetupEligibility(): void {
  eligibilityRead = null
  $localSetupEligibility.set(null)
}

$connection.listen(() => invalidateLocalSetupEligibility())

function transition(state: LocalSetupOfferState, patch: Partial<OfferRecord>): void {
  setRecord({ ...$localSetupOffer.get(), ...patch, at: new Date().toISOString(), state })
}

/**
 * Guided onboarding ending (either way) arms the offer. A build without guided
 * onboarding arms at boot. With it on, `idle`/`cinematic`/`guided`/`handoff`
 * wait: the card must not land on top of the guide.
 */
function armFromPhase(phase: OnboardingPhase): void {
  if ($localSetupOffer.get().state !== 'unarmed') {
    return
  }

  if (phase === 'done' || phase === 'skipped') {
    transition('armed', { armedBy: `onboarding:${phase}` })
  } else if (!isOnboardingEnabled()) {
    transition('armed', { armedBy: 'no-guided-onboarding' })
  }
}

$onboardingGate.subscribe(gate => armFromPhase(gate.phase))

export interface TurnCompleteSignal {
  failed: boolean
  sessionId: null | string
}

/**
 * `message.complete` for the session the user is looking at. The whole agent
 * loop has returned at this point (tool calls and interim messages come before
 * it), so this is the end of a task, not a step in one. Errored turns do not
 * count; the caller filters subagent mirrors by only reporting the active session.
 */
export function reportLocalSetupTurnComplete({ failed, sessionId }: TurnCompleteSignal): void {
  if (failed || !sessionId || $localSetupOffer.get().state !== 'armed') {
    return
  }

  void readLocalSetupEligibility().then(({ fit }) => {
    if (fit && $localSetupOffer.get().state === 'armed') {
      transition('shown', { sessionId })
    }
  })
}

export function dismissLocalSetupOffer(): void {
  transition('dismissed', {})
}

export function acceptLocalSetupOffer(): void {
  transition('accepted', {})
}

/** The card is live: shown, still eligible. The composer adds "and this session is idle". */
export const $localSetupCardLive = computed(
  [$localSetupOffer, $localSetupEligibility],
  (offer, eligibility) => offer.state === 'shown' && Boolean(eligibility?.fit)
)

/** The model-menu row stays until setup completes, whatever happened to the card. */
export const $localSetupRowFit = computed($localSetupEligibility, eligibility => eligibility?.fit ?? null)

/** Asks the model pill that owns the active composer to open its menu, where the row sits on top. */
export const $localSetupMenuRequest = atom(0)

export function requestLocalSetupMenu(): void {
  $localSetupMenuRequest.set($localSetupMenuRequest.get() + 1)
}

// ── Debug handle ────────────────────────────────────────────────────────────
// Ships in packaged builds on purpose: rtxspark/vespyr run MSIX bundles we
// drive over CDP, and `state()` is how a run that showed nothing explains itself.

interface LocalSetupOfferDebug {
  force: () => Promise<Eligibility>
  reset: () => void
  state: () => {
    eligibility: Eligibility | null
    localModelsEnabled: boolean
    offer: OfferRecord
    onboardingPhase: OnboardingPhase
  }
}

declare global {
  interface Window {
    __hermesTips?: LocalSetupOfferDebug
  }
}

window.__hermesTips = {
  /** Skip onboarding and waiting: mark the card shown now, if the machine qualifies. */
  force: async () => {
    invalidateLocalSetupEligibility()
    const result = await readLocalSetupEligibility()

    if (result.fit) {
      transition('shown', { armedBy: 'debug:force', sessionId: 'debug' })
    }

    return result
  },
  reset: () => {
    setRecord(EMPTY)
    invalidateLocalSetupEligibility()
    armFromPhase($onboardingGate.get().phase)
  },
  state: () => ({
    eligibility: $localSetupEligibility.get(),
    localModelsEnabled: $localModelsEnabled.get(),
    offer: $localSetupOffer.get(),
    onboardingPhase: $onboardingGate.get().phase
  })
}
