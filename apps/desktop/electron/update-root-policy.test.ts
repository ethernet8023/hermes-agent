import assert from 'node:assert/strict'

import { test } from 'vitest'

import { classifyUpdateRoot, unmanagedCheckoutMessage } from './update-root-policy'
import type { ClassifyUpdateRootDeps } from './update-root-policy'

const deps = (overrides: Partial<ClassifyUpdateRootDeps> = {}): ClassifyUpdateRootDeps => ({
  isGitCheckout: () => true,
  readStamp: () => ({ updateMechanism: 'self' }),
  ...overrides
})

test('a checkout with a self stamp is managed, wherever it sits', () => {
  assert.equal(classifyUpdateRoot('/home/u/.hermes/hermes-agent', deps()), 'managed-checkout')
  assert.equal(classifyUpdateRoot('/weird/custom/--dir/install', deps()), 'managed-checkout')
})

test('a checkout without a stamp is unmanaged', () => {
  const d = deps({ readStamp: () => null })
  assert.equal(classifyUpdateRoot('/home/u/src/hermes-agent', d), 'unmanaged-checkout')
  assert.equal(classifyUpdateRoot('/home/u/.hermes/hermes-agent', d), 'unmanaged-checkout')
})

test('an artifact stamp (non-self mechanism) does not make a checkout managed', () => {
  const external = deps({ readStamp: () => ({ updateMechanism: 'external' }) })
  const updater = deps({ readStamp: () => ({ updateMechanism: 'electron-updater' }) })
  assert.equal(classifyUpdateRoot('/home/u/.hermes/hermes-agent', external), 'unmanaged-checkout')
  assert.equal(classifyUpdateRoot('/home/u/.hermes/hermes-agent', updater), 'unmanaged-checkout')
})

test('a mechanism-less stamp is unmanaged (never guess)', () => {
  const d = deps({ readStamp: () => ({}) })
  assert.equal(classifyUpdateRoot('/home/u/.hermes/hermes-agent', d), 'unmanaged-checkout')
})

test('no .git means not a git checkout, stamp or not', () => {
  const d = deps({ isGitCheckout: () => false })
  assert.equal(classifyUpdateRoot('/home/u/.hermes/hermes-agent', d), 'not-a-git-checkout')
  assert.equal(classifyUpdateRoot('/home/u/src/hermes-agent', d), 'not-a-git-checkout')
})

test('the refusal message names the root and points at git', () => {
  const message = unmanagedCheckoutMessage('/home/u/src/hermes-agent')
  assert.match(message, /\/home\/u\/src\/hermes-agent/)
  assert.match(message, /git pull/)
})
