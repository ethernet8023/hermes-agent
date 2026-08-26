/**
 * Pure policy for which update roots the desktop's git update flow may touch.
 *
 * Mirrors installation/tree.py's stamp-pure ladder: a .git tree whose
 * install-stamp.json says `updateMechanism: "self"` is a managed checkout the
 * update flow may move; a .git tree with no such stamp is somebody's working
 * tree — the flow would stash local changes and move it to the update branch,
 * so both check and apply refuse it and point at `git pull`. No .git at all
 * is not updatable through git. No path table: the stamp is the whole fact.
 *
 * Extracted from main.ts so the policy is unit testable without booting
 * Electron (main.ts requires('electron') at load).
 */

export type UpdateRootKind = 'managed-checkout' | 'unmanaged-checkout' | 'not-a-git-checkout'

export interface ClassifyUpdateRootDeps {
  /** True when the root has a .git entry (directory or worktree gitfile). */
  isGitCheckout: (root: string) => boolean
  /**
   * The parsed install-stamp.json at the root, or null when absent /
   * unreadable. The caller owns the read (fs is injected for testability);
   * strip a BOM before JSON.parse — Windows tooling adds one.
   */
  readStamp: (root: string) => { updateMechanism?: string } | null
}

export function classifyUpdateRoot(root: string, deps: ClassifyUpdateRootDeps): UpdateRootKind {
  if (!deps.isGitCheckout(root)) {
    return 'not-a-git-checkout'
  }

  const stamp = deps.readStamp(root)

  return stamp && stamp.updateMechanism === 'self' ? 'managed-checkout' : 'unmanaged-checkout'
}

export function unmanagedCheckoutMessage(root: string): string {
  return (
    `This copy of Hermes Desktop is running from a git checkout at ${root}.\n` +
    'Update it by closing Hermes and running `git pull`.'
  )
}
