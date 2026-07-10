/**
 * Group node renderer — a ZONE: header strip (tabs when stacked, minimize
 * chevron) + the active pane's content, resolved from the contribution
 * registry (`area: 'panes'`). Empty zones exist only in editor-authored
 * trees (drop targets until the first structural op prunes them).
 *
 * Dragging is FancyZones-style (drag-session.ts): the layout stays fixed and
 * every zone lights up as a whole-region drop target. Right-click opens the
 * contextual zone menu (split/move + header/minimize toggles).
 */

import { useStore } from '@nanostores/react'
import { type CSSProperties, type MouseEvent as ReactMouseEvent, type ReactNode, useRef } from 'react'

import { Codicon } from '@/components/ui/codicon'
import { ContextMenu, ContextMenuContent, ContextMenuItem, ContextMenuTrigger } from '@/components/ui/context-menu'
import { DecodeText } from '@/components/ui/decode-text'
import { DROP_SHEET_BLUR_CLASS, DROP_SHEET_CLASS, DropPill } from '@/components/ui/drop-affordance'
import { PANE_TAB_STRIP_LINE, PaneTab, PaneTabLabel } from '@/components/ui/pane-tab'
import { ContribBoundary } from '@/contrib/react/boundary'
import { useContributions } from '@/contrib/react/use-contributions'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'

import { $layoutEditMode } from '../../edit-mode'
import { useWindowControlsOverlap } from '../../geometry'
import type { DropPosition, GroupNode, RootEdge } from '../model'
import { adjacentGroup } from '../model'
import {
  $dropHint,
  $hiddenTreePanes,
  $layoutTree,
  $narrowViewport,
  $treeDragging,
  activateTreePane,
  closeTreePane,
  moveTreePane,
  SESSION_TILE_DRAG,
  setTreeGroupHeaderHidden,
  splitTreeZone,
  toggleTreeGroupMinimized
} from '../store'
import { FADE_IN_DURATION_MILLIS } from '../zones-engine'

import { type DoubleTapContext, startPaneDrag } from './drag-session'
import { paneChrome } from './track-model'

/** A directional action in the zone menu (computed per group state). */
interface ZoneMenuDirection {
  side: RootEdge
  label: string
  run: () => void
}

const DIRECTION_ORDER: readonly RootEdge[] = ['right', 'bottom', 'left', 'top']
const DIRECTION_ARROW: Record<RootEdge, string> = { bottom: '↓', left: '←', right: '→', top: '↑' }

/** Right-click zone menu: directional actions + header toggle + minimize.
 *  The directions are CONTEXTUAL (computed by TreeGroup): a stacked group
 *  offers "Split <dir>" (carve a new zone with the clicked pane — VS Code
 *  split-and-move in one gesture); a single-pane group offers "Move <dir>"
 *  into the zone actually sitting on that side — directions with no visible
 *  neighbor aren't offered, so no action ever appears to do nothing. */
function ZoneMenu({
  children,
  closable,
  directions,
  headerHidden,
  minimized,
  nodeId
}: {
  children: ReactNode
  /** The pane the menu closes (the right-clicked chip / the active pane);
   *  undefined = not closable (the main zone). */
  closable?: () => string | undefined
  directions: ZoneMenuDirection[]
  headerHidden?: boolean
  minimized?: boolean
  nodeId: string
}) {
  const { t } = useI18n()

  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>{children}</ContextMenuTrigger>
      <ContextMenuContent>
        {directions.map(direction => (
          <ContextMenuItem key={direction.side} onSelect={direction.run}>
            {direction.label}
          </ContextMenuItem>
        ))}
        <ContextMenuItem onSelect={() => setTreeGroupHeaderHidden(nodeId, !headerHidden)}>
          {headerHidden ? t.zones.showHeader : t.zones.hideHeader}
        </ContextMenuItem>
        <ContextMenuItem onSelect={() => toggleTreeGroupMinimized(nodeId, !minimized)}>
          {minimized ? t.zones.restore : t.zones.minimize}
        </ContextMenuItem>
        {/* Resolved at render: the menu mounts on open, after the right-click
            set menuPaneRef — so an uncloseable target hides the item instead
            of offering a dead action. */}
        {closable?.() !== undefined && (
          <ContextMenuItem
            onSelect={() => {
              const paneId = closable?.()

              if (paneId) {
                closeTreePane(paneId)
              }
            }}
          >
            {t.common.close}
          </ContextMenuItem>
        )}
      </ContextMenuContent>
    </ContextMenu>
  )
}

