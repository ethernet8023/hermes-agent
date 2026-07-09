/**
 * Wiring surfaces — each pane is its own memoized component. Every surface
 * reads the reactive state it renders from at the leaf (its own atom
 * subscriptions) and reaches the controller's callbacks through the stable
 * `actions` bag, so a state change scoped to one surface (or a bare
 * wiring-controller tick) never re-renders another. This is what keeps the
 * layout tree's zones independently rendered — the whole point of the shell.
 */

import { useStore } from '@nanostores/react'
import { type ComponentProps, lazy, memo, Suspense, useMemo } from 'react'
import { Navigate, Route, Routes, useParams } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { ContribBoundary } from '@/contrib/react/boundary'
import { useContributions } from '@/contrib/react/use-contributions'
import { sessionTitle as storedSessionTitle } from '@/lib/chat-runtime'
import { $pinnedSessionIds } from '@/store/layout'
import {
  $activeSessionId,
  $freshDraftReady,
  $gatewayState,
  $selectedStoredSessionId,
  $sessions,
  sessionPinId
} from '@/store/session'

import { ChatView } from '../chat'
import { ChatSidebar } from '../chat/sidebar'
import { SessionActionsMenu } from '../chat/sidebar/session-actions-menu'
import { TerminalPaneChrome } from '../right-sidebar/terminal/chrome'
import { contributedRoutes, NEW_CHAT_ROUTE, ROUTES_AREA, sessionRoute } from '../routes'
import { useStatusSnapshot } from '../shell/hooks/use-status-snapshot'
import { useStatusbarItems } from '../shell/hooks/use-statusbar-items'
import { ModelMenuPanel } from '../shell/model-menu-panel'
import { StatusbarControls } from '../shell/statusbar-controls'

import { setStatusbarItemGroup, useStatusbarContributions } from './panes'
import type { SidebarActions, WiringActions } from './types'

// Same lazy-view split as DesktopController — pages load on demand. The
// full-page views the workspace route table mounts live here; overlay views
// (agents/settings/…) are the controller's and stay in wiring.tsx.
const ArtifactsView = lazy(async () => ({ default: (await import('../artifacts')).ArtifactsView }))
const MessagingView = lazy(async () => ({ default: (await import('../messaging')).MessagingView }))
const SkillsView = lazy(async () => ({ default: (await import('../skills')).SkillsView }))

export function LegacySessionRedirect() {
  const { sessionId } = useParams()

  return <Navigate replace to={sessionId ? sessionRoute(sessionId) : NEW_CHAT_ROUTE} />
}

// The session-title dropdown (rename/pin/branch/delete menu) — the real app's
// ChatHeader, relocated into the composable titlebar's CENTER slot. In the
// tree layout the chat pane has no titlebar band of its own (the old in-pane
// <header> is a zero-height suppressed strip), so the dropdown lives in the
// window titlebar, centered over the workspace like it always visually was.
function SessionTitleDropdown({
  isRoutedSessionView,
  onDelete,
  onPin
}: {
  isRoutedSessionView: boolean
  onDelete: () => void
  onPin: () => void
}) {
  const sessions = useStore($sessions)
  const pinnedSessionIds = useStore($pinnedSessionIds)
  const selectedStoredSessionId = useStore($selectedStoredSessionId)
  const activeSessionId = useStore($activeSessionId)

  const stored =
    sessions.find(s => s.id === selectedStoredSessionId || s._lineage_root_id === selectedStoredSessionId) ?? null

  const title = stored ? storedSessionTitle(stored) : 'New session'

  // Pins live on the durable lineage-root id (survives auto-compression).
  const pinId = stored ? sessionPinId(stored) : selectedStoredSessionId
  const pinned = pinId ? pinnedSessionIds.includes(pinId) : false

  // A brand-new draft has nothing to rename/pin/delete.
  if (!selectedStoredSessionId && !activeSessionId && !isRoutedSessionView) {
    return null
  }

  return (
    <SessionActionsMenu
      align="center"
      onDelete={selectedStoredSessionId ? onDelete : undefined}
      onPin={selectedStoredSessionId ? onPin : undefined}
      pinned={pinned}
      sessionId={selectedStoredSessionId || activeSessionId || ''}
      sideOffset={8}
      title={title}
    >
      <Button
        className="pointer-events-auto flex h-6 min-w-0 max-w-[38vw] gap-1 overflow-hidden border border-transparent bg-transparent px-2 py-0 text-(--ui-text-secondary) hover:border-(--ui-stroke-tertiary) hover:bg-(--ui-control-hover-background) hover:text-foreground data-[state=open]:border-(--ui-stroke-tertiary) data-[state=open]:bg-(--ui-control-active-background) [-webkit-app-region:no-drag]"
        type="button"
        variant="ghost"
      >
        <h2 className="min-w-0 flex-1 truncate text-[0.75rem] font-medium leading-none">{title}</h2>
        <Codicon className="shrink-0 text-(--ui-text-tertiary)" name="chevron-down" size="0.8125rem" />
      </Button>
    </SessionActionsMenu>
  )
}

