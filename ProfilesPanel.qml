import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui as Ui

Ui.Panel {
  id: root
  property var navigationOwner: null
  moduleName: "pinta365.ovpn"
  ipcTarget: "ovpn"
  manageIpc: false
  property var providerService: null
  property var profiles: []
  property string error: ""
  property string connectUuid: ""
  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string helper: Qt.resolvedUrl("helper/openvpn_profiles.py").toString().replace(/^file:\/\//, "")
  readonly property int activeCount: profiles.filter(function(p) { return p.active }).length
  readonly property bool busy: action.running
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function attachService() {
    var shell = bar ? bar.shell : null
    var service = shell && shell.serviceFor ? shell.serviceFor("pinta365.ovpn") : null
    if (service) { providerService = service; service.settings = root.settings }
  }
  onSettingsChanged: attachService()
  onBarChanged: attachService()
  Timer { interval: 250; repeat: true; running: root.providerService === null; triggeredOnStart: true; onTriggered: root.attachService() }
  function refresh() {
    if (inventory.running) return
    inventory.command = ["/usr/bin/python3", helper, "list"]
    inventory.running = true
  }
  function run(args, secret) {
    if (busy) return
    error = ""
    action.secret = secret || "{}"
    action.command = ["/usr/bin/python3", helper].concat(args)
    action.running = true
  }
  function toggleProfile(profile) {
    if (profile.active) run(["disconnect", profile.uuid])
    else { connectUuid = profile.uuid; username.text = ""; password.text = "" }
  }
  onOpenedChanged: {
    if (opened) refresh()
    else { connectUuid = ""; username.text = ""; password.text = "" }
  }
  IpcHandler {
    target: "ovpn"
    function open(): void { root.open() }
    function close(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): void { root.refresh() }
    function status(): string { return JSON.stringify({ backend: "OpenVPN profiles", active: root.activeCount, profiles: root.profiles.length, error: root.error }) }
  }
  Ui.BarIconButton {
    id: button
    bar: root.bar
    slotSize: Style.bar.statusSlot
    tooltipText: "OpenVPN · " + root.activeCount + " connected"
    iconComponent: Component {
      ShieldIcon { color: root.activeCount > 0 ? root.foreground : root.dim; filled: root.activeCount > 0 }
    }
    onPressed: function(code) { if (code === Qt.LeftButton) root.toggle() }
  }
  Ui.KeyboardPanel {
    id: popup
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keys
    contentWidth: popup.fittedContentWidth(Style.space(430))
    contentHeight: popup.fittedContentHeight(content.implicitHeight, Style.space(520))
    Ui.PanelKeyCatcher {
      id: keys
      anchors.fill: parent
      blocked: password.activeFocus || username.activeFocus
      onCloseRequested: root.close()
      onTabRequested: function(direction) { (root.navigationOwner || root).switchPanel(direction) }
      onTextKey: function(t) { if (t.toLowerCase() === "r") root.refresh() }
      Flickable {
        anchors.fill: parent
        contentWidth: width
        contentHeight: content.implicitHeight
        clip: true
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
        Column {
          id: content
          width: parent.width
          spacing: Style.space(12)
          Text { text: "OpenVPN"; color: root.foreground; font.family: Style.font.family; font.pixelSize: Style.font.title; font.bold: true }
          Text { width: parent.width; text: "Your NetworkManager OpenVPN profiles"; color: root.dim; wrapMode: Text.Wrap; font.pixelSize: Style.font.body }
          Text { width: parent.width; visible: root.error !== ""; text: root.error; textFormat: Text.PlainText; color: Color.urgent; wrapMode: Text.Wrap }
          Text { width: parent.width; visible: root.profiles.length === 0; text: "No OpenVPN profiles yet. Import your .ovpn file to connect."; wrapMode: Text.Wrap; color: root.dim; font.pixelSize: Style.font.body }
          Repeater {
            model: root.profiles
            RowLayout {
              required property var modelData
              width: content.width
              ColumnLayout {
                Layout.fillWidth: true
                Text { Layout.fillWidth: true; text: modelData.name; textFormat: Text.PlainText; color: root.foreground; elide: Text.ElideRight; font.pixelSize: Style.font.body }
                Text { text: modelData.active ? "Connected" : modelData.connecting ? "Connecting…" : "Disconnected"; color: root.dim; font.pixelSize: Style.font.caption }
              }
              Button { text: modelData.active ? "Disconnect" : "Connect"; enabled: !root.busy && !modelData.connecting; onClicked: root.toggleProfile(modelData) }
            }
          }
          Column {
            width: parent.width
            visible: root.connectUuid !== ""
            spacing: Style.space(8)
            Text { width: parent.width; text: "Password (leave empty for certificates or saved credentials)"; wrapMode: Text.Wrap; color: root.dim }
            TextField { id: username; width: parent.width; placeholderText: "Username (optional; keeps the saved username)" }
            TextField { id: password; width: parent.width; echoMode: TextInput.Password; placeholderText: "VPN password" }
            Row {
              spacing: Style.space(8)
              Button { text: "Connect"; enabled: !root.busy; onClicked: { root.run(["connect", root.connectUuid], JSON.stringify({username: username.text, password: password.text})); username.text = ""; password.text = ""; root.connectUuid = "" } }
              Button { text: "Cancel"; onClicked: { username.text = ""; password.text = ""; root.connectUuid = "" } }
            }
          }
          Row {
            spacing: Style.space(8)
            Button { text: "Import .ovpn"; enabled: !root.busy; onClicked: root.run(["import"]) }
            Button { text: "Refresh"; onClicked: root.refresh() }
          }
          Text { visible: root.busy; text: "Working…"; color: root.dim }
        }
      }
    }
  }
  Process {
    id: inventory
    stdout: StdioCollector { id: inventoryOutput; waitForEnd: true }
    onExited: {
      try {
        var data = JSON.parse(inventoryOutput.text)
        if (data.ok) { root.profiles = data.profiles }
        else root.error = data.error
      } catch (e) { root.error = "Could not read NetworkManager profiles" }
    }
  }
  Process {
    id: action
    property string secret: "{}"
    stdinEnabled: true
    stdout: StdioCollector { id: actionOutput; waitForEnd: true }
    onStarted: { write(secret + "\n"); secret = "{}" }
    onExited: {
      try { var data = JSON.parse(actionOutput.text); if (!data.ok) root.error = data.error }
      catch (e) { root.error = "The OpenVPN action failed" }
      root.refresh()
    }
  }
  Timer { interval: 5000; running: true; repeat: true; triggeredOnStart: true; onTriggered: root.refresh() }
}