export function TreeGroup({
  node,
  parentAxis,
  railSide = 'left'
}: {
  node: GroupNode
  parentAxis?: 'column' | 'row'
  railSide?: 'left' | 'right'
}) {
  const { t } = useI18n()
  const ref = useRef<HTMLDivElement>(null)
  const stripRef = useRef<HTMLDivElement>(null)
  // The chip under the last right-click — the pane the zone menu's Split
  // actions carry into the new zone (header background = the active pane).
  const menuPaneRef = useRef<string | undefined>(undefined)
  const panes = useContributions('panes')
  // Coarse drag flag only (set once at drag start/end). The per-frame drop
  // HINT lives in ZoneDropOverlay so a moving pointer re-renders the tiny
  // overlay, not every zone's header/body (and not the menuDirections walk).
  const dragging = useStore($treeDragging)
  const editMode = useStore($layoutEditMode)
  const wcOverlap = useWindowControlsOverlap(ref, true)

  const hiddenPanes = useStore($hiddenTreePanes)
  const narrow = useStore($narrowViewport)

  const paneFor = (id: string) => panes.find(p => p.id === id)

  // Unregistered (plugin not loaded), chrome-toggled-off, and narrow-collapsed
  // panes drop out of the header; the active pane falls back to the first
  // shown one (render-side — the tree keeps `active`).
  const paneShown = (id: string) =>
    Boolean(paneFor(id)) && !hiddenPanes.has(id) && !(narrow && paneChrome(paneFor(id)).collapsible)

  const shown = node.panes.filter(paneShown)
  const activeId = shown.includes(node.active) ? node.active : (shown[0] ?? node.active)
  const active = paneFor(activeId)
  const isEmpty = node.panes.length === 0

  // ONE header style: the app's compact pane-header. DEFAULT is contextual —
  // a single pane isn't a "tab", so its header auto-hides; a stack shows its
  // chips. EXCEPTION: a lone TILE (closeable, placement 'main' — a session/page
  // split) always shows its header, so it has a tab + close X — a tile in its
  // own zone was otherwise unclosable (the "3rd tile has no tab" trap). Chrome
  // panes (sessions/files/terminal…) and the uncloseable workspace keep the
  // clean no-tab default. Double-click toggles it either way; a minimized
  // group always shows its header (it IS the header).
  const hasLoneTile = shown.some(id => {
    const chrome = paneChrome(paneFor(id))

    return !chrome.uncloseable && chrome.placement === 'main'
  })

  const headerHidden = node.headerHidden ?? (shown.length <= 1 && !hasLoneTile)

  // A group collapses ALONG its parent split's axis. In a row that means the
  // WIDTH collapses — a full-width horizontal header would strand a tall
  // empty column, so the minimized form is a narrow vertical rail instead
  // (tabs reading top-to-bottom). In a column (stacked zones) the horizontal
  // header IS the collapsed form, exactly as before.
  const verticalCollapse = Boolean(node.minimized) && parentAxis === 'row' && !isEmpty
  const headerVisible = !isEmpty && !verticalCollapse && (Boolean(node.minimized) || !headerHidden)

  // Drag handles preventDefault pointerdown (no native dblclick), so the
  // header + chips share a synthesized double-tap: restore if collapsed
  // (undoing the first tap's minimize toggle) and hide the chrome.
  const hideHeaderDoubleTap: DoubleTapContext = {
    key: `hide-header-${node.id}`,
    onDoubleTap: () => {
      toggleTreeGroupMinimized(node.id, false)
      setTreeGroupHeaderHidden(node.id, true)
    }
  }

  const dirWord: Record<RootEdge, string> = {
    bottom: t.zones.dirDown,
    left: t.zones.dirLeft,
    right: t.zones.dirRight,
    top: t.zones.dirUp
  }

  // Zone-menu directions, contextual to this group's state:
  //  - stacked panes -> "Split <dir>": carve a new zone on that side with the
  //    right-clicked chip's pane in it (split + move, one gesture);
  //  - a single pane -> "Move <dir>": join the zone visually adjacent on that
  //    side (splitting here would only make an invisible empty zone). Sides
  //    with no visible neighbor are omitted entirely.
  const tree = useStore($layoutTree)

  const menuDirections: ZoneMenuDirection[] =
    shown.length > 1
      ? DIRECTION_ORDER.map(side => ({
          side,
          label: `${t.zones.split(dirWord[side])} ${DIRECTION_ARROW[side]}`,
          run: () => splitTreeZone(node.id, side, menuPaneRef.current ?? activeId)
        }))
      : DIRECTION_ORDER.flatMap(side => {
          const neighbor = tree ? adjacentGroup(tree, node.id, side, g => g.panes.some(paneShown)) : null

          if (!neighbor || neighbor.id === node.id) {
            return []
          }

          return [
            {
              side,
              label: `${t.zones.move(dirWord[side])} ${DIRECTION_ARROW[side]}`,
              run: () => moveTreePane(activeId, { groupId: neighbor.id, pos: 'center' })
            }
          ]
        })

  // Close targets the right-clicked chip (falling back to the active pane);
  // only panes that declare `uncloseable` (the main workspace) are exempt.
  const closable = () => {
    const paneId = menuPaneRef.current ?? activeId

    return paneChrome(paneFor(paneId)).uncloseable ? undefined : paneId
  }

  // Same menu on the header strip and the edit veil — one prop bag.
  const zoneMenu = { closable, directions: menuDirections, headerHidden, minimized: node.minimized, nodeId: node.id }

  // Double-click ANYWHERE in the zone toggles the header (the header itself
  // handles its own double-tap, so this covers the body — crucially the only
  // clickable surface once the header is hidden). Interactive targets and
  // real text selections (double-click selects a word) never toggle.
  const onZoneDoubleClick = (e: ReactMouseEvent<HTMLDivElement>) => {
    if (isEmpty || node.minimized) {
      return
    }

    const target = e.target as HTMLElement

    if (target.closest('button, a, input, textarea, select, [contenteditable], [role="tab"], .xterm')) {
      return
    }

    if (window.getSelection()?.toString()) {
      return
    }

    setTreeGroupHeaderHidden(node.id, !headerHidden)
  }

  return (
    <div
      className="relative flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden bg-(--ui-bg-editor)"
      data-tree-group={node.id}
      // Advertises the visible tab strip so panes can drop their own
      // self-naming labels (see [data-pane-self-label] in styles.css).
      data-zone-header={headerVisible || undefined}
      onDoubleClick={onZoneDoubleClick}
      ref={ref}
      style={wcOverlap ? { paddingTop: wcOverlap.y + wcOverlap.height } : undefined}
    >
      {wcOverlap && (
        <div
          aria-hidden="true"
          className="pointer-events-none absolute z-10 [-webkit-app-region:drag]"
          style={{ height: wcOverlap.height, left: wcOverlap.x, top: wcOverlap.y, width: wcOverlap.width }}
        />
      )}

      {/* Minimized in a ROW: a narrow vertical rail — width collapses to the
          strip, tabs read top-to-bottom in the SAME visual language as the
          horizontal PaneTab strip (gutter bg, quaternary separators, hover
          wash, strip line on the content-facing edge). Click a tab to
          restore + activate; click anywhere else on the rail to restore. */}
      {verticalCollapse && (
        <ZoneMenu {...zoneMenu}>
          <div
            className={cn(
              'flex h-full w-7 shrink-0 cursor-pointer select-none flex-col items-stretch bg-(--pane-tab-strip-bg) [--pane-tab-strip-bg:var(--theme-card-seed)]',
              // The strip line (PANE_TAB_STRIP_LINE rotated): the divider
              // faces the content the zone collapsed away from.
              railSide === 'right'
                ? 'shadow-[inset_1px_0_0_var(--ui-stroke-tertiary)]'
                : 'shadow-[inset_-1px_0_0_var(--ui-stroke-tertiary)]'
            )}
            onClick={() => toggleTreeGroupMinimized(node.id, false)}
            title={t.zones.restore}
          >
            <div
              className="flex min-h-0 flex-col overflow-y-auto [-ms-overflow-style:none] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
              role="tablist"
            >
              {shown.map(paneId => {
                const title = paneFor(paneId)?.title ?? paneId

                return (
                  <button
                    aria-selected={paneId === activeId}
                    className={cn(
                      // PaneTab's idle recipe, rotated 90°: gutter bg,
                      // quaternary separators between tabs, tertiary text
                      // with the 4% translucent hover wash.
                      'flex max-h-48 w-full shrink-0 items-center justify-center bg-(--tab-bg) px-1.5 py-2.5 text-[0.6875rem] font-medium [-webkit-app-region:no-drag] [writing-mode:vertical-rl]',
                      'not-first:border-t not-first:border-t-(--ui-stroke-quaternary)',
                      'text-(--ui-text-tertiary) [--tab-bg:var(--pane-tab-strip-bg,var(--theme-card-seed))] hover:shadow-[inset_0_0_0_100vmax_color-mix(in_srgb,var(--ui-base)_4%,transparent)] hover:text-(--ui-text-secondary)',
                      paneId === activeId && 'text-(--ui-text-secondary)'
                    )}
                    data-tree-tab={paneId}
                    key={paneId}
                    onClick={event => {
                      event.stopPropagation()
                      toggleTreeGroupMinimized(node.id, false)
                      activateTreePane(node.id, paneId)
                    }}
                    role="tab"
                    type="button"
                  >
                    <span className="truncate">{title}</span>
                  </button>
                )
              })}
            </div>
          </div>
        </ZoneMenu>
      )}

      {/* Header: the file-preview tab strip (PaneTab), one shared component. */}
      {headerVisible && (
        <ZoneMenu {...zoneMenu}>
          <div
            // Active = sidebar surface (merges into body). Strip =
            // `--theme-card-seed` (VS Code `tab.inactiveBackground`). Line =
            // PANE_TAB_STRIP_LINE; active tab cuts through it.
            className={cn(
              'group/pane-header flex h-7 shrink-0 select-none bg-(--pane-tab-strip-bg) [-webkit-app-region:no-drag] [--pane-tab-active-bg:var(--ui-sidebar-surface-background)] [--pane-tab-strip-bg:var(--theme-card-seed)]',
              PANE_TAB_STRIP_LINE
            )}
            onContextMenu={e => {
              menuPaneRef.current =
                (e.target as HTMLElement).closest('[data-tree-tab]')?.getAttribute('data-tree-tab') ?? undefined
            }}
            onPointerDown={e =>
              // Tap the header to collapse to it / expand back — the DetailPane
              // / sidebar-section gesture. Double-tap hides the header entirely.
              // Drag still moves the pane.
              startPaneDrag(
                activeId,
                e,
                () => toggleTreeGroupMinimized(node.id, !node.minimized),
                undefined,
                hideHeaderDoubleTap
              )
            }
            ref={stripRef}
            style={{ cursor: 'grab' }}
          >
            <div
              className="flex min-w-0 flex-1 overflow-x-auto overflow-y-hidden [-ms-overflow-style:none] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
              role="tablist"
            >
              {shown.map(paneId => {
                const isActive = paneId === activeId && !node.minimized
                const closeable = !paneChrome(paneFor(paneId)).uncloseable
                const title = paneFor(paneId)?.title ?? paneId

                return (
                  <PaneTab
                    active={isActive}
                    aria-selected={isActive}
                    data-tree-tab={paneId}
                    key={paneId}
                    onClose={closeable ? () => closeTreePane(paneId) : undefined}
                    onPointerDown={e =>
                      startPaneDrag(
                        paneId,
                        e,
                        () => {
                          // Tabs ACTIVATE (restoring a collapsed group).
                          // Minimize lives on the chevron / single-pane label
                          // — overloading the active tab made double-click a
                          // minimize/restore/hide lottery.
                          if (node.minimized) {
                            toggleTreeGroupMinimized(node.id, false)
                          }

                          activateTreePane(node.id, paneId)
                        },
                        stripRef.current ? { groupId: node.id, strip: stripRef.current } : undefined,
                        hideHeaderDoubleTap
                      )
                    }
                    role="tab"
                    style={{ cursor: 'grab' }}
                  >
                    <PaneTabLabel>{title}</PaneTabLabel>
                  </PaneTab>
                )
              })}
            </div>
            <button
              aria-label={node.minimized ? t.zones.restore : t.zones.minimize}
              className="mx-1 grid size-5 shrink-0 place-items-center self-center rounded-md text-(--ui-text-tertiary) opacity-0 transition-opacity hover:bg-(--ui-control-hover-background) hover:text-foreground focus-visible:opacity-100 group-hover/pane-header:opacity-100"
              onClick={() => toggleTreeGroupMinimized(node.id, !node.minimized)}
              onPointerDown={e => e.stopPropagation()}
              type="button"
            >
              <Codicon name={node.minimized ? 'chevron-down' : 'chevron-up'} size="0.75rem" />
            </button>
          </div>
        </ZoneMenu>
      )}

      {/* Body: the active pane's contributed content, or the empty zone. */}
      {!node.minimized && (
        <div className="relative min-h-0 min-w-0 flex-1 overflow-auto">
          {isEmpty ? (
            <div className="grid h-full place-items-center">
              {/* Same decode primitive as the CONNECTING boot overlay. */}
              <DecodeText className="text-(--ui-text-quaternary)" cursor prefix={1} text="HERMES" />
            </div>
          ) : active?.render ? (
            <ContribBoundary id={active.id}>{active.render()}</ContribBoundary>
          ) : (
            <div className="p-3 font-mono text-[11px] text-(--ui-text-quaternary)">{t.zones.missingPane(activeId)}</div>
          )}
        </div>
      )}

      {/* Edit-mode veil: the BODY is a drag handle for the active pane. It
          starts below the header so tabs/headers stay directly interactive
          (drag any tab, right-click for the zone menu). */}
      {editMode && !dragging && !isEmpty && !node.minimized && (
        <ZoneMenu {...zoneMenu}>
          <div
            // z-50: pane CONTENT may carry its own stacked chrome (the
            // terminal rail is z-40) — the edit veil must cover all of it.
            // The scrim mixes the accent over the CHROME BG (not transparent)
            // so it properly dims content in dark themes instead of leaving a
            // barely-tinted wash; the light blur reads as "edit mode" the same
            // way the zone editor's backdrop does.
            className="absolute inset-x-0 bottom-0 z-50 flex cursor-grab items-center justify-center outline-1 -outline-offset-2 outline-dashed backdrop-blur-[2px]"
            onPointerDown={e => startPaneDrag(activeId, e)}
            style={{
              top: headerVisible ? 28 : 0,
              background:
                'color-mix(in srgb, var(--ui-accent) 6%, color-mix(in srgb, var(--ui-bg-chrome) 55%, transparent))',
              outlineColor: 'color-mix(in srgb, var(--ui-accent) 55%, transparent)'
            }}
          >
            <span className="flex max-w-[calc(100%-1rem)] items-center gap-1.5 rounded-md border border-(--ui-stroke-secondary) bg-popover px-2 py-1 text-[0.64rem] font-semibold uppercase tracking-[0.16em] text-(--ui-text-secondary)">
              <Codicon className="shrink-0" name="gripper" size="0.8125rem" />
              <span className="min-w-0 truncate">{active?.title ?? activeId}</span>
            </span>
          </div>
        </ZoneMenu>
      )}

      {/* FancyZones drop overlay — its own component so the per-frame drop
          hint re-renders only this (tiny) node, not the whole zone. */}
      <ZoneDropOverlay isEmpty={isEmpty} node={node} />
    </div>
  )
}