export const SidebarSurface = memo(function SidebarSurface({
  actions,
  currentView
}: {
  actions: SidebarActions
  currentView: ComponentProps<typeof ChatSidebar>['currentView']
}) {
  return <ChatSidebar currentView={currentView} {...actions} />
})

export const TerminalSurface = memo(function TerminalSurface() {
  return (
    <div className="relative flex h-full min-h-0 flex-col overflow-hidden bg-(--ui-editor-surface-background)">
      <TerminalPaneChrome />
    </div>
  )
})

/** Owns the statusbar's own data hooks (status snapshot poll, contributed
 *  items) so its 15s refresh — and any statusbar-only churn — re-renders the
 *  bar alone, never the chat/sidebar/terminal. */
export const StatusbarSurface = memo(function StatusbarSurface({
  actions,
  agentsOpen,
  chatOpen,
  commandCenterOpen
}: {
  actions: WiringActions
  agentsOpen: boolean
  chatOpen: boolean
  commandCenterOpen: boolean
}) {
  const gatewayState = useStore($gatewayState)
  const freshDraftReady = useStore($freshDraftReady)
  const { inferenceStatus, statusSnapshot } = useStatusSnapshot(gatewayState, actions.requestGateway)
  const extraLeftItems = useStatusbarContributions('left')
  const extraRightItems = useStatusbarContributions('right')

  const { leftStatusbarItems, statusbarItems } = useStatusbarItems({
    agentsOpen,
    chatOpen,
    commandCenterOpen,
    extraLeftItems,
    extraRightItems,
    freshDraftReady,
    gatewayState,
    inferenceStatus,
    openAgents: actions.openAgents,
    openCommandCenterSection: actions.openCommandCenterSection,
    requestGateway: actions.requestGateway,
    statusSnapshot,
    toggleCommandCenter: actions.toggleCommandCenter
  })

  return <StatusbarControls items={statusbarItems} leftItems={leftStatusbarItems} />
})

export const SessionTitleSurface = memo(function SessionTitleSurface({
  actions,
  isRoutedSessionView
}: {
  actions: WiringActions
  isRoutedSessionView: boolean
}) {
  return (
    <SessionTitleDropdown
      isRoutedSessionView={isRoutedSessionView}
      onDelete={actions.onDeleteSelectedSession}
      onPin={actions.onToggleSelectedPin}
    />
  )
})

/** The workspace pane: the real route table (chat + full-page views + plugin
 *  routes). Subscribes to `$gatewayState` and ROUTES_AREA itself; the gateway
 *  instance + voice cap arrive as props so a reconnect/config load re-renders
 *  only this surface. ChatView subscribes to its own session atoms, so
 *  streaming never round-trips through the controller. */
