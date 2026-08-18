import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

// Bar widget and popup for Clockify. One entry point, like the first-party
// dropbox plugin: the bar glyph shows whether a timer runs, and the popup starts
// it, stops it, and totals the day and the week.
Panel {
  id: root
  moduleName: "io.github.matyssxdxd.clockify"
  ipcTarget: "io.github.matyssxdxd.clockify"
  manageIpc: false

  // The helper lives next to this file. Resolving it off the component URL
  // works for a bar widget, which the host hands `bar` / `settings` but no
  // manifest to read `__sourceDir` from.
  readonly property string helperPath: String(Qt.resolvedUrl("clockify.py")).replace(/^file:\/\//, "")

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  readonly property bool hideWhenIdle: setting("hideWhenIdle", false) === true

  // Live totals. The helper only counts closed entries, so the running entry's
  // elapsed time is added here where a clock is already ticking.
  readonly property int elapsedSeconds: clockify.timing ? Model.elapsedSeconds(clockify.running.start, clock.date) : 0
  readonly property int todaySeconds: clockify.todaySeconds + elapsedSeconds
  readonly property int weekSeconds: clockify.weekSeconds + elapsedSeconds

  // Draft entry. Empty while a timer runs: the hero shows what is running, and
  // typing here describes the *next* task, which Start switches over to.
  property string draftDescription: ""
  property string draftProjectId: ""

  // Panel cursor. Sections are recomputed from state, so the login form and the
  // form fields are all just entries in one flat list.
  property bool cursorActive: false
  property int cursorIndex: 0
  readonly property var sections: clockify.configured ? ["description", "project"] : ["key"]
  readonly property string currentSection: sections.length === 0
    ? ""
    : String(sections[Math.max(0, Math.min(sections.length - 1, cursorIndex))])
  readonly property string statusText: Model.statusLine(clockify.lastError, clockify.note, clockify.configured)
  readonly property bool statusIsError: clockify.lastError !== ""

  function sectionHasCursor(name) {
    return cursorActive && currentSection === name
  }

  // Mouse hover and Enter both move the cursor by name; a section that is not on
  // screen right now (the key field, once connected) leaves it where it was.
  function focusSection(name) {
    var index = sections.indexOf(name)
    if (index === -1) return
    cursorActive = true
    cursorIndex = index
  }

  function moveCursor(dy) {
    cursorActive = true
    if (dy === 0 || sections.length === 0) return
    cursorIndex = Math.max(0, Math.min(sections.length - 1, cursorIndex + dy))
  }

  function activateCursor() {
    var section = currentSection
    if (section === "key") keyField.forceActiveFocus()
    else if (section === "description") descriptionField.forceActiveFocus()
    else if (section === "project") projectDropdown.open()
  }

  function toggleTimer() {
    if (clockify.actionBusy) return
    if (clockify.timing) clockify.stop()
    else clockify.start(draftDescription, draftProjectId)
    clearDraftDescription()
  }

  // A timer is running now, so the draft has been consumed. The field's own
  // `text` is set rather than only the property: typing into a TextField breaks
  // a declarative binding, so the property alone would leave the text on screen.
  function clearDraftDescription() {
    draftDescription = ""
    descriptionField.text = ""
  }

  function commitKey() {
    var key = keyField.text
    if (String(key).trim() === "") return
    keyField.text = ""
    keyField.focus = false
    clockify.saveKey(key)
  }

  // Hiding an idle widget is a legitimate preference, but hiding an unconnected
  // one would leave no way to reach the panel and paste a key.
  visible: clockify.timing || !hideWhenIdle || !clockify.configured
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onOpenedChanged: if (opened) {
    cursorActive = false
    cursorIndex = 0
    if (panelFlick) panelFlick.contentY = 0
    draftProjectId = clockify.defaultProjectId
    clockify.refresh(true)
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  Service {
    id: clockify
    settings: root.settings
    helperPath: root.helperPath
    onKeyRejected: function(message) { Qt.callLater(function() { keyField.forceActiveFocus() }) }
  }

  // Seconds only matter while the popup shows a running timer; nothing outside it
  // counts them, and repainting every monitor's bar each second would be waste.
  SystemClock {
    id: clock
    precision: root.opened && clockify.timing ? SystemClock.Seconds : SystemClock.Minutes
  }

  Connections {
    target: clockify
    function onDefaultProjectIdChanged() {
      if (root.draftProjectId === "") root.draftProjectId = clockify.defaultProjectId
    }
  }

  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): string { clockify.refresh(true); return "ok" }
    function startTimer(description: string, projectId: string): string {
      clockify.start(description, projectId)
      return "ok"
    }
    function stopTimer(): string { clockify.stop(); return "ok" }
    function toggleTimer(): string { root.toggleTimer(); return "ok" }
    function status(): string {
      if (!clockify.configured) return "not configured"
      if (!clockify.timing) return "idle, today " + Model.formatDuration(root.todaySeconds)
      return Model.describeEntry(clockify.running) + " " + Model.formatClock(root.elapsedSeconds)
    }
    // A bar exists per monitor, so this widget exists per monitor, and only one
    // of them owns this IPC target. Reporting which one, when it last heard from
    // Clockify, and its last error is the difference between debugging this
    // plugin and guessing at it.
    function debug(): string {
      var window = root.QsWindow ? root.QsWindow.window : null
      return JSON.stringify({
        screen: window && window.screen ? String(window.screen.name) : "unknown",
        configured: clockify.configured,
        timing: clockify.timing,
        running: clockify.running,
        todaySeconds: root.todaySeconds,
        weekSeconds: root.weekSeconds,
        projectCount: clockify.projects.length,
        fetchedAt: clockify.fetchedAt,
        lastError: clockify.lastError,
        helperPath: root.helperPath
      })
    }
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    // The glyph alone -- bold while a timer runs, dimmed when none does. `text`
    // still sizes the pill and feeds the hover tooltip, but WidgetButton's own
    // label is hidden: it exposes no font weight, so the visible copy below is
    // the one that can be bound to.
    text: "󱎫"
    labelVisible: false
    dimmed: !clockify.timing
    tooltipText: Model.barTooltip(clockify.running, root.elapsedSeconds, root.todaySeconds)
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) root.toggleTimer()
      else if (buttonCode === Qt.MiddleButton) clockify.refresh(false)
      else root.toggle()
    }

    Text {
      anchors.centerIn: parent
      text: button.text
      color: button.foreground
      font.family: button.fontFamily
      font.pixelSize: button.fontSize
      font.bold: clockify.timing
      renderType: Text.NativeRendering
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(380))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(560))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      // Whatever owns the text cursor owns the keys: j/k and s/r must not fight
      // with someone typing a task description.
      blocked: descriptionField.activeFocus || keyField.activeFocus || projectDropdown.popupOpen
      onMoveRequested: function(dx, dy) {
        if (!root.cursorActive) { root.cursorActive = true; return }
        root.moveCursor(dy)
      }
      onActivateRequested: if (root.cursorActive) root.activateCursor()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(text) {
        if (text === "s" || text === "S") root.toggleTimer()
        else if (text === "r" || text === "R") clockify.refresh(true)
        else if (text === "p" || text === "P") projectDropdown.open()
        else if (text === "d" || text === "D") descriptionField.forceActiveFocus()
      }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: column
          width: panelFlick.width
          spacing: Style.space(12)

          PanelHero {
            id: hero
            width: parent.width
            title: clockify.timing ? Model.describeEntry(clockify.running) : "No timer running"
            meta: clockify.timing
              ? Model.formatClock(root.elapsedSeconds) + (Model.projectLabel(clockify.running) === "" ? "" : "  ·  " + Model.projectLabel(clockify.running))
              : (clockify.configured ? "Today " + Model.formatDuration(root.todaySeconds) : "Not connected")
            foreground: root.foreground
            fontFamily: root.fontFamily
            iconOpacity: clockify.timing ? 1.0 : 0.5
            iconComponent: Component {
              Text {
                text: "󱎫"
                color: clockify.timing ? root.foreground : root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.display
              }
            }
          }

          Text {
            visible: root.statusText !== ""
            width: parent.width
            text: root.statusText
            color: root.statusIsError ? root.urgent : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          // ---------------------------------------------------------- login

          Column {
            visible: !clockify.configured
            width: parent.width
            spacing: Style.space(8)

            PanelSectionHeader {
              text: "API KEY"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            RowLayout {
              width: parent.width
              spacing: Style.space(8)

              TextField {
                id: keyField
                Layout.fillWidth: true
                password: true
                placeholderText: "Paste your Clockify API key"
                foreground: root.foreground
                font.family: root.fontFamily
                enabled: !clockify.actionBusy
                hasCursor: !activeFocus && root.sectionHasCursor("key")
                onHoveredChanged: if (hovered) root.focusSection("key")
                onAccepted: root.commitKey()
                Keys.onPressed: function(event) {
                  if (event.key === Qt.Key_Escape) {
                    focus = false
                    event.accepted = true
                  }
                }
              }

              Button {
                text: "Connect"
                iconText: "󰌆"
                bordered: true
                enabled: !clockify.actionBusy && keyField.text !== ""
                foreground: root.foreground
                fontFamily: root.fontFamily
                Layout.alignment: Qt.AlignVCenter
                onClicked: root.commitKey()
              }
            }

            Text {
              width: parent.width
              text: "Clockify → Preferences → Advanced → API. The key is stored in ~/.config/omarchy/clockify.json with 0600 permissions."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }
          }

          // ----------------------------------------------------- new entry

          Column {
            visible: clockify.configured
            width: parent.width
            spacing: Style.space(8)

            PanelSectionHeader {
              text: clockify.timing ? "SWITCH TO" : "START TIMING"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            TextField {
              id: descriptionField
              width: parent.width
              placeholderText: clockify.timing ? "Describe the next task" : "What are you working on?"
              foreground: root.foreground
              font.family: root.fontFamily
              enabled: !clockify.actionBusy
              hasCursor: !activeFocus && root.sectionHasCursor("description")
              onTextChanged: root.draftDescription = text
              onHoveredChanged: if (hovered) root.focusSection("description")
              // Enter starts timing this description straight away, switching
              // off whatever was running.
              onAccepted: {
                focus = false
                clockify.start(root.draftDescription, root.draftProjectId)
                root.clearDraftDescription()
              }
              Keys.onPressed: function(event) {
                if (event.key === Qt.Key_Escape) {
                  focus = false
                  event.accepted = true
                }
              }
            }

            RowLayout {
              width: parent.width
              spacing: Style.space(8)

              SearchableDropdown {
                id: projectDropdown
                Layout.fillWidth: true
                showLabel: false
                placeholderText: "Filter projects"
                emptyText: clockify.projectsLoaded ? "No matching project" : "Loading projects…"
                triggerLabel: "No project"
                options: Model.projectOptions(clockify.projects, root.draftProjectId)
                value: root.draftProjectId
                foreground: root.foreground
                fontFamily: root.fontFamily
                hasCursor: root.sectionHasCursor("project")
                onHovered: function(on) { if (on) root.focusSection("project") }
                onChanged: function(value) {
                  root.draftProjectId = value
                  clockify.rememberProject(value)
                }
              }

              Button {
                text: clockify.timing ? "Stop" : "Start"
                iconText: clockify.timing ? "󰓛" : "󰐊"
                bordered: true
                active: clockify.timing
                enabled: !clockify.actionBusy
                foreground: clockify.timing ? root.urgent : root.foreground
                fontFamily: root.fontFamily
                Layout.alignment: Qt.AlignVCenter
                onClicked: root.toggleTimer()
              }
            }
          }

          PanelSeparator {
            visible: clockify.configured
            foreground: root.foreground
          }

          // -------------------------------------------------------- totals

          RowLayout {
            visible: clockify.configured
            width: parent.width
            spacing: Style.space(12)

            Total {
              label: "TODAY"
              value: Model.formatDuration(root.todaySeconds)
              Layout.fillWidth: true
            }

            Total {
              label: clockify.weekStart === "sunday" ? "THIS WEEK (SUN)" : "THIS WEEK"
              value: Model.formatDuration(root.weekSeconds)
              Layout.fillWidth: true
            }
          }

          PanelSeparator {
            visible: clockify.configured
            foreground: root.foreground
          }

          // -------------------------------------------------------- footer

          RowLayout {
            visible: clockify.configured
            width: parent.width
            spacing: Style.space(8)

            Text {
              Layout.fillWidth: true
              text: "󰀄  " + clockify.userName + (clockify.workspaceName === "" ? "" : "  ·  " + clockify.workspaceName)
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              elide: Text.ElideRight
            }

            PanelActionButton {
              iconText: "󰑐"
              tooltipText: "Refresh"
              foreground: root.foreground
              fontFamily: root.fontFamily
              enabled: !clockify.refreshing
              Layout.alignment: Qt.AlignVCenter
              onClicked: clockify.refresh(true)
            }

            PanelActionButton {
              iconText: "󰌆"
              tooltipText: "Forget the stored API key"
              foreground: root.foreground
              fontFamily: root.fontFamily
              enabled: !clockify.actionBusy
              Layout.alignment: Qt.AlignVCenter
              onClicked: clockify.clearKey()
            }
          }
        }
      }
    }
  }

  component Total: Column {
    id: total
    property string label: ""
    property string value: ""

    spacing: Style.space(2)

    Text {
      text: total.label
      color: root.foreground
      opacity: 0.6
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      font.letterSpacing: 1
    }

    Text {
      text: total.value
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.title
    }
  }
}
