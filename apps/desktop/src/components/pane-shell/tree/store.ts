/**
 * Layout tree store: one persisted tree replaces paneStates side/band
 * overrides. The DEFAULT tree is declared by the app root (like config);
 * the persisted tree is the user's customization; reset returns to default.
 */

import { atom } from 'nanostores'

import { SIDEBAR_COLLAPSE_MEDIA_QUERY } from '@/app/layout-constants'
import { setPluginEnabled } from '@/contrib/plugins-store'
import { registry } from '@/contrib/registry'
import { notify } from '@/store/notifications'
import { clearAllPaneSizeOverrides } from '@/store/panes'

import {
  allPaneIds,
  type DropPosition,
  findGroupOfPane,
  groupLeafIds,
  insertAtGroup,
  isLayoutNode,
  type LayoutNode,
  mergeZonesWithPane as mergeZonesWithPaneOp,
  mirrorRootRow,
  movePane as movePaneOp,
  normalize,
  removePane,
  reorderPaneInGroup as reorderPaneInGroupOp,
  type RootEdge,
  setActivePane as setActivePaneOp,
  setGroupHeaderHidden as setGroupHeaderHiddenOp,
  setGroupMinimized,
  setSplitWeights as setSplitWeightsOp,
  splitGroupZone as splitGroupZoneOp
} from './model'

// v2: v1 trees were saved against placeholder panes with index-order zone
// assignment (chat could land in a corner cell). Retire them wholesale.
const STORAGE_KEY = 'hermes.desktop.layoutTree.v2'

try {
  window.localStorage.removeItem('hermes.desktop.layoutTree.v1')
} catch {
  // Nonfatal.
}

let defaultTree: LayoutNode | null = null

function loadPersisted(): LayoutNode | null {
  if (typeof window === 'undefined') {
    return null
  }

  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)

    if (!raw) {
      return null
    }

    const parsed = JSON.parse(raw) as unknown

    // Canonicalize on load: strips stale attributes older code persisted
    // (e.g. explicit headerHidden on lone-pane zones) and re-flattens.
    return isLayoutNode(parsed) ? normalize(parsed) : null
  } catch {
    return null
  }
}

function persist(tree: LayoutNode | null) {
  if (typeof window === 'undefined') {
    return
  }

  try {
    if (tree) {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(tree))
    } else {
      window.localStorage.removeItem(STORAGE_KEY)
    }
  } catch {
    // Storage failures are nonfatal.
  }
}

/** The live tree (null until a default is declared). */
export const $layoutTree = atom<LayoutNode | null>(loadPersisted())

/**
 * Which layout preset the current tree came from; `'custom'` after the user
 * rearranges anything. Drives the picker's active highlight.
 */
export const $activePresetId = atom<string>(
  typeof window === 'undefined'
    ? 'default'
    : (window.localStorage.getItem('hermes.desktop.layoutPreset.active') ?? 'default')
)

export function markActivePreset(id: string) {
  $activePresetId.set(id)

  try {
    window.localStorage.setItem('hermes.desktop.layoutPreset.active', id)
  } catch {
    // Nonfatal.
  }
}

/** Pane id being dragged (tree drag session), null when idle. */
export const $treeDragging = atom<string | null>(null)

/**
 * Panes hidden by app chrome toggles (titlebar sidebar / right-sidebar
 * buttons). The tree KEEPS the zone and its mounted content; a zone whose
 * every pane is hidden collapses to nothing until a toggle brings it back.
 * Not persisted here — each binding's store owns persistence.
 */
export const $hiddenTreePanes = atom<ReadonlySet<string>>(new Set())

/** Add/remove `item` in a readonly set, returning a fresh set — or null when
 *  membership already matches `present` (so callers can early-out on a no-op). */
function toggledSet<T>(set: ReadonlySet<T>, item: T, present: boolean): Set<T> | null {
  if (set.has(item) === present) {
    return null
  }

  const next = new Set(set)

  if (present) {
    next.add(item)
  } else {
    next.delete(item)
  }

  return next
}

export function setTreePaneHidden(paneId: string, hidden: boolean) {
  const next = toggledSet($hiddenTreePanes.get(), paneId, hidden)

  if (!next) {
    return
  }

  $hiddenTreePanes.set(next)

  // Unhiding is an intent to SEE the pane — front it in its group.
  if (!hidden) {
    revealTreePane(paneId)
  }
}