// ---------------------------------------------------------------------------
// FancyZones drop overlay
// ---------------------------------------------------------------------------

/** Sheet inset from the zone edge (px). */
const REGION_PAD = 6

/** The sheet's box per drop position — longhand insets so CSS transitions can
 *  interpolate the px↔% change: the target GLIDES between the full zone and
 *  the hovered half instead of snapping (VS Code dock preview). */
const REGION: Record<DropPosition, CSSProperties> = {
  bottom: { bottom: REGION_PAD, left: REGION_PAD, right: REGION_PAD, top: '50%' },
  center: { bottom: REGION_PAD, left: REGION_PAD, right: REGION_PAD, top: REGION_PAD },
  left: { bottom: REGION_PAD, left: REGION_PAD, right: '50%', top: REGION_PAD },
  right: { bottom: REGION_PAD, left: '50%', right: REGION_PAD, top: REGION_PAD },
  top: { bottom: '50%', left: REGION_PAD, right: REGION_PAD, top: REGION_PAD }
}

const EDGE_ARROW: Record<Exclude<DropPosition, 'center'>, string> = {
  bottom: 'arrow-down',
  left: 'arrow-left',
  right: 'arrow-right',
  top: 'arrow-up'
}

/**
 * The FancyZones drop overlay for one zone. Split out of TreeGroup so the
 * per-pointermove `$dropHint` churn re-renders only this lightweight node —
 * the zone's header, body, and menu-direction walk stay put during a drag.
 *
 * ONE dashed sheet per zone, in the attachment dropzone's design language
 * (DROP_SHEET_CLASS + DropPill — the composer drop and the zone targets speak
 * identically): a quiet outline over every eligible zone, accent-lit over the
 * target, morphing to the hovered half for an edge split. The pill names the
 * outcome; edges get their arrow.
 */
