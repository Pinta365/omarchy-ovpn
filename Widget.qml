import QtQuick

// Keep OVPN.com as the default; custom profiles use an independent panel.
Item {
  id: root
  property var bar: null
  property string moduleName: "pinta365.ovpn"
  property var settings: null
  readonly property bool profileMode: settings !== null && settings.backend === "OpenVPN profiles"
  readonly property bool opened: page.item ? page.item.opened : false
  function open() { if (page.item) page.item.open() }
  function close() { if (page.item) page.item.close() }
  function toggle() { if (page.item) page.item.toggle() }
  function closeForPopoutSwitch() { if (page.item) page.item.closeForPopoutSwitch() }
  function switchPanel(direction) { return bar ? bar.switchPanelFrom(root, direction) : false }
  implicitWidth: page.item ? page.item.implicitWidth : 0
  implicitHeight: page.item ? page.item.implicitHeight : 0
  Loader {
    id: page
    active: root.bar !== null && root.settings !== null
    source: root.profileMode ? "ProfilesPanel.qml" : "Panel.qml"
  }
  Binding { target: page.item; property: "navigationOwner"; value: root; when: page.item !== null }
  Binding { target: page.item; property: "bar"; value: root.bar; when: page.item !== null }
  Binding { target: page.item; property: "settings"; value: root.settings; when: page.item !== null }
}
