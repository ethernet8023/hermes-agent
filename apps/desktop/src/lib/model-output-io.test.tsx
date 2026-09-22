// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { setApiRequestConnection, setApiRequestProfile } from '@/api/client'
import { LocalFilePreview } from '@/app/chat/right-rail/preview-file'
import { InlinePreviewDirective } from '@/components/assistant-ui/inline-preview-directive'
import { MarkdownImage } from '@/components/assistant-ui/markdown-text'
import { ModelOutputOwnerProvider } from '@/components/assistant-ui/model-output-policy'
import { GeneratedImage } from '@/components/chat/generated-image-result'
import { PreviewAttachment } from '@/components/chat/preview-attachment'
import type { HermesApiRequest } from '@/global'
import { en } from '@/i18n/en'
import { $previewTabs } from '@/store/preview'
import { publishSandboxStatus } from '@/store/sandbox'
import { $connection } from '@/store/session'
import type { SandboxStatus } from '@/types/hermes'

import { readDesktopFileDataUrl, readDesktopFileText, writeDesktopFileText } from './desktop-fs'
import { normalizeOrLocalPreviewTarget } from './local-preview'
import { downloadGatewayMediaFile, resolveMediaDisplaySrc, resolveMediaPlaybackSrc } from './media'

it.each([
  ['C:/Other/report.md', 'C:/Other/report.md'],
  [String.raw`C:\Other\report.md`, String.raw`C:\Other\report.md`],
  [String.raw`\\server\share\report.md`, String.raw`\\server\share\report.md`],
  ['//server/share/report.md', '//server/share/report.md'],
  ['~/report.md', '~/report.md'],
  [String.raw`~\report.md`, String.raw`~\report.md`],
  ['/other/report.md', '/other/report.md'],
  ['relative/report.md', 'C:/Workspace/relative/report.md']
])('preserves the request path and owner when previewing %s', async (raw, expected) => {
  const owner = { ...a, sessionId: 'owned-stored' }
  const target = await normalizeOrLocalPreviewTarget(raw, 'C:/Workspace', owner)
  const requests = api.mock.calls.filter(([request]) => request.path.startsWith('/api/fs/read-text'))
  expect(requests).toHaveLength(1)
  const request = requests[0][0]
  expect(new URL(request.path, 'https://fixture.invalid').searchParams.get('path')).toBe(expected)
  expect(request).toMatchObject(a)
  expect(target).toMatchObject({ path: expected, modelOwner: owner })
})

it('carries proven session identity for relative model files instead of the server cwd', async () => {
  await resolveMediaDisplaySrc('relative.png', { ...a, sessionId: 'owned-stored' })
  const request = api.mock.calls.find(([r]) => r.path.startsWith('/api/fs/'))![0]
  expect(new URL(request.path, 'https://fixture.invalid').searchParams.get('session_id')).toBe('owned-stored')
})

it('refuses session-relative model reads when no session provenance was supplied', async () => {
  await expect(resolveMediaDisplaySrc('relative.png', a)).rejects.toThrow()
  expect(api.mock.calls.filter(([r]) => r.path.startsWith('/api/fs/'))).toHaveLength(0)
})

it.each([
  ['text', () => readDesktopFileText('relative.html', a)],
  ['stream', () => resolveMediaPlaybackSrc('relative.mp3', a)],
  ['download', () => downloadGatewayMediaFile('relative.pdf', undefined, a)]
] as const)('refuses unowned relative %s paths without borrowing an ambient cwd', async (_kind, read) => {
  await expect(read()).rejects.toThrow()
  expect(api.mock.calls.filter(([r]) => r.path.startsWith('/api/fs/'))).toHaveLength(0)
  expect(saveGatewayFile).not.toHaveBeenCalled()
})

it('rechecks live owner policy at each model-selected IO, including a cached permissive owner', async () => {
  api.mockImplementationOnce(async () => status(true))
  await expect(readDesktopFileDataUrl('/new/private.png', a)).rejects.toThrow()
  expect(api.mock.calls.filter(([r]) => r.path.startsWith('/api/fs/'))).toHaveLength(0)
  expect(nativeRead).not.toHaveBeenCalled()
})

