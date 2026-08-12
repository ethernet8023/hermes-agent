import { describe, expect, it } from 'vitest'

import { enrichSelectedSshHost, selectSshHost } from './host-selection'

import type { SshDraft } from './index'

const draft: SshDraft = {
  host: 'linux-box',
  user: 'operator',
  port: 2222,
  keyPath: '/keys/linux',
  remoteHermesPath: '/opt/hermes',
  remoteProfile: ''
}

describe('selectSshHost', () => {
  it('clears host-specific fields when the selected host changes', () => {
    expect(selectSshHost(draft, 'mac-box')).toEqual({
      host: 'mac-box',
      user: '',
      port: null,
      keyPath: '',
      remoteHermesPath: '',
      remoteProfile: ''
    })
  })

  it('preserves the draft when reselecting the same host', () => {
    expect(selectSshHost(draft, draft.host)).toBe(draft)
  })

  it('enriches only the host that produced the ssh config result', () => {
    const selected = selectSshHost(draft, 'mac-box')

    expect(
      enrichSelectedSshHost(selected, 'mac-box', {
        identityFile: '~/.ssh/id_ed25519',
        port: 22,
        user: 'hermes'
      })
    ).toMatchObject({
      host: 'mac-box',
      user: 'hermes',
      port: null,
      keyPath: '~/.ssh/id_ed25519'
    })
    expect(enrichSelectedSshHost(draft, 'mac-box', { user: 'wrong' })).toBe(draft)
  })

  it('never overwrites a value the user already typed', () => {
    expect(
      enrichSelectedSshHost(draft, 'linux-box', { identityFile: '/keys/other', port: 2200, user: 'someone-else' })
    ).toMatchObject({ user: 'operator', port: 2222, keyPath: '/keys/linux' })
  })
})