/**
 * CLOSE — the tab context menu's "Close". Two routes:
 *  - a registered closer (core panes whose visibility an app store owns:
 *    review/terminal/preview/sessions) closes through that store, so the
 *    titlebar/statusbar toggles stay truthful;
 *  - everything else (plugin panes, unbound core panes) is DISMISSED: removed
 *    from the tree and remembered so adoption doesn't re-add it. Reveal
 *    intent (a preview target, ⌘G) or a layout reset un-dismisses.
 */
const DISMISSED_KEY = 'hermes.desktop.dismissedPanes.v1'

function loadDismissed(): ReadonlySet<string> {
  try {
    const raw = window.localStorage.getItem(DISMISSED_KEY)

    return new Set(raw ? (JSON.parse(raw) as string[]) : [])
  } catch {
    return new Set()
  }
}

export const $dismissedPanes = atom<ReadonlySet<string>>(loadDismissed())

function saveDismissed(next: ReadonlySet<string>) {
  $dismissedPanes.set(next)

  try {
    if (next.size === 0) {
      window.localStorage.removeItem(DISMISSED_KEY)
    } else {
      window.localStorage.setItem(DISMISSED_KEY, JSON.stringify([...next]))
    }
  } catch {
    // Nonfatal.
  }
}

function setDismissed(paneId: string, dismissed: boolean) {
  const next = toggledSet($dismissedPanes.get(), paneId, dismissed)

  if (next) {
    saveDismissed(next)
  }
}

const paneClosers: Record<string, () => void> = {}
const paneOpeners: Record<string, () => void> = {}

/** Route a pane's Close through the app store that owns its visibility. */
export function registerPaneCloser(paneId: string, close: () => void) {
  paneClosers[paneId] = close
}

/**
 * Route a pane's "show it" intent through the app store that owns its
 * visibility — the mirror of `registerPaneCloser`, so a preset can reveal a
 * toggle-gated pane (e.g. the terminal, whose visibility ⌃`/`$terminalTakeover`
 * owns) while the toggle stays truthful. Only panes that opt in via
 * `data.revealOnPreset` are opened on preset apply.
 */
export function registerPaneOpener(paneId: string, open: () => void) {
  paneOpeners[paneId] = open
}

/** Remove a pane from the tree WITHOUT a dismissal record — for surfaces
 *  whose lifecycle an owner store drives (session tiles): the owner removes
 *  the contribution too, and a later re-open must re-adopt cleanly. */
export function removeTreePane(paneId: string) {
  const tree = $layoutTree.get()

  if (tree) {
    commit(removePane(tree, paneId))
  }
}

export function closeTreePane(paneId: string) {
  const closer = paneClosers[paneId]

  if (closer) {
    closer()

    return
  }

  // A plugin's pane: Close = DISABLE the plugin — the same switch as
  // Settings → Plugins, so recovery is discoverable and symmetric. The
  // contribution unregisters but the pane id STAYS in the tree, so
  // re-enabling restores it exactly where it was. (Dismissal + removal
  // would strand the pane with no way back short of a layout reset.)
  const source = registry.getArea('panes').find(c => c.id === paneId)?.source

  if (source?.startsWith('plugin:')) {
    const pluginId = source.slice('plugin:'.length)
    void setPluginEnabled(pluginId, false)
    notify({
      kind: 'info',
      title: `Plugin "${pluginId}" disabled`,
      message: 'Re-enable it in Settings → Plugins to bring the pane back.'
    })

    return
  }

  const tree = $layoutTree.get()

  if (tree) {
    setDismissed(paneId, true)
    commit(removePane(tree, paneId))
  }
}

/**
 * POSITIONAL side collapse — the titlebar's left/right sidebar toggles (and
 * ⌘B / ⌘J). Everything on that side of the MAIN zone in the root row hides
 * together, whatever panes live there (this is what makes the buttons agree
 * with a rearranged layout; the flip derivation works the same way). An AND
 * on top of per-pane visibility: zone shown ⇔ side open ∧ some pane shown.
 */
export type TreeSide = 'left' | 'right'

export const $collapsedTreeSides = atom<ReadonlySet<TreeSide>>(new Set())

// Side visibility is DERIVED from an app store (the binding owns persistence
// + button state); reveals flow back through its setter so they never
// disagree with the flag.
const sideOpeners: Partial<Record<TreeSide, (open: boolean) => void>> = {}

export function setTreeSideCollapsed(side: TreeSide, collapsed: boolean) {
  const next = toggledSet($collapsedTreeSides.get(), side, collapsed)

  if (next) {
    $collapsedTreeSides.set(next)
  }
}

/** Bind a side's visibility to an app store (mirror of bindPaneVisibility). */
export function bindTreeSideVisibility(
  side: TreeSide,
  $open: { get(): boolean; listen(fn: (open: boolean) => void): void },
  setOpen: (open: boolean) => void
) {
  sideOpeners[side] = setOpen
  setTreeSideCollapsed(side, !$open.get())
  $open.listen(open => setTreeSideCollapsed(side, !open))
}