it('discards bytes completing after their owner policy was revoked', async () => {
  let finish!: (value: string) => void
  const previous = api.getMockImplementation()!
  api.mockImplementation(request =>
    request.path.startsWith('/api/sandbox/status')
      ? Promise.resolve(status(false))
      : new Promise(resolve => {
          finish = resolve
        })
  )
  const read = readDesktopFileDataUrl('/pending/private.png', a)

  const outcome = read.then(
    value => value,
    error => error
  )

  await waitFor(() => expect(finish).toBeTypeOf('function'))
  publishSandboxStatus(status(true), a)
  finish('data:image/png;base64,eA==')
  const result = await outcome
  api.mockImplementation(previous)
  expect(result).toBeInstanceOf(Error)
})

it('pins generated images and preview/download actions through the same model owner', async () => {
  const normalize = vi.fn(async () => null)
  window.hermesDesktop = { ...window.hermesDesktop, normalizePreviewTarget: normalize }

  const view = render(
    <ModelOutputOwnerProvider value={a}>
      <GeneratedImage result={{ image: '/same/generated.png' }} />
      <PreviewAttachment source="explicit-link" target="/same/report.txt" />
    </ModelOutputOwnerProvider>
  )

  await act(async () => {})
  fireEvent.click(view.getByRole('button', { name: en.fileMenu.download }))
  await waitFor(() => expect(saveGatewayFile).toHaveBeenCalled())
  expect(saveGatewayFile).toHaveBeenCalledWith(expect.objectContaining(a))
  fireEvent.click(view.getByRole('button', { name: en.preview.openPreview }))
  await waitFor(() => expect($previewTabs.get().some(tab => tab.target.source === '/same/report.txt')).toBe(true))
  expect(normalize).not.toHaveBeenCalled()
  const target = $previewTabs.get().find(tab => tab.target.source === '/same/report.txt')!.target
  view.unmount()
  const preview = render(<LocalFilePreview reloadKey={0} target={target} />)
  await act(async () => {})

  for (const [request] of api.mock.calls) {
    expect(request).toMatchObject(a)
  }

  act(() => publishSandboxStatus(status(true), a))
  expect(preview.container.textContent).not.toContain('owned document')
  $previewTabs.set([])
})

it.each(['markdown', 'generated'] as const)(
  'does not expose a %s remote image before fresh owner confirmation',
  async kind => {
    api.mockImplementationOnce(async () => status(true))

    const view = render(
      <ModelOutputOwnerProvider value={a}>
        {kind === 'markdown' ? (
          <MarkdownImage src="https://untrusted.invalid/image.png" />
        ) : (
          <GeneratedImage result={{ image: 'https://untrusted.invalid/image.png' }} />
        )}
      </ModelOutputOwnerProvider>
    )

    expect(view.container.querySelector('img')).toBeNull()
    await act(async () => {})
    expect(view.container.querySelector('img')).toBeNull()
  }
)

it('keeps an edited model preview on its owner and rechecks image download clicks', async () => {
  await writeDesktopFileText('/same/edit.txt', 'user edit', a)
  expect(api.mock.calls.find(([r]) => r.method === 'POST')?.[0]).toMatchObject(a)
})

it('rechecks image download clicks after external policy changes', async () => {
  const saveImageFromUrl = vi.fn(async () => true)
  window.hermesDesktop = { ...window.hermesDesktop, saveImageFromUrl }

  const view = render(
    <ModelOutputOwnerProvider value={a}>
      <MarkdownImage src="https://untrusted.invalid/image.png" />
    </ModelOutputOwnerProvider>
  )

  await waitFor(() => expect(view.container.querySelector('img')).toBeTruthy())
  api.mockImplementationOnce(async () => status(true))
  await act(async () => fireEvent.click(view.getByRole('button', { name: en.desktop.downloadImage })))
  expect(saveImageFromUrl).not.toHaveBeenCalled()
})

it('removes previous owner bytes synchronously when a permitted leaf is rehomed', async () => {
  publishSandboxStatus(status(false), b)

  const view = render(
    <ModelOutputOwnerProvider value={a}>
      <MarkdownImage src="/same/file.png" />
    </ModelOutputOwnerProvider>
  )

  await waitFor(() => expect(view.container.querySelector('img')).toBeTruthy())
  view.rerender(
    <ModelOutputOwnerProvider value={b}>
      <MarkdownImage src="/same/file.png" />
    </ModelOutputOwnerProvider>
  )
  expect(view.container.querySelector('img')).toBeNull()
  await act(async () => {})
})

