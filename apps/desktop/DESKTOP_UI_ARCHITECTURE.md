# Hermes Desktop — UI Architecture Blueprint

> **Purpose**: Recreate the Hermes Desktop layout as a web client ("Odyssey").
> This document maps the exact visual structure, component hierarchy, store
> data, and gateway RPC calls. The desktop is an Electron + React + Vite app
> that talks JSON-RPC to the same gateway Odyssey will use.

Source root: `~/.hermes/hermes-agent/apps/desktop/src/`

---

## 1. Layout Principal — Tree-based Pane System

The entire layout is **not a fixed 3-pane CSS grid**. It is a **recursive layout
tree** (FancyZones/VSCode-style) where every zone is a node in a binary split
tree. The default preset *looks* like a 3-pane layout, but the structure is
fully user-editable, draggable, and persistable.

### Entry point

**`app/index.tsx`** → re-exports `ContribController` from `app/contrib/controller.tsx`.

There is **no `main.tsx`** in `app/` (it lives at the repo's electron entry).

### Root component: `ContribController`

**File**: `app/contrib/controller.tsx` (lines 689-778)

```tsx
<SidebarProvider className="h-screen min-h-0 flex-col bg-background" open={sidebarOpen}>
  <ContribWiring>            {/* gateway boot, sessions, streams, terminal */}
    <div className="flex h-screen min-h-0 w-screen flex-col">
      {/* 1. TITLE BAR — fixed chrome, 34px, composable via slots */}
      <div className="relative flex h-[34px] shrink-0 items-center">
        <TitlebarSlot area="titleBar.left"  ... />
        <TitlebarSlot area="titleBar.center" ... />
        <TitlebarSlot area="titleBar.right" ... />
      </div>

      {/* 2. LAYOUT TREE — fills all remaining space */}
      <LayoutTreeRoot />

      {/* 3. CLOSE-CONFIRM dialog (busy tab close gate) */}
      <SessionTileCloseConfirm />

      {/* 4. STATUS BAR — bottom, toggleable */}
      {statusbarVisible && <WiredPane part="statusbar" />}
    </div>
  </ContribWiring>
</SidebarProvider>
```

**Key stores read**: `$sidebarOpen`, `$statusbarVisible`, `isHudWindow()`.

### The Layout Tree (`LayoutTreeRoot`)

**File**: `components/pane-shell/tree/renderer/index.tsx`

Renders recursively from `$layoutTree` (a nanostores atom). Two node types:
- **`split`** → a flex row or column with resize sashes (1px seams)
- **`group`** → a **ZONE**: header strip (tabs) + pane content body

```
TreeNode (dispatch)
├── TreeSplit  → flex row/column + sashes
└── TreeGroup  → ZONE: PaneTabStrip + body (active pane content)
```

### Default Layout Preset (what you see on boot)

Defined in `controller.tsx` as `DEFAULT_TREE` (line 341):

```
ROW [1, 3.4, 1.25]
├── sessions          (LEFT sidebar)
├── workspace         (MAIN chat — always dominates)
└── COLUMN [1.6, 1]
    ├── ROW [1, 1.2]
    │   ├── review    (git diff, hidden until ⌘G)
    │   └── files     (file browser)
    └── terminal      (bottom tool panel, 20vh)
```

Four presets are registered (`layouts` area): **Default**, **Focus**, **Terminal
deck**, **Quad**.

### Pane Contributions (registry-based)

Every pane is registered through a **contribution registry** (`area: 'panes'`).
Core panes registered in `controller.tsx` (lines 139-220):

| Pane ID       | placement | render()                          | toggle        |
|---------------|-----------|-----------------------------------|---------------|
| `sessions`    | `left`    | `<WiredPane part="sidebar" />`    | `$sidebarOpen` (⌘B) |
| `workspace`   | `main`    | `<WiredPane part="chatRoutes" />` | uncloseable   |
| `terminal`    | `bottom`  | `<WiredPane part="terminal" />`   | `$terminalTakeover` (⌃\`) |
| `files`       | `right`   | `<FilesPane />`                   | `$fileBrowserOpen` (⌘J) |
| `review`      | `right`   | `<ReviewPaneContent />`           | `$reviewOpen` (⌘G) |
| `logs`        | `bottom`  | `<LogsPane />`                    | ⌘K summon only |

Each pane declares `data: { placement, width, minWidth, maxWidth, height, ... }`
for sizing clamps. Side collapse (⌘B/⌘J) hides an entire root column; tool
panels (terminal) collapse to a rail without unmounting.

### Visibility binding (controller.tsx lines 509-565)

```
bindTreeSideVisibility('left',  $sidebarOpen,    setSidebarOpen)
bindTreeSideVisibility('right', $fileBrowserOpen, setFileBrowserOpen)
bindPaneVisibility('files',   $hasWorkspace && $fileBrowserOpen, ...)
bindPaneVisibility('review',  $reviewOpen && $hasWorkspace, ...)
bindToolPaneCollapse('terminal', $terminalTakeover, ...)
```

`$hasWorkspace = computed($currentCwd, cwd => Boolean(cwd.trim()))` — files and
review panes hide when there's no project (detached chat).

---

## 2. Left Sidebar (`sessions` pane)

### Component chain

```
WiredPane part="sidebar"
  → SidebarSurface (surfaces.tsx) — memo'd
    → ChatSidebar (chat/sidebar/index.tsx)
```

**File**: `app/chat/sidebar/index.tsx` (1553 lines — the largest single component)

### Visual structure (top to bottom)

```
<Sidebar> (shadcn Sidebar, collapsible="none")
  <SidebarContent>
    1. NAV GROUP (shrink-0) — SIDEBAR_NAV array:
       ├── New Session (Codicon "robot", ⌘N, action: 'new-session')
       ├── Skills      (Codicon "symbol-misc", route: /skills)
       ├── Messaging   (Codicon "comment", route: /messaging)
       └── Artifacts   (Codicon "files", route: /artifacts)
       + contributedNav (plugins, same chrome)

    2. SEARCH FIELD (SearchField) — full-text session search
       - Debounced 200ms → searchSessions() RPC
       - Client-side match on loaded sessions + server results merged

    3. SCROLL AREA (flex-1, overflow-y-auto):
       IF searching:
         └── SidebarSessionsSection "Results" (searchResults)

       IF NOT searching:
         ├── SidebarSessionsSection "Pinned" (pinnedSessions, sortable)
         │   - open/closed via $sidebarPinsOpen
         │   - reorder via dnd-kit (reorderPinned)
         │
         ├── SidebarSessionsSection "Sessions"/"Projects" (displayAgentSessions)
         │   - dateGrouped (Today, Yesterday, Last week, …)
         │   - project tree mode (worktreeGroupingActive) OR flat recents
         │   - toggle between Projects view / Sessions view ($sidebarAgentsGrouped)
         │   - "Load more" footer (SidebarLoadMoreRow)
         │
         ├── [messaging groups] — per-platform sections (Telegram, Discord, …)
         │   - SidebarSessionsSection per sourceId
         │   - NON_SESSION_INITIAL_ROWS=3, load more in steps of 10
         │
         └── SidebarCronJobsSection (if cronJobs.length > 0)
             - open/closed via $sidebarCronOpen

    4. PROFILE RAIL (shrink-0) — <ProfileRail /> at bottom
       - Only shows when profiles.length > 1 (multi-profile)

  <ProjectDialog />   (project create/rename)
  <WorktreeDialog />  (worktree management)
```

### SIDEBAR_NAV definition (lines 145-174)

```ts
const SIDEBAR_NAV: SidebarNavItem[] = [
  { id: 'new-session', icon: Codicon("robot"), action: 'new-session', keybindActionId: 'session.new' },
  { id: 'skills',      icon: Codicon("symbol-misc"), route: SKILLS_ROUTE, keybindActionId: 'nav.skills' },
  { id: 'messaging',   icon: Codicon("comment"), route: MESSAGING_ROUTE, keybindActionId: 'nav.messaging' },
  { id: 'artifacts',   icon: Codicon("files"), route: ARTIFACTS_ROUTE, keybindActionId: 'nav.artifacts' }
]
```

### Props (ChatSidebarProps)

```ts
{
  currentView: AppView
  onNavigate: (item: SidebarNavItem) => void
  onLoadMoreSessions: () => Promise<void>
  onResumeSession: (sessionId: string) => void
  onDeleteSession: (sessionId: string) => void
  onArchiveSession: (sessionId: string) => void
  onBranchSession: (sessionId: string) => void
  onNewSessionInWorkspace: (path: null | string) => void
  onNewSessionSplit: (dir: SplitDir) => void
  onManageCronJob: (jobId: string) => void
  onTriggerCronJob: (jobId: string) => void
  ...
}
```

### Key stores read

| Store | Purpose |
|-------|---------|
| `$sessions` | Recents list (SessionInfo[]) |
| `$messagingSessions` | Platform sessions (Telegram/Discord) |
| `$cronSessions`, `$cronJobs` | Cron-triggered sessions |
| `$pinnedSessionIds`, `$sidebarPinsOpen` | Pinned section |
| `$selectedStoredSessionId` (via `$focusedStoredSessionId`) | Active highlight |
| `$profiles`, `$profileScope` | Profile rail / ALL-profiles grouping |
| `$projects`, `$projectTree`, `$activeProjectId` | Project tree mode |
| `$currentCwd` | Workspace context |
| `$workingSessionIds` | Running session indicators |
| `$panesFlipped` | Sidebar border side |
| `$bindings['session.new']` | ⌘N kbd hint |

### Gateway RPC calls

- `searchSessions(query)` — full-text search across all sessions
- Session resume/delete/archive/branch — via `onResumeSession` etc. (wired to
  `useSessionActions` in `session/hooks/`)

### Row chrome (`chrome.tsx`)

Shared row primitives used by all sidebar sections:
- `SidebarRowShell` — grid `minmax(0,1fr) auto`, `min-h-[1.625rem]`
- `SidebarRowBody` — main tap target (RowButton)
- `SidebarRowLead` — icon/dot column (`size-3.5`)
- `SidebarRowGrab` — dnd-kit drag handle (dot ↔ grabber swap)
- `SidebarDateDivider` — "Yesterday" / "Last week" caption + hairline

---

## 3. Session Tabs (multi-session tiling)

### There is NO `session-switcher.tsx` in `app/chat/`

The file `app/session-switcher.tsx` exists but is a **compact ⌃Tab quick-switcher
HUD** (keyboard-driven, portal-rendered). The actual **tab strip** is rendered
by the layout tree's `TreeGroup`.

### Tab rendering: `TreeGroup`

**File**: `components/pane-shell/tree/renderer/tree-group.tsx`

Each zone renders a `<PaneTabStrip>` containing:
- One `<PaneTab>` per pane in the zone's group (tabs when stacked ≥2)
- A trailing **"+" button** after the last tab (for chat zones only):

```tsx
{shown.some(isSessionStripPane) && newSessionTabAction && !node.minimized && (
  <PaneStripGlyph
    icon={<Codicon name="add" />}
    label={t.zones.newSessionTab}
    onSelect={() => newSessionTabAction()}   // = ⌘T
  />
)}
```

**`$newSessionTabAction`** (store.ts line 536) is an atom holding a `() => void`
registered by the app wiring. It opens a new session as a tab in the focused
zone, reusing an existing empty tab if one exists.

### Session tiles (`session-tile.tsx`)

**File**: `app/chat/session-tile.tsx`

A **session tile** is a stored session rendered as its own layout-tree pane
beside the main thread. Key points:
- `openSessionTile(storedId)` → `watchSessionTiles()` registers a pane
  contribution (`session-tile:${storedId}`) docked right of main
- A tile IS the real chat surface: same `ChatView`/`ChatBar`/`Thread`, under its
  own `SessionView` (session slice from `$sessionStates`) and `ComposerScope`
  (own attachments, own focus key)
- Lifecycle: resume on boot via `sessionTileDelegate().resumeTile()`
- Close gating: busy/input-blocked tabs show `SessionTileCloseConfirm` dialog
- Tab title: `tileTitle()` → stored row title, or "New session" for unlisted tabs

### Main tab behavior

The `workspace` pane (main) is **uncloseable** — closing it via ⌘W empties it:
1. If session tabs are stacked → next tile shifts INTO main
2. If nothing stacked → drops to fresh "New session" draft
3. Full-page views (skills/artifacts) return false (no-op)

See `close-tab.ts` → `closeWorkspaceTab()` / `closeActiveTab()`.

### Tab close logic (`close-tab.ts`)

```ts
closeActiveTab():
  1. Terminal focused? → closeActiveTerminal()
  2. Focused chat zone has closeable tab? → closeFocusedSessionTab()
  3. Focused tool panel? → closeFocusedToolTab()
  4. Else → closeWorkspaceTab() (empty main to fresh draft)
```

### ⌃Tab quick-switcher (`session-switcher.tsx`)

Portal-rendered HUD, keyboard-driven, shows all sessions with status dots:
- `working` (pulsing accent), `attention` (amber), `unread` (emerald)
- ⌃1…⌃9 shortcuts visible

---

## 4. Right Sidebar / Tool Panels

The "right sidebar" is **not a single component** — it's multiple independent
panes in the layout tree. The right column of the default tree holds:
`review` | `files` (in a row split), with `terminal` below.

### Files pane (`RightSidebarPane`)

**File**: `app/right-sidebar/index.tsx`

```tsx
<aside aria-label="files">
  <FilesystemTab>  (if hasWorkspace)
    <RightSidebarSectionHeader>
      <SidebarPanelLabel>{cwdName}</SidebarPanelLabel>
      <Button refresh /> <Button collapse-all />
    </RightSidebarSectionHeader>
    <FileTreeBody>
      <ProjectTree ... />  (the actual tree component)
    </FileTreeBody>
  </FilesystemTab>
  OR <PaneEmptyState label="No project open" />
</aside>
```

**Data**: `useProjectTree(currentCwd)` → file tree via gateway.
**RPC**: filesystem reads through the gateway (files.list, etc.)
**Action**: `previewFile(path)` → `normalizeOrLocalPreviewTarget` → `openPreview()`

### Terminal pane (`PersistentTerminal`)

**File**: `app/right-sidebar/terminal/persistent.tsx`

A single xterm.js Terminal mounted at the layout root, **CSS-overlayed** onto
the active `<TerminalSlot />` via `position:fixed` + rect tracking. This avoids
detaching xterm's WebGL renderer when moving between zones.

```tsx
<PersistentTerminal>
  <div style={{ position: 'fixed', top, left, width, height, ... }}>
    <TerminalWorkspace onAddSelectionToChat={...} />
  </div>
</PersistentTerminal>
```

- Keep-alive: PTYs stay alive while hidden (collapsed to rail)
- ResizeObserver + MutationObserver + layout-tree subscription track the slot
- `$terminalTakeover` gates visibility

**File**: `app/right-sidebar/terminal/chrome.tsx` → `TerminalPaneChrome` (tabs,
new terminal, split)

### Review pane (`ReviewPane`)

**File**: `app/right-sidebar/review/` — git diff viewer, toggled by ⌘G
(`$reviewOpen`). Keyed by cwd so switching projects rebuilds diff state.

### Tab toggling

Tabs within a zone are handled by the `TreeGroup` renderer — clicking a
`<PaneTab>` activates it (`activateTreePane(id)`). Side panels toggle via:
- ⌘B → `$sidebarOpen` (left column)
- ⌘J → `$fileBrowserOpen` (right column / files)
- ⌘G → `$reviewOpen` (review)
- ⌃\` → `$terminalTakeover` (terminal)

---

## 5. Composer / Input Bar

### Component chain

```
ChatView (chat/index.tsx)
  → ChatRuntimeBoundary (assistant-ui runtime provider)
    → Thread (message list)
    → ChatBar (chat/composer/index.tsx)  ← THE INPUT BAR
```

### `ChatBar` (`chat/composer/index.tsx`)

Props (`ChatBarProps`):
```ts
{
  busy, cwd, disabled, sessionId, state: ChatBarState,
  gateway: HermesGateway,
  onCancel, onSubmit, onSteer,
  onAttachDroppedItems, onAttachImageBlob, onPasteClipboardImage,
  onPickFiles, onPickFolders, onPickImages,
  onRemoveAttachment, onAddUrl, onTranscribeAudio,
  ...
}
```

### Rendered structure (simplified)

```
<ComposerPrimitive.Root className="rounded-2xl">  (the pill)
  ├─ [HelpHint]                 (if isHelpHint)
  ├─ [ComposerTriggerPopover]   (@//, /slash, :emoji completions)
  ├─ [drag region]              (pop-out grab margin)
  └─ <div className="composer-surface">  (the visible surface)
      ├─ [CodingStatusRow]      (branch/worktree actions)
      └─ <div className="grid [menu|input|controls]">
          ├─ [grid-area: menu]
          │   └─ <ContextMenu />     ← the "+" "Add context" button
          │       DropdownMenu: Files, Folder, Images, Paste image,
          │                     URL, Prompt snippets, + contributed
          ├─ [grid-area: input]
          │   └─ <div contentEditable>  (rich editor, NOT textarea)
          │       + <ComposerDirectiveActions />
          │       + <ComposerPrimitive.Input> (sr-only textarea for AUI binding)
          └─ [grid-area: controls]
              └─ <ComposerControls />  ← model pill + voice + send/stop
```

Above the composer (in the "dock"):
```
<ActionBadges />           (contributed micro-actions)
<ComposerStatusStack />    (todos, subagents, background tasks, queue)
```

### `ContextMenu` ("+" button — `chat/composer/context-menu.tsx`)

```tsx
<DropdownMenu>
  <DropdownMenuTrigger>  ← Codicon "add" (the "+" button)
  <DropdownMenuContent>
    <DropdownMenuLabel>Attach</DropdownMenuLabel>
    <ContextMenuItem icon={FileText}   onSelect={onPickFiles}>Files</ContextMenuItem>
    <ContextMenuItem icon={FolderOpen} onSelect={onPickFolders}>Folder</ContextMenuItem>
    <ContextMenuItem icon={ImageIcon}  onSelect={onPickImages}>Images</ContextMenuItem>
    <ContextMenuItem icon={Clipboard}  onSelect={onPasteClipboardImage}>Paste image</ContextMenuItem>
    <ContextMenuItem icon={Link}       onSelect={onOpenUrlDialog}>URL</ContextMenuItem>
    <DropdownMenuSeparator />
    <ContextMenuItem icon={MessageSquareText}>Prompt snippets →</ContextMenuItem>
    {attachmentProviders.map(...)}   (contributed)
    <Kbd>@</Kbd> tip
  </DropdownMenuContent>
</DropdownMenu>
```

### `ComposerControls` (`chat/composer/controls.tsx`)

Right-aligned cluster (`ml-auto`):
```
<ModelPill />          ← model selector (compact variant available)
<DictationButton />    ← mic (Codicon "mic" / Square when recording)
<AutoSpeakButton />    ← Volume2/VolumeX (TTS replies toggle)
<WakeWordButton />     ← Ear/EarOff ("Hey Hermes" wake word)
[if steer] <Queue />   ← Layers3 icon (steer/queue-while-busy)
<SendButton />         ← PRIMARY_ICON_BTN:
                          - not busy + empty → AudioLines (voice start)
                          - not busy + has text → Codicon "arrow-up" (send)
                          - busy + steer → SteeringWheel
                          - busy + queue → Layers3
                          - busy + stop → square stop icon
```

When a voice conversation is active, the whole cluster is replaced by
`<ConversationPill />` (mute, stop-listening, end-conversation with audio
level bars).

### `AttachmentList` (`chat/composer/attachments.tsx`)

Renders attachment chips above the input row:
```
<div className="flex flex-wrap gap-1.5">
  {attachments.map(a => <AttachmentPill kind={a.kind} ... />)}
</div>
```
Kinds: `folder`, `url`, `image`, `file`, `terminal`. Each pill shows icon +
label + detail + remove (×) on hover. Images open in `<ImageLightbox />`.

### Key stores / hooks used by ChatBar

- `useComposerDraft` — the draft engine (DOM contentEditable + draftRef)
- `useComposerQueue` — queued turns, in-place editing
- `useComposerSubmit` — submit/steer/queue decision tree
- `useComposerVoice` / `useMicRecorder` — dictation + voice conversation
- `useAtCompletions` — @// file/dir suggestions (gateway)
- `useSlashCompletions` — /command suggestions
- `$gatewayState` — reconnecting state
- `sessionCompacting(sessionId)` — compaction in progress
- `scope.attachments.$attachments` — per-scope (main/tile) attachment set

### Gateway RPC calls (via `useComposerActions`)

- File picker → `files.read` / attachment upload
- URL dialog → inline ref insertion
- Submit → `prompt.submit` (streamed via WebSocket gateway events)
- Voice → `audio.transcribe`
- @suggestions → context suggestion query

---

## 6. Status Bar

### Component chain

```
WiredPane part="statusbar"
  → StatusbarSurface (surfaces.tsx) — memo'd, owns data hooks
    → StatusbarControls (shell/statusbar-controls.tsx)
```

### `StatusbarControls` (`shell/statusbar-controls.tsx`)

```tsx
<footer className="flex h-5 items-stretch justify-between">
  <div className="left items">  ← leftItems (core + contributed)
    {leftItems.filter(visible).map(item => <StatusbarItemView />)}
  </div>
  <div className="right items"> ← items (core + contributed)
    {items.filter(visible).map(item => <StatusbarItemView />)}
  </div>
  <StatusbarVisibilityMenu />   (right-click → show/hide items)
</footer>
```

Each `StatusbarItem` can be: `action` (button), `link` (anchor), `menu`
(dropdown), `text` (label), or `render()` (arbitrary node escape hatch).

### `useStatusbarItems` (`shell/hooks/use-statusbar-items.tsx`)

Builds the core items array. **LEFT cluster** (coreLeftStatusbarItems):

| ID | Icon | Label | Detail | Action |
|----|------|-------|--------|--------|
| `connection` | Terminal | SSH/Cloud/Remote host | — | navigate settings |
| `command-center` | Command | — | — | toggleCommandCenter |
| `gateway-health` | Activity/AlertCircle | "Gateway" | Ready/Needs setup/Connecting/Offline | menu: GatewayMenuPanel |
| `workspace-cwd` | FolderOpen | project name or cwd leaf | — | menu: copy/reveal path |
| `agents` | hubot/Loader2 | "Agents" | count running/failed | openAgents |
| `cron` | Clock | "Cron" | — | navigate /cron |
| `webhooks` | Globe | "Webhooks" | — | navigate /webhooks |

**RIGHT cluster** (coreRightStatusbarItems):

| ID | Icon | Label | Detail | Action |
|----|------|-------|--------|--------|
| `running-timer` | Loader2 | "Turn" | `<LiveDuration>` | text (hidden unless busy) |
| `context-usage` | — | usage label | context bar % | menu: ContextUsagePanel |
| `session-timer` | — | "Session" | `<LiveDuration>` | text |
| approval-mode | — | mode label | — | menu |
| `terminal` | Terminal | — | — | togglePaneVisible('terminal') |
| `version-client` | Hash | version | behind count | openUpdateOverlay |
| `version-backend` | Hash | version | — | (remote only) |

### Stores read (statusbar tracks FOCUSED session)

| Store | What |
|-------|------|
| `$activeSessionId`, `$busy` | Primary busy state |
| `$focusedStoredSessionId`, `$focusedRuntimeId` | Focused tile session |
| `$focusedSessionState` (selected fields) | busy, turnStartedAt, usage, cwd |
| `$currentUsage` | Context usage stats |
| `$sessions` (selected scalars) | Focused row cwd/startedAt |
| `$projectTree` | Project name for cwd |
| `$paneVisible('terminal')` | Terminal on-screen state |
| `$subagentsBySession` (counts) | Running/failed subagent totals |
| `$updateStatus`, `$desktopVersion` | Update indicators |
| `$connection` | Remote mode/host |
| `$gatewayRestarting` | Restart spinner |
| `$statusbarHiddenIds` | Per-item show/hide prefs |

### Gateway RPC

- `status.get` (polled every 15s via `useStatusSnapshot`)
- `usage` (context usage panel)
- Inference readiness check

---

## 7. Wiring (`ContribWiring`)

**File**: `app/contrib/wiring.tsx` (1134 lines)

This is the controller that bootstraps everything. It mounts inside
`ContribController` and provides:

### Hooks chain (order matters)
1. `useGatewayBoot` — connect to gateway WebSocket
2. `useGatewayRequest` — `gateway`, `requestGateway` for RPC
3. `useSessionListActions` — load/refresh sessions, messaging, cron
4. `useSessionStateCache` — runtime↔stored id mapping, message cache
5. `useCwdActions` — workspace branch detection
6. `useHermesConfig` — STT/TTS config
7. `useModelControls` — model selection (applySavedMainModel, selectModel)
8. `useMessageStream` — WebSocket event handler (deltas, tool calls, todos)
9. `usePreviewRouting` — agent-driven preview open
10. `useContextSuggestions` — @mention file/dir suggestions
11. `useSessionActions` — resume/branch/archive/delete/create
12. `usePromptActions` — submit/steer/slash/edit/rewind
13. `useRouteResume` — resume session from URL route
14. `useBackgroundSync` — background profile polling
15. `useKeybinds` — global keyboard shortcuts
16. `useOverlayRouting` — agents/settings/cron/command-center overlays

### Overlays mounted by wiring
- `BootFailureOverlay`, `DesktopInstallOverlay`
- `GatewayConnectingOverlay`
- `DesktopOnboardingOverlay`
- `CommandPalette` (⌘K)
- `FindBar`
- `NotificationStack`
- `UpdatesOverlay`, `ModelPickerOverlay`, `ModelVisibilityOverlay`
- `SessionPickerOverlay`, `SessionSwitcher` (⌃Tab)
- `FloatingPet`, `RemoteDisplayBanner`
- Lazy overlay views: AgentsView, CommandCenterView, CronView, WebhooksView,
  ProfilesView, SettingsView, StarmapView

### `WiredPane` (context consumer)

Registered panes render `<WiredPane part="sidebar|chatRoutes|terminal|statusbar" />`
which reads the `ContribWiringContext` and mounts the corresponding surface
(`SidebarSurface`, `ChatRoutesSurface`, `TerminalSurface`, `StatusbarSurface`).

---

## 8. Chat Routes (`ChatRoutesSurface`)

**File**: `app/contrib/surfaces.tsx` (lines 109-192)

The workspace pane is a React Router `<Routes>`:

```tsx
<Routes>
  <Route element={chatView} index />              {/* new session */}
  <Route element={chatView} path=":sessionId" />  {/* existing session */}
  <Route element={page(<SkillsView />)}    path="skills" />
  <Route element={page(<MessagingView />)} path="messaging" />
  <Route element={page(<ArtifactsView />)} path="artifacts" />
  {/* overlay routes render null (handled by overlays) */}
  <Route element={null} path="agents|command-center|cron|profiles|settings|starmap|webhooks" />
  {/* plugin-contributed routes */}
  {routeContributions.map(...)}
</Routes>
```

`chatView = <ChatView gateway={...} modelMenuContent={...} {...chatActions} />`

---

## 9. Store Map (nanostores atoms)

Key stores by concern:

| Store | File | Purpose |
|-------|------|---------|
| `$layoutTree` | `components/pane-shell/tree/store.ts` | The layout tree structure |
| `$sidebarOpen`, `$fileBrowserOpen`, `$panesFlipped` | `store/layout.ts` | Side panel toggles |
| `$sessions`, `$activeSessionId`, `$selectedStoredSessionId` | `store/session.ts` | Session list + selection |
| `$messages`, `$busy`, `$currentCwd`, `$currentUsage` | `store/session.ts` | Active session state |
| `$sessionStates`, `$sessionTiles` | `store/session-states.ts` | Per-runtime session slices + tiles |
| `$focusedStoredSessionId`, `$focusedRuntimeId` | `store/session-states.ts` | Focused tile tracking |
| `$pinnedSessionIds` | `store/layout.ts` | Pinned sessions |
| `$profiles`, `$profileScope`, `$activeGatewayProfile` | `store/profile.ts` | Profile management |
| `$projects`, `$projectTree`, `$activeProjectId` | `store/projects.ts` | Project tree |
| `$cronJobs`, `$cronSessions` | `store/cron.ts` | Cron jobs |
| `$messagingSessions` | `store/session.ts` | Platform messaging sessions |
| `$gatewayState`, `$connection` | `store/session.ts` | Gateway connection |
| `$statusbarVisible`, `$statusbarHiddenIds` | `store/statusbar-prefs.ts` | Statusbar prefs |
| `$reviewOpen` | `store/review.ts` | Review pane toggle |
| `$terminalTakeover` | `app/right-sidebar/store.ts` | Terminal toggle |
| `$newSessionTabAction` | `tree/store.ts` | "+" tab action callback |

---

## 10. Gateway RPC Summary

The desktop calls these JSON-RPC methods (via `requestGateway` / `@/hermes`):

| Method | Used by |
|--------|---------|
| `session.list` | Sidebar sessions list |
| `session.resume` | Open/resume a session |
| `session.create` | New session |
| `session.delete`, `session.archive` | Session management |
| `session.messages` | Hydrate transcript |
| `prompt.submit` | Send a message (streamed via WS) |
| `prompt.cancel` | Stop a run |
| `search sessions` | Sidebar full-text search |
| `status.get` | Statusbar health poll |
| `usage` / `usage.snapshot` | Context usage panel |
| `files.list`, `files.read` | File browser tree |
| `audio.transcribe` | Voice dictation |
| `model.options` | Model selector |
| `cron.list`, `cron.trigger` | Cron jobs |
| `git.diff`, `git.status` | Review pane |
| WebSocket events | `useMessageStream` — deltas, tool calls, todos, compaction |

---

## Blueprint Summary for Odyssey Web

To recreate this layout in a web client:

1. **Adopt a layout tree model** (not fixed CSS grid). Implement `split`/`group`
   nodes with flex + resize sashes. Persist the tree.
2. **4 default zones**: sidebar (left), chat (main), files+review (right),
   terminal (bottom). Terminal is a tool panel (collapse-to-rail).
3. **Sidebar**: nav buttons (New/Skills/Messaging/Artifacts) → search →
   Pinned/Recents/Messaging/Cron sections → profile rail at bottom.
4. **Chat pane**: React Router with `:sessionId` routes. Each tab is a pane in
   the tree's main group; "+" opens new session tab.
5. **Composer**: rounded pill with contentEditable rich editor. Left: "+"
   context menu. Right: model pill + voice buttons + send/stop. Attachments
   as chips above. Status stack (todos/subagents/queue) above that.
6. **Status bar**: 5px-tall footer, left cluster (gateway/cwd/agents/cron) +
   right cluster (timers/usage/terminal/version). Right-click to customize.
7. **All state via nanostores** (or equivalent). Gateway over WebSocket +
   JSON-RPC REST fallback.
8. **Contribution registry** pattern: panes, statusbar items, titlebar tools,
   nav items, and composer areas are all extensible through `registry.register`.
