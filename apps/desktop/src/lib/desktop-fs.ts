import { hermesApi } from '@/api/client'
import type { SandboxOwner } from '@/api/sandbox'
import type {
  HermesConnection,
  HermesReadDirResult,
  HermesReadFileTextResult,
  HermesSelectPathsOptions
} from '@/global'
import { translateNow } from '@/i18n'
import { confirmModelOutputAccess } from '@/store/sandbox'
import { $connection } from '@/store/session'

export interface DesktopFsRemotePicker {
  selectPaths: (options?: HermesSelectPathsOptions) => Promise<string[]>
}

let remotePicker: DesktopFsRemotePicker | null = null

export function setDesktopFsRemotePicker(next: DesktopFsRemotePicker | null) {
  remotePicker = next
}

function connectionCacheKey(connection: HermesConnection | null) {
  if (!connection) {
    return 'local:'
  }

  // A profile belongs to a registry connection, not the whole Desktop. The
  // registry id is the isolation boundary, including for SSH connections; the
  // stable host identity below is only the fallback for legacy connections.
  if (connection.connectionId) {
    return `connection:${connection.connectionId}:${connection.profile || ''}`
  }

  const target =
    connection.remoteKind === 'ssh'
      ? connection.remoteIdentity || connection.remoteHost || ''
      : connection.baseUrl || ''

  return `${connection.mode || 'local'}:${connection.remoteKind || ''}:${connection.profile || ''}:${target}`
}

export function desktopFsCacheKey(connection: HermesConnection | null = $connection.get()) {
  return connectionCacheKey(connection)
}

export function isDesktopFsRemoteMode() {
  return $connection.get()?.mode === 'remote'
}

// Active profile for FS/git REST calls. Without it the Electron api bridge
// hits the primary (local) backend even when the user switched to a remote profile.
export function desktopFsProfile(): string | undefined {
  return $connection.get()?.profile || undefined
}

function fsPath(endpoint: string, filePath: string) {
  return `/api/fs/${endpoint}?path=${encodeURIComponent(filePath)}`
}

function bridge() {
  const desktop = window.hermesDesktop

  if (!desktop) {
    throw new Error('Hermes Desktop bridge is unavailable')
  }

  return desktop
}

function remoteFsApi<T>(path: string, body?: Record<string, unknown>): Promise<T> {
  return hermesApi<T>(
    body ? { body, method: 'POST', path, profile: desktopFsProfile() } : { path, profile: desktopFsProfile() }
  )
}

export async function readDesktopDir(path: string): Promise<HermesReadDirResult> {
  if (!isDesktopFsRemoteMode()) {
    return bridge().readDir(path)
  }

  return remoteFsApi<HermesReadDirResult>(fsPath('list', path))
}

export function isOwnerAbsoluteFilePath(path: string): boolean {
  return /^(?:\/|[a-z]:[\\/]|\\\\|~[\\/])/i.test(path)
}

export async function readDesktopFileText(
  path: string,
  owner?: SandboxOwner | null
): Promise<HermesReadFileTextResult> {
  if (owner !== undefined) {
    if (!owner) {
      throw new Error('Model output owner is unknown')
    }

    const check = await confirmModelOutputAccess(owner)

    if (!isOwnerAbsoluteFilePath(path)) {
      throw new Error('Model text preview requires its session-resolved path')
    }

    const result = await hermesApi<HermesReadFileTextResult>({
      connectionId: owner.connectionId ?? undefined,
      profile: owner.profile ?? 'default',
      path: fsPath('read-text', path)
    })

    check()

    return result
  }

  if (!isDesktopFsRemoteMode()) {
    return bridge().readFileText(path)
  }

  return remoteFsApi<HermesReadFileTextResult>(fsPath('read-text', path))
}

// Save UTF-8 text back to a file. Local writes go through the hardened Electron
// IPC; remote writes hit the dashboard's POST /api/fs/write-text (same path
// hardening, parent-must-exist, size cap) so the editor behaves identically in
// both modes. Stale-on-disk detection is the caller's job (re-read before save).
export async function writeDesktopFileText(
  path: string,
  content: string,
  owner?: SandboxOwner
): Promise<{ path: string }> {
  if (owner) {
    const check = await confirmModelOutputAccess(owner)

    const result = await hermesApi<{ path?: string }>({
      connectionId: owner.connectionId ?? undefined,
      profile: owner.profile ?? 'default',
      path: '/api/fs/write-text',
      method: 'POST',
      body: { path, content }
    })

    check()

    return { path: result.path || path }
  }

  const desktop = bridge()

  if (!isDesktopFsRemoteMode()) {
    if (!desktop.writeTextFile) {
      throw new Error('Saving is not available')
    }

    return desktop.writeTextFile(path, content)
  }

  const result = await remoteFsApi<{ ok?: boolean; path?: string }>('/api/fs/write-text', { content, path })

  return { path: result.path || path }
}

