import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Ui
import qs.Commons
import "AssignmentModel.js" as AssignmentModel

// Login Workspace Assignments -- pick installed apps and assign each one a
// Hyprland workspace (or the scratchpad) to launch on login.
//
// This plugin never edits ~/.config/hypr/hyprland.lua, autostart.lua, or
// bindings.lua. It only ever writes its own state file and its own
// generated Lua file (~/.config/hypr/hypr-login-workspaces.lua); wiring that
// file into Hyprland is a one-time manual step documented in the README.
//
// All security-sensitive work -- file IO, .desktop scanning, argv
// validation, window detection, Lua generation, hyprctl reload/rollback --
// lives in bin/hlw-helper.py, invoked here only as bounded argv-array
// subprocesses. This file never runs a shell string and never trusts a
// value it did not just get back from that helper.
Item {
  id: root

  // ---- plugin lifecycle -----------------------------------------------
  property var shell: null
  property bool closingFromHost: false

  function open(payloadJson) {
    closingFromHost = false
    window.visible = true
    root.refreshApps()
    root.refreshState()
    Qt.callLater(function() { if (keyCatcher) keyCatcher.forceActiveFocus() })
  }

  function close() {
    closingFromHost = true
    window.visible = false
    closingFromHost = false
  }

  function requestClose() {
    if (shell && typeof shell.hide === "function") shell.hide("io.github.lovetr923.hypr-login-workspaces")
    else window.visible = false
  }

  // ---- theme ------------------------------------------------------------
  readonly property color foreground: Color.foreground
  readonly property color background: Color.background
  readonly property color accent: Color.accent
  readonly property color urgent: Color.urgent
  readonly property string fontFamily: Style.font.family

  // ---- plugin-relative paths, resolved without any host-injected property
  // (manifest.__sourceDir is deliberately stripped before a third-party
  // manifest reaches this file) ------------------------------------------
  readonly property string pluginDir: {
    var manifestPath = AssignmentModel.urlToLocalPath(Qt.resolvedUrl("manifest.json"))
    var idx = manifestPath.lastIndexOf("/")
    return idx >= 0 ? manifestPath.slice(0, idx) : manifestPath
  }
  readonly property string helperPath: pluginDir + "/bin/hlw-helper.py"

  function helperCommand(args) {
    return ["/usr/bin/python3", "-I", "-S", root.helperPath].concat(args)
  }

  // ---- data state ---------------------------------------------------------
  property var assignments: []
  property var appChoices: []
  property bool appsLoading: false
  property bool stateLoading: false

  property string saveStatus: ""   // "" | "saving" | "saved" | "error"
  property string saveError: ""

  property string selectedAppValue: ""
  property int selectedWorkspace: 1
  property bool sendToScratchpad: false

  property bool detecting: false
  property string detectError: ""
  property var detectResult: null   // { class, title }

  property string pendingRemoveId: ""

  readonly property var selectedAppEntry: {
    for (var i = 0; i < appChoices.length; i++)
      if (appChoices[i].value === selectedAppValue) return appChoices[i]
    return null
  }
  readonly property bool selectedRequiresDetect: !!(selectedAppEntry && selectedAppEntry.requiresDetect)

  function canAdd() {
    if (!selectedAppEntry) return false
    if (assignments.length >= 32) return false
    if (selectedRequiresDetect) return detectResult !== null
    return true
  }

  function destinationLabel(ws) { return AssignmentModel.destinationLabel(ws) }

  // ---- process runners ------------------------------------------------
  // Every subcommand prints exactly one bounded JSON document to stdout.
  // Accumulate with a byte cap here too (defense in depth -- the actual
  // bound is enforced inside the helper), never StdioCollector.
  function runHelper(args, onDone) {
    helperProc.pendingCallback = onDone
    helperProc.buffer = ""
    helperProc.overflowed = false
    helperProc.command = root.helperCommand(args)
    helperProc.running = true
  }

  Process {
    id: helperProc
    property var pendingCallback: null
    property string buffer: ""
    property bool overflowed: false
    readonly property int maxBytes: 1048576

    running: false
    stdout: SplitParser {
      splitMarker: ""
      onRead: function(data) {
        if (helperProc.overflowed) return
        helperProc.buffer += data
        if (helperProc.buffer.length > helperProc.maxBytes) {
          helperProc.overflowed = true
          helperProc.signal(15)
        }
      }
    }
    onExited: function(exitCode) {
      var cb = pendingCallback
      pendingCallback = null
      if (!cb) return
      if (overflowed) { cb(null, "output too large"); return }
      if (exitCode !== 0) { cb(null, "helper exited with code " + exitCode); return }
      try {
        cb(JSON.parse(buffer), "")
      } catch (e) {
        cb(null, "could not parse helper output")
      }
    }
  }

  Timer {
    id: helperKillTimer
    interval: 15000
    repeat: false
    onTriggered: if (helperProc.running) helperProc.signal(9)
  }
  Connections {
    target: helperProc
    function onRunningChanged() {
      if (helperProc.running) helperKillTimer.restart()
      else helperKillTimer.stop()
    }
  }

  // Detect-window can legitimately take up to ~11s inside the helper
  // (3s launch timeout + 8s poll deadline); give it real headroom before
  // the QML-side kill timer above would otherwise cut it off at 15s.
  function runDetect(args, onDone) {
    detectHelperProc.pendingCallback = onDone
    detectHelperProc.buffer = ""
    detectHelperProc.overflowed = false
    detectHelperProc.command = root.helperCommand(args)
    detectHelperProc.running = true
  }

  Process {
    id: detectHelperProc
    property var pendingCallback: null
    property string buffer: ""
    property bool overflowed: false
    readonly property int maxBytes: 65536

    running: false
    stdout: SplitParser {
      splitMarker: ""
      onRead: function(data) {
        if (detectHelperProc.overflowed) return
        detectHelperProc.buffer += data
        if (detectHelperProc.buffer.length > detectHelperProc.maxBytes) {
          detectHelperProc.overflowed = true
          detectHelperProc.signal(15)
        }
      }
    }
    onExited: function(exitCode) {
      var cb = pendingCallback
      pendingCallback = null
      if (!cb) return
      if (overflowed) { cb(null, "output too large"); return }
      if (exitCode !== 0) { cb(null, "helper exited with code " + exitCode); return }
      try {
        cb(JSON.parse(buffer), "")
      } catch (e) {
        cb(null, "could not parse helper output")
      }
    }
  }
  Timer {
    id: detectKillTimer
    interval: 20000
    repeat: false
    onTriggered: if (detectHelperProc.running) detectHelperProc.signal(9)
  }
  Connections {
    target: detectHelperProc
    function onRunningChanged() {
      if (detectHelperProc.running) detectKillTimer.restart()
      else detectKillTimer.stop()
    }
  }

  // ---- actions ------------------------------------------------------------
  function refreshApps() {
    appsLoading = true
    runHelper(["list-apps"], function(result, err) {
      appsLoading = false
      if (!result || !result.ok) return
      var choices = []
      for (var i = 0; i < result.apps.length; i++) {
        var a = result.apps[i]
        choices.push({
          value: a.desktopId,
          label: a.name,
          argv: a.argv,
          klass: a["class"],
          requiresDetect: a.requiresDetect
        })
      }
      appChoices = choices
    })
  }

  function refreshState() {
    stateLoading = true
    runHelper(["read-state"], function(result, err) {
      stateLoading = false
      if (!result || !result.ok) return
      assignments = result.assignments || []
    })
  }

  function startDetect() {
    if (!selectedAppEntry) return
    detecting = true
    detectError = ""
    detectResult = null
    runDetect(["detect-window"].concat(selectedAppEntry.argv), function(result, err) {
      detecting = false
      if (!result) { detectError = "Detection failed to run."; return }
      if (!result.ok) {
        if (result.error === "timeout") detectError = "Timed out waiting for the app's window."
        else if (result.error === "ambiguous") detectError = "Multiple new windows appeared; try again alone."
        else detectError = "Could not detect the window."
        return
      }
      detectResult = { class: String(result["class"] || ""), title: String(result.title || "") }
    })
  }

  function addAssignment() {
    if (!canAdd()) return
    var entry = selectedAppEntry
    var match = {}
    var matchStrategy = "exact"

    if (selectedRequiresDetect && detectResult) {
      if (entry.value === "builtin:herdr") {
        match = { title: detectResult.title }
        matchStrategy = "herdr-title-prefix"
      } else {
        if (detectResult.class) match.class = detectResult.class
        if (detectResult.title) match.title = detectResult.title
      }
    } else if (entry.klass) {
      match = { class: entry.klass }
    }

    if (Object.keys(match).length === 0) return

    var workspace = sendToScratchpad
      ? { type: "scratchpad" }
      : { type: "number", value: selectedWorkspace }

    var next = assignments.slice()
    next.push({
      id: AssignmentModel.newId(),
      label: entry.label,
      source: entry.value.indexOf("builtin:") === 0 ? "builtin" : "desktop",
      desktopId: entry.value,
      argv: entry.argv,
      match: match,
      matchStrategy: matchStrategy,
      workspace: workspace,
      detected: selectedRequiresDetect
    })
    assignments = next

    selectedAppValue = ""
    // SearchableDropdown writes its own `value` property imperatively on
    // selection, which silently breaks the one-way binding set on it from
    // here -- so the reset above alone would not clear what the dropdown
    // displays. Reset it directly by id as well.
    appPicker.value = ""
    detectResult = null
    detectError = ""
    sendToScratchpad = false
    selectedWorkspace = 1
  }

  function removeAssignment(id) {
    var next = []
    for (var i = 0; i < assignments.length; i++)
      if (assignments[i].id !== id) next.push(assignments[i])
    assignments = next
  }

  function saveAndApply() {
    saveStatus = "saving"
    saveError = ""
    var payload = JSON.stringify({ schemaVersion: 1, assignments: assignments })
    runHelper(["save", payload], function(result, err) {
      if (!result) {
        saveStatus = "error"
        saveError = "Save failed to run."
        return
      }
      if (!result.ok) {
        saveStatus = "error"
        if (result.error === "hyprland-config-error")
          saveError = "Hyprland rejected the generated config; rolled back to the previous version."
        else
          saveError = "Could not save: " + String(result.error || "unknown error")
        return
      }
      saveStatus = "saved"
      root.refreshState()
    })
  }

  // ---- window -------------------------------------------------------------
  FloatingWindow {
    id: window
    title: "Login Workspace Assignments"
    color: root.background
    implicitWidth: 640
    implicitHeight: 680
    minimumSize: Qt.size(520, 480)

    onVisibleChanged: {
      if (!visible && !root.closingFromHost && root.shell && typeof root.shell.hide === "function")
        root.shell.hide("io.github.lovetr923.hypr-login-workspaces")
    }

    FocusScope {
      anchors.fill: parent
      focus: true

      PanelKeyCatcher {
        id: keyCatcher
        anchors.fill: parent
        blocked: appPicker.popupOpen
        onCloseRequested: root.requestClose()

        ScrollView {
          anchors.fill: parent
          anchors.margins: Style.space(18)
          clip: true
          ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

          Column {
            width: parent.width
            spacing: Style.space(18)

            // ---- Header ----------------------------------------------
            Column {
              width: parent.width
              spacing: Style.space(4)

              Text {
                textFormat: Text.PlainText
                text: "Login Workspace Assignments"
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.title
                font.bold: true
              }
              Text {
                textFormat: Text.PlainText
                text: "Launch an app at login and pin it to a workspace or the scratchpad."
                color: Qt.darker(root.foreground, 1.4)
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                width: parent.width
                wrapMode: Text.WordWrap
              }
            }

            PanelSeparator { foreground: root.foreground }

            // ---- Add an app -------------------------------------------
            Column {
              width: parent.width
              spacing: Style.space(10)

              PanelSectionHeader { text: "ADD AN APP"; foreground: root.foreground; fontFamily: root.fontFamily }

              SearchableDropdown {
                id: appPicker
                width: parent.width
                label: "App"
                placeholderText: root.appsLoading ? "Loading installed apps..." : "Search installed apps..."
                options: root.appChoices.map(function(a) { return { value: a.value, label: a.label } })
                value: root.selectedAppValue
                onChanged: function(v) {
                  root.selectedAppValue = v
                  root.detectResult = null
                  root.detectError = ""
                }
              }

              Row {
                spacing: Style.space(18)
                visible: !root.sendToScratchpad
                NumberField {
                  label: "Workspace"
                  from: 1
                  to: 10
                  value: root.selectedWorkspace
                  onModified: function(v) { root.selectedWorkspace = v }
                }
              }

              Toggle {
                width: parent.width
                label: "Send to the scratchpad instead"
                description: "Uses Hyprland's existing special:scratchpad, toggled with Super+S / Super+grave."
                checked: root.sendToScratchpad
                onClicked: root.sendToScratchpad = !root.sendToScratchpad
              }

              // Detect-by-launch: only for apps with no reliable window class
              // in their .desktop file (or built-ins that need a live title,
              // like Herdr).
              Column {
                width: parent.width
                spacing: Style.space(8)
                visible: root.selectedRequiresDetect

                Text {
                  textFormat: Text.PlainText
                  width: parent.width
                  wrapMode: Text.WordWrap
                  text: "This app has no reliable window class on its own. Launch it once so its real window can be identified."
                  color: Qt.darker(root.foreground, 1.4)
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                }

                Button {
                  text: root.detecting ? "Launching... waiting for its window" : "Detect window by launching the app"
                  enabled: !root.detecting && root.selectedAppEntry !== null
                  bordered: true
                  onClicked: root.startDetect()
                }

                Text {
                  textFormat: Text.PlainText
                  visible: root.detectError !== ""
                  text: root.detectError
                  color: root.urgent
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                }

                BorderSurface {
                  visible: root.detectResult !== null
                  width: parent.width
                  implicitHeight: detectCol.implicitHeight + Style.spacing.rowPaddingX * 2
                  radius: Style.cornerRadius
                  color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.04)
                  borderSpec: Border.flat(Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.10), 1)

                  Column {
                    id: detectCol
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    anchors.leftMargin: Style.space(14)
                    anchors.rightMargin: Style.space(14)
                    spacing: Style.space(6)

                    Text {
                      textFormat: Text.PlainText
                      width: parent.width
                      wrapMode: Text.WordWrap
                      text: root.detectResult ? ("Detected class: " + (root.detectResult.class || "(none)")) : ""
                      color: root.foreground
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.bodySmall
                    }
                    Text {
                      textFormat: Text.PlainText
                      width: parent.width
                      wrapMode: Text.WordWrap
                      text: root.detectResult ? ("Detected title: " + (root.detectResult.title || "(none)")) : ""
                      color: root.foreground
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.bodySmall
                    }
                  }
                }
              }

              Button {
                text: "Add assignment"
                enabled: root.canAdd()
                bordered: true
                onClicked: root.addAssignment()
              }
            }

            PanelSeparator { foreground: root.foreground }

            // ---- Current assignments -----------------------------------
            Column {
              width: parent.width
              spacing: Style.space(10)

              PanelSectionHeader { text: "CURRENT ASSIGNMENTS"; foreground: root.foreground; fontFamily: root.fontFamily }

              Text {
                textFormat: Text.PlainText
                visible: root.assignments.length === 0
                text: "No assignments yet."
                color: Qt.darker(root.foreground, 1.5)
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
              }

              Repeater {
                id: assignmentsRepeater
                model: root.assignments
                delegate: AssignmentRow {
                  required property var modelData
                  width: assignmentsRepeater.parent ? assignmentsRepeater.parent.width : 0
                  rowLabel: modelData.label
                  destination: root.destinationLabel(modelData.workspace)
                  foreground: root.foreground
                  urgentColor: root.urgent
                  fontFamily: root.fontFamily
                  onRemoveRequested: {
                    root.pendingRemoveId = modelData.id
                    confirmRemove.message = "Remove the assignment for \"" + modelData.label + "\"?"
                    confirmRemove.opened = true
                  }
                }
              }
            }

            PanelSeparator { foreground: root.foreground }

            // ---- Save ---------------------------------------------------
            Row {
              spacing: Style.space(14)

              Button {
                text: root.saveStatus === "saving" ? "Saving..." : "Save & Apply"
                enabled: root.saveStatus !== "saving"
                bordered: true
                onClicked: root.saveAndApply()
              }

              Text {
                textFormat: Text.PlainText
                visible: root.saveStatus === "saved"
                text: "Saved and applied."
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                anchors.verticalCenter: parent.verticalCenter
              }
              Text {
                textFormat: Text.PlainText
                visible: root.saveStatus === "error"
                width: Style.space(360)
                wrapMode: Text.WordWrap
                text: root.saveError
                color: root.urgent
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                anchors.verticalCenter: parent.verticalCenter
              }
            }
          }
        }
      }
    }
  }

  ConfirmDialog {
    id: confirmRemove
    confirmText: "Remove"
    onConfirmed: {
      root.removeAssignment(root.pendingRemoveId)
      root.pendingRemoveId = ""
      confirmRemove.opened = false
    }
    onCanceled: {
      root.pendingRemoveId = ""
      confirmRemove.opened = false
    }
  }

  // One row in the assignment list: label + destination, trailing remove
  // button shown on hover -- same shape as bluetooth Panel.qml's DeviceRow.
  component AssignmentRow: CursorSurface {
    id: row
    property string rowLabel: ""
    property string destination: ""
    // `foreground` is inherited from CursorSurface/BorderSurface already --
    // callers bind it directly (see the delegate below); redeclaring it
    // here would be a duplicate-property error, not an override.
    property color urgentColor: Color.urgent
    property string fontFamily: Style.font.family
    signal removeRequested()

    readonly property bool showRemove: rowMouse.containsMouse
    hasCursor: rowMouse.containsMouse
    implicitHeight: rowContent.implicitHeight + Style.spacing.rowPaddingX

    MouseArea {
      id: rowMouse
      anchors.fill: parent
      hoverEnabled: true
    }

    Item {
      id: rowContent
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      implicitHeight: Math.max(labelCol.implicitHeight, removeBtn.implicitHeight)

      Column {
        id: labelCol
        anchors.left: parent.left
        anchors.right: removeBtn.left
        anchors.rightMargin: Style.space(8)
        anchors.verticalCenter: parent.verticalCenter
        spacing: Style.space(1)

        Text {
          textFormat: Text.PlainText
          text: row.rowLabel
          color: row.foreground
          font.family: row.fontFamily
          font.pixelSize: Style.font.body
          elide: Text.ElideRight
          width: parent.width
        }
        Text {
          textFormat: Text.PlainText
          text: row.destination
          color: Qt.darker(row.foreground, 1.5)
          font.family: row.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
          width: parent.width
        }
      }

      PanelActionButton {
        id: removeBtn
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        visible: row.showRemove
        iconText: "\u{f0159}"
        tooltipText: "Remove"
        foreground: row.foreground
        hoverColor: row.urgentColor
        fontFamily: row.fontFamily
        onClicked: row.removeRequested()
      }
    }
  }
}