function ZoneDropOverlay({ isEmpty, node }: { isEmpty: boolean; node: GroupNode }) {
  const { t } = useI18n()
  const dragging = useStore($treeDragging)
  const hint = useStore($dropHint)

  if (dragging === null) {
    return null
  }

  // A session drag (sidebar row) reuses this exact overlay, but only over
  // zones that host a chat surface — a session never lands next to the sidebar
  // or terminal.
  const sessionDrag = dragging === SESSION_TILE_DRAG

  if (sessionDrag && !node.panes.some(p => p === 'workspace' || p.startsWith('session-tile:'))) {
    return null
  }

  const isDragSource = node.panes.includes(dragging)

  // The source zone, when it holds only the dragged pane, has nothing to drop.
  if (isDragSource && node.panes.length === 1) {
    return null
  }

  const primary = hint?.groupId === node.id
  const active = hint?.groupIds?.includes(node.id) ?? false
  const multi = (hint?.groupIds?.length ?? 0) > 1
  // Sub-positions only exist for a single-zone target (a Shift-span merges).
  const pos = primary && !multi ? (hint?.pos ?? 'center') : 'center'
  // Session drag over a zone's CENTER: the "link to chat" overlay inside the
  // surface (ChatDropOverlay — the same sheet + pill) owns that region; this
  // sheet fades out so the two never stack. Edges behave exactly like a tab.
  const centerLink = sessionDrag && primary && pos === 'center'

  const pill =
    !primary || centerLink
      ? null
      : multi
        ? { icon: 'combine', label: t.zones.spanHere }
        : pos !== 'center'
          ? { icon: EDGE_ARROW[pos], label: sessionDrag ? t.zones.openHere : t.zones.splitHere }
          : isDragSource
            ? { icon: 'discard', label: t.zones.staysHere }
            : { icon: 'layers', label: isEmpty ? t.zones.moveHere : t.zones.stackHere }

  return (
    <div
      className="pointer-events-none absolute inset-0 z-40"
      style={{ animation: `hermes-zone-fade ${FADE_IN_DURATION_MILLIS}ms linear both` }}
    >
      <div
        className={cn(
          DROP_SHEET_CLASS,
          'absolute flex items-center justify-center transition-all duration-150 ease-out',
          // Blur only the live target — idle outlines must not fog the app.
          active && !centerLink && DROP_SHEET_BLUR_CLASS,
          centerLink && 'opacity-0'
        )}
        style={{
          ...REGION[pos],
          // Accent over a card wash so the fill dims content on dark themes
          // (a bare accent alpha disappears there).
          background: active
            ? 'color-mix(in srgb, var(--ui-accent) 18%, color-mix(in srgb, var(--dt-card) 55%, transparent))'
            : 'color-mix(in srgb, var(--ui-accent) 5%, color-mix(in srgb, var(--dt-card) 25%, transparent))',
          borderColor: `color-mix(in srgb, var(--ui-accent) ${active ? 75 : 28}%, transparent)`
        }}
      >
        {pill && <DropPill icon={pill.icon}>{pill.label}</DropPill>}
      </div>
    </div>
  )
}
