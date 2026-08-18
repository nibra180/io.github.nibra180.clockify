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
  property string workspaceName: ""
  property var running: null
  property int todaySeconds: 0
  property int weekSeconds: 0
  property var projects: []
  property bool projectsLoaded: false
  property string defaultProjectId: ""
  property string weekStart: "monday"
  property string lastError: ""
  property string note: ""
  property string fetchedAt: ""

  readonly property bool timing: running !== null
  readonly property bool refreshing: statusProcess.running
  readonly property bool actionBusy: actionProcess.running || keyProcess.running
  readonly property int refreshIntervalSec: intSetting("refreshIntervalSec", 60, 15, 3600)
  readonly property bool ready: helperPath !== ""

  signal keyRejected(string message)

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
    if (!ready || statusProcess.running) return
    var command = [helperPath, "status"]
    // The project list is only worth its extra API calls when a panel is about
    // to show it; the bar refresh runs every minute forever.
    if (withProjects === true || (!projectsLoaded && configured)) command.push("--projects")
    statusProcess.command = pythonCommand(command)
    statusProcess.running = true
  }

  function start(description, projectId) {
    runAction([helperPath, "start",
               "--description", String(description || ""),
               "--project", String(projectId === undefined || projectId === null ? "" : projectId)])
  }

  function stop() {
    runAction([helperPath, "stop"])
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
    note = "Checking the key with Clockify…"
    keyProcess.secret = trimmed
    keyProcess.command = pythonCommand([helperPath, "set-key"])
    keyProcess.running = true
  }

  function clearKey() {
    runAction([helperPath, "clear-key"])
  }

  function runAction(command, expectsSnapshot) {
    if (!ready || actionProcess.running) return
    actionProcess.expectsSnapshot = expectsSnapshot !== false
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
    if (!configured) {
      projects = []
      projectsLoaded = false
      workspaceName = ""
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
    onTriggered: root.refresh(false)
  }

  Timer {
    id: noteTimer
    interval: 2800
    repeat: false
    onTriggered: root.note = ""
  }

  Process {
    id: statusProcess
    running: false
    command: []
    stdout: StdioCollector { id: statusOut; waitForEnd: true }
    stderr: StdioCollector { id: statusErr; waitForEnd: true }
    onExited: function(exitCode) {
      if (exitCode !== 0) {
        root.lastError = root.helperFailure(statusErr.text, exitCode)
        return
      }
      root.applyPayload(statusOut.text)
    }
  }

  Process {
    id: actionProcess
    // set-config answers without a snapshot: applying it would blank the state.
    property bool expectsSnapshot: true
    running: false
    command: []
    stdout: StdioCollector { id: actionOut; waitForEnd: true }
    stderr: StdioCollector { id: actionErr; waitForEnd: true }
    onExited: function(exitCode) {
      if (exitCode !== 0) {
        root.lastError = root.helperFailure(actionErr.text, exitCode)
        return
      }
      if (!expectsSnapshot) return
      root.applyPayload(actionOut.text)
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