export const ChatRoutesSurface = memo(function ChatRoutesSurface({
  actions,
  maxVoiceRecordingSeconds
}: {
  actions: WiringActions
  maxVoiceRecordingSeconds?: number
}) {
  const gatewayState = useStore($gatewayState)
  useContributions(ROUTES_AREA)
  const routeContributions = contributedRoutes()

  // Recapture the live gateway instance whenever the connection state flips.
  // getGateway reads a controller ref, so gatewayState is the intentional
  // re-eval trigger (not a value the computation itself reads).
  const gateway = useMemo(
    () => actions.getGateway(),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [actions, gatewayState]
  )

  const modelMenuContent = useMemo(
    () =>
      gatewayState === 'open' ? (
        <ModelMenuPanel
          gateway={gateway || undefined}
          onSelectModel={actions.selectModel}
          requestGateway={actions.requestGateway}
        />
      ) : null,
    [actions, gateway, gatewayState]
  )

  const chatView = (
    <ChatView
      gateway={gateway}
      maxVoiceRecordingSeconds={maxVoiceRecordingSeconds}
      modelMenuContent={modelMenuContent}
      onAddContextRef={actions.onAddContextRef}
      onAddUrl={actions.onAddUrl}
      onAttachDroppedItems={actions.onAttachDroppedItems}
      onAttachImageBlob={actions.onAttachImageBlob}
      onBranchInNewChat={actions.onBranchInNewChat}
      onCancel={actions.onCancel}
      onDeleteSelectedSession={actions.onDeleteSelectedSession}
      onDismissError={actions.onDismissError}
      onEdit={actions.onEdit}
      onPasteClipboardImage={actions.onPasteClipboardImage}
      onPickFiles={actions.onPickFiles}
      onPickFolders={actions.onPickFolders}
      onPickImages={actions.onPickImages}
      onReload={actions.onReload}
      onRemoveAttachment={actions.onRemoveAttachment}
      onRestoreToMessage={actions.onRestoreToMessage}
      onRetryResume={actions.onRetryResume}
      onSteer={actions.onSteer}
      onSubmit={actions.onSubmit}
      onThreadMessagesChange={actions.onThreadMessagesChange}
      onToggleSelectedPin={actions.onToggleSelectedPin}
      onTranscribeAudio={actions.onTranscribeAudio}
    />
  )

  return (
    <Routes>
      <Route element={chatView} index />
      <Route element={chatView} path=":sessionId" />
      <Route
        element={
          <Suspense fallback={null}>
            <SkillsView setStatusbarItemGroup={setStatusbarItemGroup} />
          </Suspense>
        }
        path="skills"
      />
      <Route
        element={
          <Suspense fallback={null}>
            <MessagingView setStatusbarItemGroup={setStatusbarItemGroup} />
          </Suspense>
        }
        path="messaging"
      />
      <Route
        element={
          <Suspense fallback={null}>
            <ArtifactsView setStatusbarItemGroup={setStatusbarItemGroup} />
          </Suspense>
        }
        path="artifacts"
      />
      <Route element={null} path="agents" />
      <Route element={null} path="command-center" />
      <Route element={null} path="cron" />
      <Route element={null} path="profiles" />
      <Route element={null} path="settings" />
      <Route element={null} path="starmap" />
      {/* Registry-contributed pages (core features + plugins) render in the
          workspace pane like any built-in view — behind the same blast wall
          as every other contribution mount. */}
      {routeContributions.map(route => (
        <Route
          element={<ContribBoundary id={route.key}>{route.render()}</ContribBoundary>}
          key={route.key}
          path={route.path.slice(1)}
        />
      ))}
      <Route element={<Navigate replace to={NEW_CHAT_ROUTE} />} path="new" />
      <Route element={<LegacySessionRedirect />} path="sessions/:sessionId" />
      <Route element={<Navigate replace to={NEW_CHAT_ROUTE} />} path="*" />
    </Routes>
  )
})