it('preserves a confirmed non-MXC generated image external fallback without downloading from an unrelated gateway', async () => {
  const openExternal = vi.fn(async () => {})
  window.hermesDesktop = { ...window.hermesDesktop, openExternal }
  const url = 'https://untrusted.invalid/failed.png'

  const view = render(
    <ModelOutputOwnerProvider value={a}>
      <GeneratedImage result={{ image: url }} />
    </ModelOutputOwnerProvider>
  )

  await waitFor(() => expect(view.container.querySelector('img')).not.toBeNull())
  fireEvent.error(view.container.querySelector('img')!)
  await act(async () => fireEvent.click(view.getByRole('link')))
  expect(openExternal).toHaveBeenCalledWith(url)
  expect(saveGatewayFile).not.toHaveBeenCalled()
})

it('withdraws the previous executable document immediately when its owner changes', async () => {
  publishSandboxStatus(status(false), b)
  const frame = <InlinePreviewDirective attrs={{ file: '/same/frame.html' }} streaming={false} />
  const view = render(<ModelOutputOwnerProvider value={a}>{frame}</ModelOutputOwnerProvider>)
  await waitFor(() => expect(view.container.querySelector('iframe')).not.toBeNull())
  view.rerender(<ModelOutputOwnerProvider value={b}>{frame}</ModelOutputOwnerProvider>)
  expect(view.container.querySelector('iframe')).toBeNull()
  await act(async () => {})
})

it('uses native streaming only after resolving the explicitly owned local backend', async () => {
  const local = { connectionId: 'local', profile: 'local-profile' }
  publishSandboxStatus(status(false), local)
  const getConnectionFor = vi.fn(async () => ({ mode: 'local' }))
  window.hermesDesktop = { ...window.hermesDesktop, getConnectionFor } as unknown as typeof window.hermesDesktop
  const url = await resolveMediaPlaybackSrc('/same/audio.mp3', local)
  expect(new URL(url).hostname).toBe('stream')
  expect(getConnectionFor).toHaveBeenCalledWith(expect.objectContaining(local))
})

const original = window.hermesDesktop
const a = { connectionId: 'model-io-a', profile: 'alpha' }
const b = { connectionId: 'model-io-b', profile: 'beta' }
const status = (enabled: boolean) => ({ enabled }) as SandboxStatus

const api = vi.fn(async (request: HermesApiRequest): Promise<unknown> => {
  if (request.path.startsWith('/api/sandbox/status')) {
    return status(false)
  }

  if (request.path.startsWith('/api/fs/read-text')) {
    return { text: '<p>owned document</p>' }
  }

  return 'data:image/png;base64,eA=='
})

const nativeRead = vi.fn(async () => 'data:image/png;base64,eA==')
const saveGatewayFile = vi.fn(async () => ({ saved: true }))

beforeEach(() => {
  api.mockClear()
  nativeRead.mockClear()
  saveGatewayFile.mockClear()
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null)
  window.hermesDesktop = {
    ...original,
    api,
    readFileDataUrl: nativeRead,
    saveGatewayFile,
    getConnectionFor: vi.fn(async () => ({ mode: 'remote' }))
  } as unknown as typeof original
  setApiRequestConnection(b.connectionId)
  setApiRequestProfile(b.profile)
  $connection.set({ mode: 'remote', ...b } as never)
  publishSandboxStatus(status(false), a)
  publishSandboxStatus(status(true), b)
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  window.hermesDesktop = original
  $connection.set(null)
})

it('routes model image and inline HTML reads through the proven owner, not foreground B', async () => {
  render(
    <ModelOutputOwnerProvider value={a}>
      <MarkdownImage src="/same/file.png" />
      <InlinePreviewDirective attrs={{ file: '/same/file.html' }} streaming={false} />
    </ModelOutputOwnerProvider>
  )
  await waitFor(() => expect(api.mock.calls.filter(([r]) => r.path.startsWith('/api/fs/'))).toHaveLength(2))

  for (const [request] of api.mock.calls) {
    expect(request).toMatchObject(a)
  }

  expect(nativeRead).not.toHaveBeenCalled()
})

it('pins media streams and downloads to the supplied model owner', async () => {
  const stream = new URL(await resolveMediaPlaybackSrc('/same/audio.mp3', a))
  expect(stream.searchParams.get('connectionId')).toBe(a.connectionId)
  expect(stream.searchParams.get('profile')).toBe(a.profile)
  await downloadGatewayMediaFile('/same/report.pdf', { sessionId: 'stored-a', ...a }, a)
  expect(saveGatewayFile).toHaveBeenCalledWith(expect.objectContaining({ ...a, sessionId: 'stored-a' }))
  await act(async () => {})
})
