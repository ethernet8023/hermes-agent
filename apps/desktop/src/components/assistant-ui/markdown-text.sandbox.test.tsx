import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { atom } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { PRIMARY_SESSION_VIEW, SessionViewProvider } from '@/app/chat/session-view'
import { GeneratedImage } from '@/components/chat/generated-image-result'
import { registry } from '@/contrib/registry'
import type { HermesApiRequest } from '@/global'
import { renderMediaTags } from '@/lib/chat-messages/parts'
import { TRANSCRIPT_DIRECTIVE_AREA } from '@/lib/transcript-directives'
import { artifactsForSession } from '@/store/artifacts'
import { invalidateSandboxStatus, publishSandboxStatus, refreshSandboxStatus, sandboxState } from '@/store/sandbox'
import { setSessionOwnerHint, setSessions } from '@/store/session'
import { recordSessionEventScope } from '@/store/session-states'
import type { SandboxStatus, SessionInfo } from '@/types/hermes'

import { DirectiveContent } from './directive-text'
import { InlinePreviewDirective } from './inline-preview-directive'
import { MarkdownTextContent, MessageTextContent } from './markdown-text'

const owner = { connectionId: 'renderer-device', profile: 'renderer-profile' }
const runtimeId = 'renderer-session'
const view = { ...PRIMARY_SESSION_VIEW, $runtimeId: atom<string | null>(runtimeId), $storedId: atom(null) }
const originalDesktop = window.hermesDesktop
const readFileDataUrl = vi.fn(async () => 'data:image/png;base64,cGlj')

const readFileText = vi.fn(async (path: string) => ({
  path,
  text: '<html><script>fetch("https://example.test")</script></html>'
}))

const fetchLinkTitle = vi.fn(async () => 'Remote title')
const status = (enabled: boolean) => ({ enabled, policy: { network: true } }) as SandboxStatus

beforeEach(() => {
  vi.clearAllMocks()
  recordSessionEventScope({ session_id: runtimeId, ...owner })
  publishSandboxStatus(status(true), owner)
  window.hermesDesktop = {
    ...originalDesktop,
    readFileDataUrl,
    readFileText,
    fetchLinkTitle,
    api: vi.fn(async (request: HermesApiRequest) => {
      if (request.path.startsWith('/api/sandbox/status')) {
        return status(false)
      }

      const path = new URL(request.path, 'https://fixture.invalid').searchParams.get('path')!

      if (request.path.startsWith('/api/fs/read-text')) {
        return readFileText(path)
      }

      if (request.path.startsWith('/api/fs/read-data-url')) {
        return readFileDataUrl()
      }

      throw new Error('Unexpected test request')
    })
  } as typeof window.hermesDesktop
})

afterEach(() => {
  cleanup()
  setSessions([])
  window.hermesDesktop = originalDesktop
})

function message(text: string) {
  return render(
    <SessionViewProvider value={view}>
      <MarkdownTextContent isRunning={false} text={text} />
    </SessionViewProvider>
  )
}