export async function readDesktopFileDataUrl(path: string, owner?: SandboxOwner | null): Promise<string> {
  if (owner !== undefined) {
    if (!owner) {
      throw new Error('Model output owner is unknown')
    }

    const check = await confirmModelOutputAccess(owner)

    if (!owner.sessionId && !isOwnerAbsoluteFilePath(path)) {
      throw new Error('Relative model output requires a proven session')
    }

    const result = await hermesApi<string | { dataUrl?: string }>({
      connectionId: owner.connectionId ?? undefined,
      profile: owner.profile ?? 'default',
      path:
        fsPath('read-data-url', path) + (owner.sessionId ? `&session_id=${encodeURIComponent(owner.sessionId)}` : '')
    })

    check()

    return typeof result === 'string' ? result : result.dataUrl || ''
  }

  if (!isDesktopFsRemoteMode()) {
    return bridge().readFileDataUrl(path)
  }

  const result = await remoteFsApi<string | { dataUrl?: string }>(fsPath('read-data-url', path))

  return typeof result === 'string' ? result : result.dataUrl || ''
}

/**
 * Read a composer image local-shell first, even when the active agent is
 * remote. Picker, clipboard, and OS-drop paths belong to this machine; in-app
 * project-tree paths may belong only to the gateway and fall back there.
 */
export async function readDesktopFileDataUrlLocalFirst(path: string): Promise<string> {
  try {
    const local = await window.hermesDesktop?.readFileDataUrl?.(path)

    if (local) {
      return local
    }
  } catch (error) {
    if (!isDesktopFsRemoteMode()) {
      throw error
    }

    // Not on this machine (or unreadable locally) — try the active gateway.
  }

  return readDesktopFileDataUrl(path)
}

export async function desktopGitRoot(path: string, owner?: SandboxOwner): Promise<string | null> {
  if (owner) {
    const check = await confirmModelOutputAccess(owner)

    const result = await hermesApi<{ root: string | null }>({
      connectionId: owner.connectionId ?? undefined,
      profile: owner.profile ?? 'default',
      path: fsPath('git-root', path)
    })

    check()

    return result.root
  }

  const desktop = bridge()

  if (!isDesktopFsRemoteMode()) {
    return desktop.gitRoot ? desktop.gitRoot(path) : null
  }

  return (await remoteFsApi<{ root: string | null }>(fsPath('git-root', path))).root
}

export async function desktopDefaultCwd(): Promise<{ branch: string; cwd: string } | null> {
  if (!isDesktopFsRemoteMode()) {
    return null
  }

  return remoteFsApi<{ branch: string; cwd: string }>('/api/fs/default-cwd')
}

// Reveal a path in the OS file manager (Finder / Explorer / Files). Local only.
// The bridge answers `false` when the path is not on this computer (a remote
// backend's workspace) — surface it instead of a silent no-op.
export async function revealDesktopPath(path: string): Promise<void> {
  const revealed = await bridge().revealPath?.(path)

  if (revealed === false) {
    throw new Error(translateNow('fileMenu.revealMissing'))
  }
}

// Rename a file/folder in place; returns the new absolute path. Local only.
export async function renameDesktopPath(path: string, newName: string): Promise<string> {
  const desktop = bridge()

  if (!desktop.renamePath) {
    throw new Error('Rename is not available')
  }

  const result = await desktop.renamePath(path, newName)

  return result.path
}

// Move a file/folder to the OS trash (recoverable). Local only.
export async function trashDesktopPath(path: string): Promise<void> {
  const desktop = bridge()

  if (!desktop.trashPath) {
    throw new Error('Delete is not available')
  }

  await desktop.trashPath(path)
}

export async function copyTextToClipboard(text: string): Promise<void> {
  await bridge().writeClipboard(text)
}

// Working-tree-vs-HEAD diff for one file. Empty when unchanged / not a repo.
// Remote gateway → backend git (/api/git/file-diff); local → Electron git.
export async function desktopFileDiff(repoRoot: string, filePath: string, owner?: SandboxOwner): Promise<string> {
  if (owner) {
    const check = await confirmModelOutputAccess(owner)

    const result = await hermesApi<{ diff: string }>({
      connectionId: owner.connectionId ?? undefined,
      profile: owner.profile ?? 'default',
      path: `/api/git/file-diff?path=${encodeURIComponent(repoRoot)}&file=${encodeURIComponent(filePath)}`
    })

    check()

    return result.diff || ''
  }

  if (isDesktopFsRemoteMode()) {
    const result = await remoteFsApi<{ diff: string }>(
      `/api/git/file-diff?path=${encodeURIComponent(repoRoot)}&file=${encodeURIComponent(filePath)}`
    )

    return result.diff || ''
  }

  const git = bridge().git

  return git?.fileDiff ? git.fileDiff(repoRoot, filePath) : ''
}

export async function selectDesktopPaths(options?: HermesSelectPathsOptions): Promise<string[]> {
  const desktop = bridge()
  const profile = desktopFsProfile()
  const localOptions = profile ? { ...options, profile } : options

  if (!isDesktopFsRemoteMode()) {
    return desktop.selectPaths(localOptions)
  }

  if (!options?.directories) {
    return desktop.selectPaths(localOptions)
  }

  return remotePicker ? remotePicker.selectPaths({ ...options, multiple: false }) : []
}
