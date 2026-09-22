import { atom } from 'nanostores'

import { getSandboxStatus, type SandboxOwner, sandboxOwnerKey, updateSandboxPolicy } from '@/api/sandbox'
import type { SandboxStatus } from '@/types/hermes'

export interface SandboxSnapshot {
  status: SandboxStatus | null
  confirmed: boolean
  busy: boolean
  error: unknown
}

// One backend cache shared by Settings and every conversation, never an ambient policy atom.
const entries = new Map<string, { state: ReturnType<typeof atom<SandboxSnapshot>>; generation: number; read: number }>()

function entry(owner: SandboxOwner) {
  const key = sandboxOwnerKey(owner)
  let value = entries.get(key)

  if (!value) {
    value = {
      state: atom<SandboxSnapshot>({ status: null, confirmed: false, busy: false, error: null }),
      generation: 0,
      read: 0
    }
    entries.set(key, value)
  }

  return value
}

export const sandboxState = (owner: SandboxOwner) => entry(owner).state

/** Cached UI state is not authority for a new model-selected host read.
 * Recheck the owning backend, then fence its asynchronous completion. */
export async function confirmModelOutputAccess(owner: SandboxOwner | null): Promise<() => void> {
  if (!owner) {
    throw new Error('Model output owner is unknown')
  }

  const cache = entry(owner)
  const generation = cache.generation
  const status = await refreshSandboxStatus(owner)

  const check = () => {
    const current = cache.state.get()

    if (
      cache.generation !== generation ||
      !current.confirmed ||
      current.busy ||
      status?.enabled !== false ||
      current.status?.enabled !== false
    ) {
      throw new Error('Model output policy does not allow host IO')
    }
  }

  check()

  return check
}

// Visible executable output must also observe edits from CLI/other windows.
// One poll per owner, regardless of the number of transcript leaves.
const observers = new Map<string, { count: number; stop: () => void }>()

export function observeSandboxStatus(owner: SandboxOwner): () => void {
  const key = sandboxOwnerKey(owner)
  let observer = observers.get(key)

  if (!observer) {
    let reading = false

    const refresh = () => {
      if (reading || document.visibilityState === 'hidden') {
        return
      }

      reading = true
      void refreshSandboxStatus(owner).finally(() => {
        reading = false
      })
    }

    const resume = () => {
      invalidateSandboxStatus(owner)
      refresh()
    }

    const timer = setInterval(refresh, 4000)
    window.addEventListener('focus', resume)
    document.addEventListener('visibilitychange', resume)
    observer = {
      count: 0,
      stop: () => {
        clearInterval(timer)
        window.removeEventListener('focus', resume)
        document.removeEventListener('visibilitychange', resume)
      }
    }
    observers.set(key, observer)
  }

  observer.count++

  return () => {
    if (--observer.count === 0) {
      observer.stop()
      observers.delete(key)
    }
  }
}

export function invalidateSandboxStatus(owner: SandboxOwner): void {
  const cache = entry(owner)
  cache.generation++
  cache.state.set({ ...cache.state.get(), confirmed: false })
}

export function publishSandboxStatus(status: SandboxStatus, owner: SandboxOwner): void {
  const cache = entry(owner)
  cache.generation++
  cache.state.set({ status, confirmed: true, busy: false, error: null })
}

/** Writes fence earlier reads, and exclude overlapping edits from all surfaces of this owner. */
export async function mutateSandbox<T extends SandboxStatus>(owner: SandboxOwner, work: () => Promise<T>): Promise<T> {
  const cache = entry(owner)

  if (cache.state.get().busy) {
    throw new Error('A sandbox policy update is already in progress.')
  }

  const generation = ++cache.generation
  cache.state.set({ ...cache.state.get(), busy: true, confirmed: false, error: null })

  try {
    const status = await work()

    if (cache.generation === generation) {
      cache.state.set({ status, confirmed: true, busy: false, error: null })
    }

    return status
  } catch (error) {
    if (cache.generation === generation) {
      cache.state.set({ ...cache.state.get(), confirmed: false, error })
    }

    throw error
  } finally {
    cache.state.set({ ...cache.state.get(), busy: false })
  }
}

export async function toggleSandbox(owner: SandboxOwner): Promise<SandboxStatus | null> {
  const current = sandboxState(owner).get().status

  if (!current) {
    return null
  }

  return mutateSandbox(owner, () => updateSandboxPolicy({ enabled: !current.enabled }, owner))
}

/** A failed read retains last-known configuration, but never a confirmed protection ring. */
export async function refreshSandboxStatus(
  owner: SandboxOwner,
  options: Parameters<typeof getSandboxStatus>[1] = {}
): Promise<SandboxStatus | null> {
  const cache = entry(owner)

  if (cache.state.get().busy) {
    return null
  }

  const generation = cache.generation
  const read = ++cache.read

  try {
    const status = await getSandboxStatus(owner, options)

    if (cache.generation === generation && cache.read === read) {
      if (cache.state.get().status?.enabled === false && status?.enabled !== false) {
        cache.generation++
      }

      cache.state.set({ status, confirmed: true, busy: false, error: null })
    }

    return status
  } catch (error) {
    if (cache.generation === generation && cache.read === read) {
      cache.state.set({ ...cache.state.get(), confirmed: false, error })
    }

    return null
  }
}