/** The chrome toggle owning `paneId`'s root-row column — SEMANTIC, matching
 *  the renderer's `rootChildSide`: ⌘B ⇔ the sessions column (left-placement
 *  panes) wherever it sits, ⌘J ⇔ the other side columns. Null for the main
 *  column (never side-collapsed). */
export function treeSideOfPane(paneId: string): TreeSide | null {
  const tree = $layoutTree.get()

  if (!tree || tree.type !== 'split' || tree.orientation !== 'row') {
    return null
  }

  const child = tree.children.find(node => allPaneIds(node).includes(paneId))

  if (!child) {
    return null
  }

  const placementOf = (id: string) =>
    (registry.getArea('panes').find(c => c.id === id)?.data as { placement?: string } | undefined)?.placement

  const placements = allPaneIds(child).map(placementOf)

  if (placements.includes('main')) {
    return null
  }

  return placements.includes('left') ? 'left' : 'right'
}

/**
 * App intent "show pane X" (a preview target landed, ⌘G opened review, …):
 * open its side, unhide it, and bring it to the front of its group.
 */
export function revealTreePane(paneId: string) {
  // Reveal beats a Close: un-dismiss and let adoption put the pane back.
  if ($dismissedPanes.get().has(paneId)) {
    setDismissed(paneId, false)
    adoptContributedPanes()
  }

  const side = treeSideOfPane(paneId)

  if (side && $collapsedTreeSides.get().has(side)) {
    const open = sideOpeners[side]

    // Through the bound store when there is one, so the toggle stays truthful.
    if (open) {
      open(true)
    } else {
      setTreeSideCollapsed(side, false)
    }
  }

  const hiddenNow = $hiddenTreePanes.get()

  if (hiddenNow.has(paneId)) {
    setTreePaneHidden(paneId, false)

    return
  }

  const tree = $layoutTree.get()
  const group = tree ? findGroupOfPane(tree, paneId) : null

  if (tree && group && group.active !== paneId) {
    commit(setActivePaneOp(tree, group.id, paneId))
  }
}

/**
 * Narrow viewport (the app's sidebar-collapse breakpoint): panes whose
 * contribution declares `collapsible: true` leave the grid and become
 * edge overlays (see NarrowOverlays in renderer.tsx).
 */
export const $narrowViewport = atom(
  typeof window !== 'undefined' && window.matchMedia(SIDEBAR_COLLAPSE_MEDIA_QUERY).matches
)

if (typeof window !== 'undefined') {
  const query = window.matchMedia(SIDEBAR_COLLAPSE_MEDIA_QUERY)
  query.addEventListener('change', event => $narrowViewport.set(event.matches))
}

/** The titlebar flip toggle: swap which side each root-level rail lives on. */
export function mirrorLayoutTree() {
  const tree = $layoutTree.get()

  if (tree) {
    commit(mirrorRootRow(tree))
  }
}

export interface DropHint {
  kind: 'group'
  /** The zone a drop will land in (ClosestCenter among `groupIds`). */
  groupId?: string
  /** Full highlighted set (multi-zone when Shift extends the range). */
  groupIds?: string[]
  pos?: DropPosition
}

/** Live drop target under the pointer while dragging. */
export const $dropHint = atom<DropHint | null>(null)

/**
 * Adopt panes present in `source` but missing from `target`: each joins the
 * group its source siblings map to in the target (first group as a last
 * resort). Layout changes never lose panes.
 */
function adoptMissingPanes(target: LayoutNode, source: LayoutNode): LayoutNode {
  const have = new Set(allPaneIds(target))
  let next = target

  for (const paneId of allPaneIds(source)) {
    if (have.has(paneId)) {
      continue
    }

    const sibling = findGroupOfPane(source, paneId)?.panes.find(p => have.has(p))
    const targetId = (sibling ? findGroupOfPane(next, sibling)?.id : undefined) ?? groupLeafIds(next)[0]

    if (targetId) {
      next = insertAtGroup(next, targetId, paneId, 'center') ?? next
      have.add(paneId)
    }
  }

  return next
}

/**
 * Declare the app's default tree. Adopted immediately when the user has no
 * persisted customization; a persisted tree from an older default adopts any
 * panes it's missing.
 */
export function declareDefaultTree(tree: LayoutNode) {
  defaultTree = tree
  const current = $layoutTree.get()

  if (!current) {
    $layoutTree.set(tree)

    return
  }

  const next = adoptMissingPanes(current, tree)

  if (next !== current) {
    commit(next)
  }
}

