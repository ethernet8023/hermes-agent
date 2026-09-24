/** The local campaign tips' reshow clock. */

import { describe, expect, it } from 'vitest'

import { LOCAL_TIP_RESHOW_MS, localSetupDue } from '@/lib/tips/local-cta'

describe('localSetupDue', () => {
  it('is due when never shown', () => {
    expect(localSetupDue(Date.now(), undefined)).toBe(true)
  })

  it('holds for a week after an ignored showing, then returns', () => {
    const now = Date.now()

    expect(localSetupDue(now, now - LOCAL_TIP_RESHOW_MS + 1000)).toBe(false)
    expect(localSetupDue(now, now - LOCAL_TIP_RESHOW_MS)).toBe(true)
  })
})