describe('model output containment', () => {
  it.each(['failed', 'busy', 'missing-enabled'] as const)('fails closed on %s policy status', async condition => {
    publishSandboxStatus(status(false), owner)
    const state = sandboxState(owner)
    state.set({
      ...state.get(),
      ...(condition === 'failed' ? { confirmed: false, error: new Error('offline') } : {}),
      ...(condition === 'busy' ? { busy: true } : {}),
      ...(condition === 'missing-enabled' ? { status: {} as SandboxStatus } : {})
    })
    const { container } = message('![blocked](/private/unconfirmed.png)')
    await act(async () => {})
    expect(readFileDataUrl).not.toHaveBeenCalled()
    expect(container.querySelector('img')).toBeNull()
    expect(container.textContent).toContain('confirmed')
  })

  it('ignores a pending HTML read after its owner policy tightens', async () => {
    publishSandboxStatus(status(false), owner)
    let finish!: (value: { path: string; text: string }) => void
    readFileText.mockImplementationOnce(
      () =>
        new Promise(resolve => {
          finish = resolve
        })
    )

    const { container } = render(
      <SessionViewProvider value={view}>
        <InlinePreviewDirective attrs={{ file: '/private/pending.html' }} streaming={false} />
      </SessionViewProvider>
    )

    await waitFor(() => expect(readFileText).toHaveBeenCalledWith('/private/pending.html'))
    await act(async () => publishSandboxStatus(status(true), owner))
    await act(async () =>
      finish({ path: '/private/pending.html', text: '<script>fetch("https://example.test")</script>' })
    )
    expect(container.querySelector('iframe')).toBeNull()
    expect(container.textContent).toContain('/private/pending.html')
  })

  it('does not infer an unknown session owner from a confirmed foreground profile', async () => {
    const { confirmNonMxcOwner } = await import('@/test/sandbox')
    confirmNonMxcOwner()
    window.hermesDesktop = {
      ...window.hermesDesktop,
      connections: { list: vi.fn() }
    } as unknown as typeof window.hermesDesktop

    const { container } = render(
      <SessionViewProvider value={{ ...view, $runtimeId: atom('unowned-session') }}>
        <MarkdownTextContent isRunning={false} text="![unknown](/private/unknown.png)" />
      </SessionViewProvider>
    )

    await act(async () => {})
    expect(readFileDataUrl).not.toHaveBeenCalled()
    expect(container.querySelector('img')).toBeNull()
    expect(container.textContent).toContain('owner')
  })
  it('reacts to a changed owner row without reusing the previous profile permission', async () => {
    const a = { connectionId: 'row-a', profile: 'row-profile' }
    const b = { connectionId: 'row-b', profile: 'row-profile' }
    publishSandboxStatus(status(false), a)
    publishSandboxStatus(status(true), b)
    const row = { id: 'row-session', profile: a.profile, connection_id: a.connectionId } as unknown as SessionInfo
    setSessions([row])

    const { container } = render(
      <SessionViewProvider value={{ ...view, $runtimeId: atom(null), $storedId: atom('row-session') }}>
        <MarkdownTextContent isRunning={false} text="![row](https://example.test/row.png)" />
      </SessionViewProvider>
    )

    expect(await screen.findByRole('img')).toBeTruthy()
    await act(async () => setSessions([{ ...row, connection_id: b.connectionId }]))
    expect(container.querySelector('img')).toBeNull()
    expect(container.textContent).toContain('MXC')
  })
  it('does not activate rich code in foreign history even when the foreground policy is off', async () => {
    publishSandboxStatus(status(false), owner)

    const { container } = render(
      <SessionViewProvider value={view}>
        <MarkdownTextContent
          isRunning={false}
          previewOnly
          text={
            '```svg\n<svg xmlns="http://www.w3.org/2000/svg"><image href="https://example.test/foreign.svg" /></svg>\n```'
          }
        />
      </SessionViewProvider>
    )

    await act(async () => {
      await import('@streamdown/code')
      await import('./embeds/svg-embed')
    })
    expect(container.querySelector('svg image')).toBeNull()
    expect(container.textContent).toContain('https://example.test/foreign.svg')
  })
  it('uses the selected stored owner rather than a stale runtime owner on cold replay', async () => {
    publishSandboxStatus(status(false), owner)
    const coldOwner = { connectionId: 'cold-device', profile: 'strict' }
    setSessionOwnerHint('cold-stored', coldOwner)
    publishSandboxStatus(status(true), coldOwner)

    const { container } = render(
      <SessionViewProvider value={{ ...view, $storedId: atom('cold-stored') }}>
        <MarkdownTextContent isRunning={false} text="![cold](/private/cold.png)" />
      </SessionViewProvider>
    )

    await act(async () => {})
    expect(readFileDataUrl).not.toHaveBeenCalled()
    expect(container.querySelector('img')).toBeNull()
    expect(container.textContent).toContain('MXC')
  })

  it('withdraws an existing preview immediately when status becomes unconfirmed', async () => {
    publishSandboxStatus(status(false), owner)
    const { container } = message('![remote](https://example.test/previous.png)')
    expect(await screen.findByRole('img')).toBeTruthy()
    vi.mocked(window.hermesDesktop.api).mockImplementationOnce(() => new Promise(() => {}))
    await act(async () => invalidateSandboxStatus(owner))
    expect(container.querySelector('img')).toBeNull()
    expect(container.textContent).toContain('confirmed')
  })

  it('ignores an old owner response during A to B to A switches and a policy mutation', async () => {
    const a = { connectionId: 'late-a', profile: 'same-name' }
    const b = { connectionId: 'late-b', profile: 'same-name' }
    recordSessionEventScope({ session_id: 'late-session-a', ...a })
    recordSessionEventScope({ session_id: 'late-session-b', ...b })
    const runtime = atom<string | null>('late-session-a')
    let finish!: (value: SandboxStatus) => void

    const api = vi.fn(window.hermesDesktop.api).mockImplementationOnce(
      () =>
        new Promise<SandboxStatus>(resolve => {
          finish = resolve
        })
    )

    window.hermesDesktop = { ...window.hermesDesktop, api } as typeof window.hermesDesktop

    const { container } = render(
      <SessionViewProvider value={{ ...view, $runtimeId: runtime }}>
        <MarkdownTextContent isRunning={false} text="![delayed](/private/delayed.png)" />
      </SessionViewProvider>
    )

    await waitFor(() => expect(api).toHaveBeenCalledTimes(1))
    expect(api.mock.calls[0]).toEqual([expect.objectContaining({ ...a, path: '/api/sandbox/status' })])
    expect(readFileDataUrl).not.toHaveBeenCalled()
    publishSandboxStatus(status(true), b)
    await act(async () => runtime.set('late-session-b'))
    await act(async () => finish(status(false)))
    expect(container.querySelector('img')).toBeNull()
    expect(readFileDataUrl).not.toHaveBeenCalled()
    await act(async () => runtime.set('late-session-a'))
    expect(await screen.findByRole('img')).toBeTruthy()
    readFileDataUrl.mockClear()

    api.mockImplementationOnce(
      () =>
        new Promise<SandboxStatus>(resolve => {
          finish = resolve
        })
    )
    const stale = refreshSandboxStatus(a)
    await act(async () => publishSandboxStatus(status(true), a))
    await act(async () => {
      finish(status(false))
      await stale
    })
    expect(container.querySelector('img')).toBeNull()
    expect(readFileDataUrl).not.toHaveBeenCalled()
  })
  it('never borrows the foreground owner for a static message with no provenance', async () => {
    publishSandboxStatus(status(false), owner)

    const { container } = render(
      <SessionViewProvider value={view}>
        <MessageTextContent text="MEDIA:/private/foreign.png" />
      </SessionViewProvider>
    )

    await act(async () => {})
    expect(readFileDataUrl).not.toHaveBeenCalled()
    expect(container.querySelector('img')).toBeNull()
    expect(container.textContent).toContain('owner')
    expect(container.textContent).toContain('/private/foreign.png')
  })
  it('blocks replayed generated image results but preserves user-uploaded image refs', async () => {
    const { container } = render(
      <SessionViewProvider value={view}>
        <GeneratedImage result={{ host_image: '/private/generated.png' }} />
      </SessionViewProvider>
    )

    await act(async () => {})
    expect(readFileDataUrl).not.toHaveBeenCalled()
    expect(container.querySelector('img')).toBeNull()
    expect(container.textContent).toContain('/private/generated.png')

    render(
      <SessionViewProvider value={view}>
        <DirectiveContent text="@image:/uploads/human.png" />
      </SessionViewProvider>
    )
    await waitFor(() => expect(readFileDataUrl).toHaveBeenCalledWith('/uploads/human.png'))
    expect(await screen.findByRole('img')).toBeTruthy()
  })
  it('does not fetch a link title or mount a remote image, embed, or raw HTML media', async () => {
    const { container } = message(
      'https://example.test/no-host-fetch\n\nhttps://www.youtube.com/watch?v=abc123\n\n![remote](https://example.test/image.png)\n\n<audio src="https://example.test/audio.mp3"></audio>\n\n<video src="https://example.test/video.mp4"></video>\n\n<img src="https://example.test/raw.png" />'
    )

    await act(async () => {})
    expect(fetchLinkTitle).not.toHaveBeenCalled()
    expect(container.querySelector('img, audio, video, iframe, webview, script')).toBeNull()
    expect(container.textContent).toContain('https://example.test/image.png')
  })

  it('leaves HTML and rich fences as inert source, without artifact registration', async () => {
    const source =
      '<!doctype html><html><head><title>Dangerous widget</title></head><body>' +
      '<p>hello</p>'.repeat(80) +
      '<script>fetch("https://example.test")</script></body></html>'

    const { container } = message(
      '```html\n' +
        source +
        '\n```\n\n```svg\n<svg xmlns="http://www.w3.org/2000/svg"><image href="https://example.test/svg.png" /></svg>\n```'
    )

    await waitFor(() => expect(container.textContent).toContain('fetch'))
    expect(container.querySelector('[data-slot="aui_artifact-card"], iframe, img, svg image')).toBeNull()
    expect(artifactsForSession(runtimeId)).toHaveLength(0)
    expect(container.textContent).toContain('MXC')
  })
  it('does not mount an inline HTML file preview or run a registered widget', async () => {
    const widget = vi.fn(() => <iframe srcDoc="<script>fetch('https://example.test')</script>" title="widget" />)

    const dispose = registry.register({
      id: 'test:mxc-widget',
      area: TRANSCRIPT_DIRECTIVE_AREA,
      source: 'plugin:test',
      data: { name: 'widget', render: widget }
    })

    try {
      const { container } = render(
        <SessionViewProvider value={view}>
          <InlinePreviewDirective attrs={{ file: '/private/widget.html' }} streaming={false} />
          <MarkdownTextContent isRunning={false} text={'::widget{file="/private/widget.html"}'} />
        </SessionViewProvider>
      )

      await act(async () => {})
      expect(readFileText).not.toHaveBeenCalled()
      expect(widget).not.toHaveBeenCalled()
      expect(container.querySelector('iframe')).toBeNull()
      expect(container.textContent).toContain('/private/widget.html')
      expect(container.textContent).toContain('MXC')
    } finally {
      act(dispose)
    }
  })
  it.each(['png', 'mp3', 'mp4', 'pdf', 'md', 'html'])(
    'keeps assistant MEDIA %s delivery inert on replay',
    async ext => {
      const path = `/private/result.${ext}`
      const { container } = message(renderMediaTags(`MEDIA:${path}`))
      await act(async () => {})
      expect(readFileDataUrl).not.toHaveBeenCalled()
      expect(container.querySelector('img, audio, video, iframe, a, button')).toBeNull()
      expect(container.textContent).toContain(path)
      expect(container.textContent).toContain('MXC')
    }
  )
  it('does not read a local assistant image under MXC, even with networking enabled', async () => {
    const { container } = message('![Result](/private/result.png)')
    await act(async () => {})
    expect(readFileDataUrl).not.toHaveBeenCalled()
    expect(container.querySelector('img')).toBeNull()
    expect(container.textContent).toContain('/private/result.png')
    expect(container.textContent).toContain('MXC')
  })
})