/**
 * LIVE pane adoption — a `panes` contribution that isn't in the tree yet
 * (a plugin registered after boot, incl. runtime-loaded ones) joins the
 * tree via the SAME primitive a human drag/drop commits with
 * (`insertAtGroup`: anchor group + side). The pane's data supplies the
 * gesture:
 *
 *  - `dock: { pane, pos }` — "drop me on that edge of that pane". Any pane,
 *    any side, exactly what the drop chips do.
 *  - otherwise the semantic `placement` role infers the anchor: stack with
 *    a settled pane of the same placement, main zone as last resort.
 *
 * Happens once per pane lifetime (the committed tree remembers it across
 * boots), so user rearrangement wins from then on and plugin reloads keep
 * the pane where the user left it.
 */
interface PaneDockHint {
  pane: string
  pos: DropPosition
}

function adoptContributedPanes(): void {
  const tree = $layoutTree.get()

  if (!tree) {
    return
  }

  const panes = registry.getArea('panes')

  const dataOf = (paneId: string) =>
    panes.find(c => c.id === paneId)?.data as { placement?: string; dock?: PaneDockHint } | undefined

  const placementOf = (paneId: string) => dataOf(paneId)?.placement
  const mainId = panes.find(c => placementOf(c.id) === 'main')?.id
  const inTree = new Set(allPaneIds(tree))

  // Plugin panes are never dismissed anymore (Close disables the plugin
  // instead) — drop stale entries so panes stranded by the old behavior
  // re-adopt on their own.
  for (const pane of panes) {
    if (pane.source?.startsWith('plugin:') && $dismissedPanes.get().has(pane.id)) {
      setDismissed(pane.id, false)
    }
  }

  const dismissed = $dismissedPanes.get()
  const missing = panes.filter(c => !inTree.has(c.id) && !dismissed.has(c.id))

  if (missing.length === 0) {
    return
  }

  let next = tree

  for (const pane of missing) {
    const dock = dataOf(pane.id)?.dock
    const placement = placementOf(pane.id) ?? 'right'

    const anchor =
      (dock && allPaneIds(next).includes(dock.pane) ? dock.pane : undefined) ??
      allPaneIds(next).find(id => id !== pane.id && placementOf(id) === placement) ??
      mainId

    const target = findGroupOfPane(next, anchor ?? '')?.id

    if (target) {
      next = insertAtGroup(next, target, pane.id, dock?.pos ?? 'center') ?? next

      // An adopted pane ARRIVES with its chip showing — a surprise zone with
      // zero chrome has no obvious handle to drag or close. (Explicit reveal;
      // the next structural op returns lone panes to the auto-hide default.)
      const landed = findGroupOfPane(next, pane.id)

      if (landed) {
        next = setGroupHeaderHiddenOp(next, landed.id, false)
      }
    }
  }

  if (next !== tree) {
    commit(next)
  }
}

/** Adopt now + on every registry change (call once from the app root). */
export function watchContributedPanes(): void {
  adoptContributedPanes()
  registry.subscribe(adoptContributedPanes)
}

function commit(next: LayoutNode | null) {
  if (!next) {
    return
  }

  $layoutTree.set(next)
  persist(next)
}

export function moveTreePane(paneId: string, target: { groupId: string; pos: DropPosition }) {
  const tree = $layoutTree.get()

  if (tree) {
    commit(movePaneOp(tree, paneId, target))
    markActivePreset('custom')
  }
}

/**
 * Replace the whole tree (preset application). Panes living in the CURRENT
 * tree that the preset doesn't know about (e.g. plugin panes vs a bundled
 * preset) are adopted into the group their current siblings land in, so
 * applying a preset never loses a pane.
 */
export function applyTree(tree: LayoutNode, presetId: string) {
  const previous = $layoutTree.get()

  // A preset defines the layout's SIZES too — stale drag overrides from the
  // previous arrangement would distort it.
  clearAllPaneSizeOverrides()
  commit(previous ? adoptMissingPanes(tree, previous) : tree)
  markActivePreset(presetId)

  // Picking a named layout is an intent to SEE its panes. Toggle-gated panes
  // (the terminal, whose visibility a store owns) would otherwise stay
  // collapsed after the tree changes — so reveal the ones that opt in through
  // their owning store, keeping the ⌃`/toggle state truthful. Iterate the
  // preset's DECLARED panes (not the adopted result): logs is auto-adopted
  // hidden into every tree, so only a preset that explicitly places it (Quad)
  // should turn it on.
  const panes = registry.getArea('panes')

  for (const paneId of allPaneIds(tree)) {
    const data = panes.find(c => c.id === paneId)?.data as { revealOnPreset?: boolean } | undefined

    if (data?.revealOnPreset) {
      paneOpeners[paneId]?.()
    }
  }
}

