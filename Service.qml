import QtQuick
import Quickshell.Io
import "Model.js" as Model

// Owns every piece of Clockify state the widget and panel read, and is the only
// thing here that talks to clockify.py. Two processes: one for the periodic
// refresh, one for user actions, so a slow refresh never delays a stop.
Item {
  id: root

  property var settings: ({})
  property string helperPath: ""

  property bool configured: false
  property string userName: ""
  property string workspaceId: ""
  property string workspaceName: ""
  property bool projectRequired: false
  property bool workspaceSettingsLoaded: false
  property var running: null
  property int todaySeconds: 0
  property int weekSeconds: 0
  property var projects: []
  property bool projectsLoaded: false
  property var recentEntries: []
  property var recentTasks: []
  property bool historyLoaded: false
  property string defaultProjectId: ""
  property string weekStart: "monday"
  property string lastError: ""
  property string note: ""
  property string fetchedAt: ""
  property int authGeneration: 0
  property bool cacheReloadNeeded: false
  property bool fullRefreshPending: false
  property bool settleFullRefresh: false

  readonly property bool timing: running !== null
  readonly property bool refreshing: statusProcess.running
  readonly property bool actionBusy: actionProcess.running || keyProcess.running
  readonly property int refreshIntervalSec: intSetting("refreshIntervalSec", 60, 15, 3600)
  readonly property bool ready: helperPath !== ""

  signal keyRejected(string message)
  signal timerStarted()

  function setting(name, fallback) {
    var value = settings ? settings[name] : undefined
    return value === undefined || value === null ? fallback : value
  }

  function intSetting(name, fallback, min, max) {
    var parsed = parseInt(String(setting(name, fallback)), 10)
    if (!isFinite(parsed)) parsed = fallback
    return Math.max(min, Math.min(max, parsed))
  }

  // ------------------------------------------------------------- commands

  function refresh(withProjects) {
    if (!ready) return
    if (statusProcess.running) {
      if (withProjects === true) fullRefreshPending = true
      return
    }
    var fullRefresh = withProjects === true || fullRefreshPending
    fullRefreshPending = false
    var command = [helperPath, "status"]
    // A full refresh loads projects, ticket history, and workspace rules.
    if (fullRefresh
        || (configured && (!projectsLoaded || !historyLoaded || !workspaceSettingsLoaded))) {
      command.push("--projects")
    }
    statusProcess.authGeneration = root.authGeneration
    statusProcess.command = pythonCommand(command)
    statusProcess.running = true
  }

  function runPendingRefresh() {
    if (fullRefreshPending) Qt.callLater(function() { root.refresh(true) })
  }

  function start(description, projectId) {
    runAction([helperPath, "start", "--description-stdin",
               "--project", String(projectId === undefined || projectId === null ? "" : projectId)],
              true, String(description || ""), "start")
  }

  function stop() {
    runAction([helperPath, "stop"], true, undefined, "stop")
  }

  function rememberProject(projectId) {
    if (!ready || actionProcess.running) return
    defaultProjectId = String(projectId || "")
    // Fire and forget: the stored default only matters for the next session, so
    // it rides the action process without a payload to apply.
    runAction([helperPath, "set-config", "--project", defaultProjectId], false)
  }

  function saveKey(key) {
    var trimmed = String(key || "").trim()
    if (!ready || trimmed === "" || keyProcess.running) return
    authGeneration += 1
    note = "Checking the key with Clockify…"
    keyProcess.secret = trimmed
    keyProcess.stdinEnabled = true
    keyProcess.command = pythonCommand([helperPath, "set-key"])
    keyProcess.running = true
  }

  function clearKey() {
    authGeneration += 1
    projects = []
    projectsLoaded = false
    recentEntries = []
    recentTasks = []
    historyLoaded = false
    workspaceId = ""
    workspaceName = ""
    projectRequired = false
    workspaceSettingsLoaded = false
    defaultProjectId = ""
    cacheReloadNeeded = false
    fullRefreshPending = false
    settleFullRefresh = false
    runAction([helperPath, "clear-key"])
  }

  function runAction(command, expectsSnapshot, stdinText, actionKind) {
    if (!ready || actionProcess.running) return
    actionProcess.expectsSnapshot = expectsSnapshot !== false
    actionProcess.actionKind = String(actionKind || "")
    actionProcess.stdinText = stdinText === undefined ? "" : String(stdinText).replace(/[\r\n]+/g, " ")
    actionProcess.stdinEnabled = stdinText !== undefined
    actionProcess.command = pythonCommand(command)
    actionProcess.running = true
  }

  // python3 is invoked explicitly rather than relying on the helper's shebang:
  // a plugin folder copied out of a tarball or a git checkout without the
  // execute bit would otherwise fail with a bare "permission denied".
  function pythonCommand(parts) {
    return ["python3"].concat(parts)
  }

  // -------------------------------------------------------------- payloads

  function applyPayload(raw) {
    var payload = Model.parsePayload(raw)

    if (payload.ok === false) {
      lastError = String(payload.error || "Clockify request failed")
      note = ""
      if (payload.configured !== undefined) configured = payload.configured === true
      return payload
    }

    var incomingWorkspaceId = String(payload.workspaceId || "")
    var workspaceChanged = workspaceId !== ""
      && incomingWorkspaceId !== ""
      && workspaceId !== incomingWorkspaceId
    if (workspaceChanged) {
      projects = []
      projectsLoaded = false
      recentEntries = []
      recentTasks = []
      historyLoaded = false
      workspaceName = ""
      projectRequired = false
      workspaceSettingsLoaded = false
    }
    workspaceId = incomingWorkspaceId

    lastError = String(payload.error || "")
    note = String(payload.note || "")
    configured = payload.configured === true
    userName = String(payload.userName || "")
    running = payload.running || null
    todaySeconds = Number(payload.todaySeconds || 0)
    weekSeconds = Number(payload.weekSeconds || 0)
    defaultProjectId = String(payload.defaultProjectId || "")
    weekStart = String(payload.weekStart || "monday")
    fetchedAt = String(payload.fetchedAt || "")

    // Mutations answer with a snapshot that skips the project list; keeping the
    // list we already have beats emptying the dropdown after every start.
    if (payload.projectsLoaded === true) {
      projects = Array.isArray(payload.projects) ? payload.projects : []
      projectsLoaded = true
      workspaceName = String(payload.workspaceName || "")
    }
    if (payload.projectsLoaded === true) {
      workspaceSettingsLoaded = payload.workspaceSettingsLoaded === true
      if (workspaceSettingsLoaded) projectRequired = payload.projectRequired === true
    }
    if (payload.historyLoaded === true) {
      recentEntries = Array.isArray(payload.recentEntries) ? payload.recentEntries : []
      recentTasks = Array.isArray(payload.recentTasks) ? payload.recentTasks : []
      historyLoaded = true
    } else if (historyLoaded && note === "Timer started" && running !== null) {
      recentEntries = Model.rememberRecentEntry(recentEntries, running, 100)
    }
    cacheReloadNeeded = configured && workspaceChanged && payload.projectsLoaded !== true
    if (!configured) {
      projects = []
      projectsLoaded = false
      recentEntries = []
      recentTasks = []
      historyLoaded = false
      workspaceId = ""
      workspaceName = ""
      projectRequired = false
      workspaceSettingsLoaded = false
      userName = ""
    }
    if (note !== "") noteTimer.restart()
    return payload
  }

  Timer {
    id: refreshTimer
    interval: root.refreshIntervalSec * 1000
    repeat: true
    running: root.ready
    onTriggered: root.refresh(false)
  }

  // A bar exists per monitor, so this service exists per monitor, and a shell
  // start or a live reload would otherwise fire every copy's first refresh in
  // the same instant. Spreading them keeps a multi-monitor setup from opening
  // a fistful of simultaneous connections to Clockify.
  Timer {
    id: firstRefresh
    interval: 250 + Math.floor(Math.random() * 2500)
    repeat: false
    running: root.ready
    onTriggered: root.refresh(true)
  }

  // Clockify needs a moment to settle a start/stop before its entry list agrees
  // with what just happened; one late re-read keeps totals honest.
  Timer {
    id: settleTimer
    interval: 2500
    repeat: false
    onTriggered: {
      var withHistory = root.settleFullRefresh
      root.settleFullRefresh = false
      root.refresh(withHistory)
    }
  }

  Timer {
    id: noteTimer
    interval: 2800
    repeat: false
    onTriggered: root.note = ""
  }

  Process {
    id: statusProcess
    property int authGeneration: -1
    running: false
    command: []
    stdout: StdioCollector { id: statusOut; waitForEnd: true }
    stderr: StdioCollector { id: statusErr; waitForEnd: true }
    onExited: function(exitCode) {
      if (authGeneration !== root.authGeneration) {
        root.runPendingRefresh()
        return
      }
      if (exitCode !== 0) {
        root.lastError = root.helperFailure(statusErr.text, exitCode)
        root.runPendingRefresh()
        return
      }
      root.applyPayload(statusOut.text)
      if (root.cacheReloadNeeded) root.fullRefreshPending = true
      root.runPendingRefresh()
    }
  }

  Process {
    id: actionProcess
    // set-config answers without a snapshot: applying it would blank the state.
    property bool expectsSnapshot: true
    property string actionKind: ""
    property string stdinText: ""
    running: false
    command: []
    onStarted: {
      if (!stdinEnabled) return
      write(stdinText + "\n")
      stdinText = ""
      stdinEnabled = false
    }
    stdout: StdioCollector { id: actionOut; waitForEnd: true }
    stderr: StdioCollector { id: actionErr; waitForEnd: true }
    onExited: function(exitCode) {
      var finishedKind = actionKind
      actionKind = ""
      stdinText = ""
      stdinEnabled = false
      if (exitCode !== 0) {
        root.lastError = root.helperFailure(actionErr.text, exitCode)
        return
      }
      if (!expectsSnapshot) return
      var payload = root.applyPayload(actionOut.text)
      if (payload.ok === false) {
        if (finishedKind === "start" && payload.reason === "PROJECT_REQUIRED") {
          root.projectRequired = true
          root.workspaceSettingsLoaded = true
          root.refresh(true)
        } else if (finishedKind === "start" && payload.reason === "TIMER_STARTED") {
          root.timerStarted()
          root.refresh(false)
          root.settleFullRefresh = true
          settleTimer.restart()
        }
        return
      }
      if (finishedKind === "start" && payload.note === "Timer started") root.timerStarted()
      root.settleFullRefresh = root.settleFullRefresh
        || finishedKind === "start"
        || finishedKind === "stop"
      settleTimer.restart()
    }
  }

  // The API key goes in over stdin so it never appears in this process's argv,
  // which any user on the machine can read out of /proc.
  Process {
    id: keyProcess
    property string secret: ""
    running: false
    command: []
    stdinEnabled: true
    stdout: StdioCollector { id: keyOut; waitForEnd: true }
    stderr: StdioCollector { id: keyErr; waitForEnd: true }
    onStarted: {
      write(secret + "\n")
      secret = ""
      stdinEnabled = false
    }
    onExited: function(exitCode) {
      if (exitCode !== 0) {
        root.lastError = root.helperFailure(keyErr.text, exitCode)
        root.keyRejected(root.lastError)
        return
      }
      var payload = root.applyPayload(keyOut.text)
      if (payload.ok === false || payload.configured !== true) {
        root.keyRejected(String(payload.error || "Clockify rejected the API key"))
      }
    }
  }

  function helperFailure(stderrText, exitCode) {
    var text = String(stderrText || "").replace(/\s+/g, " ").trim()
    if (text === "") return "clockify.py exited with code " + exitCode
    return text.length > 160 ? text.substring(0, 157) + "…" : text
  }
}