/**
 * Shift-drag span: merge the highlighted zones into one holding `paneId`. Falls
 * back to a single-zone move at `fallbackGroupId` when the set can't merge
 * (non-rectangular selection).
 */
export function mergeTreeZones(groupIds: string[], paneId: string, fallbackGroupId: string | null) {
  const tree = $layoutTree.get()

  if (!tree) {
    return
  }

  const merged = mergeZonesWithPaneOp(tree, groupIds, paneId)

  if (merged) {
    commit(merged)
    markActivePreset('custom')
  } else if (fallbackGroupId) {
    moveTreePane(paneId, { groupId: fallbackGroupId, pos: 'center' })
  }
}

export function activateTreePane(groupId: string, paneId: string) {
  const tree = $layoutTree.get()

  if (tree) {
    commit(setActivePaneOp(tree, groupId, paneId))
  }
}

export function reorderTreePane(groupId: string, paneId: string, toIndex: number) {
  const tree = $layoutTree.get()

  if (tree) {
    commit(reorderPaneInGroupOp(tree, groupId, paneId, toIndex))
    markActivePreset('custom')
  }
}

/** Split a zone on `side`, moving `movePaneId` out of its stack into the new
 *  zone (VS Code split-and-move — the zone menu's Split actions). */
export function splitTreeZone(groupId: string, side: RootEdge, movePaneId: string) {
  const tree = $layoutTree.get()

  if (tree) {
    commit(splitGroupZoneOp(tree, groupId, side, movePaneId))
    markActivePreset('custom')
  }
}

export function toggleTreeGroupMinimized(groupId: string, minimized: boolean) {
  const tree = $layoutTree.get()

  if (tree) {
    commit(setGroupMinimized(tree, groupId, minimized))
  }
}

/** Hide/show a zone's header entirely (double-click gesture). */
export function setTreeGroupHeaderHidden(groupId: string, headerHidden: boolean) {
  const tree = $layoutTree.get()

  if (tree) {
    commit(setGroupHeaderHiddenOp(tree, groupId, headerHidden))
  }
}

export function setTreeSplitWeights(splitId: string, weights: number[]) {
  const tree = $layoutTree.get()

  if (tree) {
    // Weight drags are high-frequency: update live, persist on the trailing edge.
    $layoutTree.set(setSplitWeightsOp(tree, splitId, weights))
  }
}

function findSplitWeights(node: LayoutNode, splitId: string): number[] | null {
  if (node.type !== 'split') {
    return null
  }

  if (node.id === splitId) {
    return node.weights
  }

  for (const child of node.children) {
    const hit = findSplitWeights(child, splitId)

    if (hit) {
      return hit
    }
  }

  return null
}

/**
 * The weights a layout preset declares for `splitId` — the ACTIVE preset
 * first, then any other preset that knows the id. (Rearranging panes marks
 * the active preset 'custom' but zone STRUCTURE — and so split ids — comes
 * from whichever preset was applied, so the original baseline stays
 * findable.) Null when no preset has a matching-shape split.
 */
export function presetSplitWeights(splitId: string, length: number): number[] | null {
  const activeId = $activePresetId.get()
  const presets = [...registry.getArea('layouts')].sort((a, b) => Number(b.id === activeId) - Number(a.id === activeId))

  for (const preset of presets) {
    const weights = preset.data && isLayoutNode(preset.data) ? findSplitWeights(preset.data, splitId) : null

    if (weights && weights.length === length) {
      return [...weights]
    }
  }

  return null
}

export function persistTree() {
  persist($layoutTree.get())
}

export function resetLayoutTree() {
  persist(null)
  clearAllPaneSizeOverrides()
  // Reset restores EVERYTHING — closed panes included.
  saveDismissed(new Set())
  $layoutTree.set(defaultTree)
  markActivePreset('default')
  // Plugin panes aren't in the declared default — re-adopt by placement.
  adoptContributedPanes()
}

// Dev hook for automation.
if (import.meta.env.DEV && typeof window !== 'undefined') {
  ;(window as unknown as Record<string, unknown>).__HERMES_LAYOUT_TREE__ = {
    close: closeTreePane,
    dismissed: () => $dismissedPanes.get(),
    get: () => $layoutTree.get(),
    move: moveTreePane,
    registry,
    reset: resetLayoutTree,
    reveal: revealTreePane
  }
}
