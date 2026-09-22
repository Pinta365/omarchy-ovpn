import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

Panel {
  id: root
  moduleName: "pinta365.ovpn"
  ipcTarget: "ovpn"
  // Own IpcHandler below carries the extra methods.
  manageIpc: false

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color accent: Color.accent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  readonly property bool showLocationText: setting("showLocationText", true) === true
  readonly property bool barTextVisible: showLocationText && vpn.connected && vpn.currentLocation !== null

  property string query: ""
  readonly property var shownLocations: Model.sorted(vpn.locations, vpn.status.favorites, query)
  readonly property bool showSignIn: vpn.needsCredentials || (!vpn.signedIn && vpn.everLoaded)

  property real now: Date.now()
  Timer {
    interval: 1000
    running: root.opened && vpn.connected
    repeat: true
    triggeredOnStart: true
    onTriggered: root.now = Date.now()
  }

  property bool cursorActive: false
  property int cursor: 0

  readonly property var stops: {
    var out = [{ kind: "main" }, { kind: "proto", value: "udp" }, { kind: "proto", value: "tcp" },
               { kind: "multihop" }]
    if (vpn.multihop) out.push({ kind: "entry" })
    if (!pickingEntry) out.push({ kind: "location", slug: Model.FASTEST })
    for (var i = 0; i < shownLocations.length; i++) out.push({ kind: "location", slug: shownLocations[i].slug })
    if (vpn.signedIn) out.push({ kind: "signout" })
    return out
  }

  function stopIs(kind, value) {
    if (!cursorActive || cursor < 0 || cursor >= stops.length) return false
    var s = stops[cursor]
    if (s.kind !== kind) return false
    return !s.value && !s.slug ? true : (s.value === value || s.slug === value)
  }

  function takeCursor(kind, value) {
    for (var i = 0; i < stops.length; i++) {
      var s = stops[i]
      if (s.kind === kind && ((!s.value && !s.slug) || s.value === value || s.slug === value)) {
        cursorActive = true
        cursor = i
        return
      }
    }
  }

  function moveCursor(dx, dy) {
    var step = dy !== 0 ? dy : dx
    // Wrap: the footer is one Up press from the top rather than a whole
    // location list away.
    cursor = (cursor + step + stops.length) % stops.length
    Qt.callLater(scrollToCursor)
  }

  function activateCursor() {
    var s = stops[cursor]
    if (!s) return
    if (s.kind === "main") vpn.openvpnMissing ? vpn.installOpenvpn() : vpn.quickToggle()
    else if (s.kind === "proto") vpn.setProtocol(s.value)
    else if (s.kind === "multihop") toggleMultihop()
    else if (s.kind === "entry") pickingEntry = !pickingEntry
    else if (s.kind === "location") chooseLocation(s.slug)
    else if (s.kind === "signout") signOutRequested()
  }

  // Signing out drops an active tunnel, so that case asks first.
  function signOutRequested() {
    if (!confirmSignOut && vpn.state !== Model.STATE_OFF) {
      confirmSignOut = true
      return
    }
    confirmSignOut = false
    vpn.signOut()
  }

  property bool pickingEntry: false
  property bool confirmSignOut: false

  function toggleMultihop() {
    if (vpn.multihop) {
      vpn.setMultihopEntry("")
      pickingEntry = false
      return
    }
    var nearest = Model.fastest(vpn.locations, vpn.pings, "")
    vpn.setMultihopEntry(nearest ? nearest.slug : (vpn.status.last || ""))
    pickingEntry = true
  }

  function chooseLocation(slug) {
    if (pickingEntry) {
      if (slug !== Model.FASTEST) vpn.setMultihopEntry(slug)
      pickingEntry = false
      query = ""
      keyCatcher.forceActiveFocus()
      return
    }
    vpn.connectTo(slug)
  }

  function favoriteCursor() {
    var s = stops[cursor]
    if (s && s.kind === "location" && s.slug !== Model.FASTEST) vpn.toggleFavorite(s.slug)
  }

  function scrollToCursor() {
    var s = stops[cursor]
    if (s && s.kind === "signout") {
      panelFlick.contentY = Math.max(0, panelFlick.contentHeight - panelFlick.height)
      return
    }
    if (!s || s.kind !== "location") return
    var item = s.slug === Model.FASTEST ? fastestRow : null
    if (!item) {
      for (var i = 0; i < locationRepeater.count; i++) {
        var candidate = locationRepeater.itemAt(i)
        if (candidate && candidate.slug === s.slug) { item = candidate; break }
      }
    }
    if (!item) return
    var y = item.mapToItem(column, 0, 0).y
    if (y < panelFlick.contentY) panelFlick.contentY = y
    else if (y + item.height > panelFlick.contentY + panelFlick.height)
      panelFlick.contentY = y + item.height - panelFlick.height
  }

  onQueryChanged: if (cursor >= stops.length) cursor = stops.length - 1

  readonly property bool editing: searchField.activeFocus || userField.activeFocus || passwordField.activeFocus

  // The plugin's single Service, shared by every bar instance.
  property var service: null
  readonly property var vpn: service !== null ? service : placeholder

  function attachService() {
    var shell = bar ? bar.shell : null
    if (service !== null || !shell || typeof shell.serviceFor !== "function") return
    var found = shell.serviceFor("pinta365.ovpn")
    if (!found) return
    service = found
    found.settings = root.settings
    if (root.opened) found.panelOpened()
  }

  // serviceFor() is a plain call and the service may load later, so poll.
  Timer {
    interval: 250
    repeat: true
    running: root.service === null
    triggeredOnStart: true
    onTriggered: root.attachService()
  }

  onBarChanged: attachService()
  onSettingsChanged: if (service !== null) service.settings = root.settings

  Component.onDestruction: if (service !== null && opened) service.panelClosed()

  // Stands in until the service is attached, so bindings never see null.
  QtObject {
    id: placeholder
    property var status: Model.defaultStatus()
    property var locations: []
    property var pings: ({})
    property string state: Model.STATE_OFF
    property bool connected: false
    property bool busy: true
    property bool signedIn: false
    property bool everLoaded: false
    property bool needsCredentials: false
    property bool authFailed: false
    property string protocol: "udp"
    property string publicIp: ""
    property string lastError: ""
    property var currentLocation: null
    property var lastLocation: null
    property var fastestLocation: null
    property bool multihop: false
    property string multihopEntry: ""
    property var entryLocation: null
    property var viaLocation: null
    property bool openvpnMissing: false
    property bool installing: false
    function isFavorite(slug) { return false }
    function installOpenvpn() {}
    function setMultihopEntry(slug) {}
    function connectTo(slug) {}
    function disconnect() {}
    function quickToggle() {}
    function toggleFavorite(slug) {}
    function setProtocol(proto) {}
    function signIn(u, p, r) {}
    function signOut() {}
    function cancelSignIn() {}
    function loadServers(refresh) {}
    function ping() {}
    function lookupIp() {}
  }

  Connections {
    target: root.service
    ignoreUnknownSignals: true
    function onNeedsCredentialsChanged() {
      if (root.service.needsCredentials && root.opened) Qt.callLater(root.focusSignIn)
    }
  }

  function focusSignIn() {
    if (!root.showSignIn) return
    if (userField.text === "") userField.text = vpn.status.username || ""
    if (userField.visible && userField.text === "") userField.forceActiveFocus()
    else passwordField.forceActiveFocus()
  }

  onOpenedChanged: {
    if (service !== null) {
      if (opened) service.panelOpened()
      else service.panelClosed()
    }
    if (!opened) {
      pickingEntry = false
      confirmSignOut = false
      query = ""
      cursorActive = false
      cursor = 0
    }
  }

  IpcHandler {
    target: "ovpn"
    function open(): void { root.open() }
    function close(): void { root.close() }
    function toggle(): void { root.toggle() }
    function connect(): void { vpn.connectTo(vpn.status.last || Model.FASTEST) }
    function connectTo(slug: string): void { vpn.connectTo(slug) }
    function disconnect(): void { vpn.disconnect() }
    function quickToggle(): void { vpn.quickToggle() }
    function multihop(entry: string): void { vpn.setMultihopEntry(entry === "off" ? "" : entry) }
    function status(): string { return vpn.state }
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  TextMetrics {
    id: cityMetrics
    font.family: root.fontFamily
    font.pixelSize: Style.font.body
    text: Model.routeName(vpn.currentLocation, vpn.viaLocation)
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    slotSize: Style.bar.iconSlot + (root.barTextVisible ? cityMetrics.width + Style.space(5) : 0)
    iconComponent: Component {
      Item {
        RowLayout {
          anchors.centerIn: parent
          spacing: Style.space(4)

          ShieldIcon {
            iconSize: Style.space(13)
            filled: vpn.connected
            pulsing: Model.isBusy(vpn.state)
            color: vpn.connected || Model.isBusy(vpn.state) ? root.foreground : root.dim
          }

          Text {
            visible: root.barTextVisible
            text: Model.routeName(vpn.currentLocation, vpn.viaLocation)
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
          }
        }
      }
    }
    onPressed: function (buttonCode) {
      if (buttonCode === Qt.MiddleButton && !vpn.openvpnMissing) vpn.quickToggle()
      else root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(340))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(640))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: root.editing
      onMoveRequested: function (dx, dy) {
        if (!root.cursorActive) { root.cursorActive = true; return }
        root.moveCursor(dx, dy)
      }
      onActivateRequested: {
        if (!root.cursorActive) { root.cursor = 0; root.cursorActive = true }
        root.activateCursor()
      }
      onCloseRequested: {
        if (root.pickingEntry) root.pickingEntry = false
        else root.close()
      }
      onTabRequested: function (direction) { root.switchPanel(direction) }
      onTextKey: function (t) {
        if (t === "/") { searchField.forceActiveFocus(); return }
        var key = t.toLowerCase()
        if (key === "f") root.favoriteCursor()
        else if (key === "d") vpn.disconnect()
        else if (key === "c") vpn.connectTo(vpn.status.last || Model.FASTEST)
        else if (key === "r") { vpn.loadServers(true); vpn.ping(); vpn.lookupIp() }
        else if (key === "t") vpn.setProtocol(vpn.protocol === "udp" ? "tcp" : "udp")
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
          // Inset so control borders never sit on the Flickable's clip edge,
          // where fractional scaling rounds them away.
          x: Style.space(2)
          width: panelFlick.width - Style.space(4)
          spacing: Style.space(10)

          PanelHero {
            width: parent.width
            foreground: root.foreground
            fontFamily: root.fontFamily
            title: vpn.openvpnMissing ? "OpenVPN support missing" : Model.stateTitle(vpn.state)
            meta: {
              var loc = vpn.connected || Model.isBusy(vpn.state) ? vpn.currentLocation : null
              var parts = []
              if (loc) parts.push(vpn.viaLocation ? Model.routeName(loc, vpn.viaLocation) : Model.placeName(loc))
              if (vpn.publicIp !== "" && !Model.isBusy(vpn.state)) parts.push(vpn.publicIp)
              return parts.join(" · ")
            }
            detail: vpn.connected && vpn.status.protocol
                    ? (vpn.status.via ? "MULTIHOP" : vpn.status.protocol.toUpperCase()) : ""
            iconComponent: Component {
              ShieldIcon {
                iconSize: Style.font.display * 1.25
                filled: vpn.connected
                pulsing: Model.isBusy(vpn.state)
                color: vpn.connected ? root.accent : root.foreground
              }
            }
          }

          Button {
            id: mainButton
            width: parent.width
            text: {
              if (vpn.openvpnMissing) return vpn.installing ? "Installing…" : "Install OpenVPN support"
              if (vpn.state === Model.STATE_CONNECTING) return "Cancel"
              if (vpn.state === Model.STATE_DISCONNECTING) return "Disconnecting…"
              if (vpn.connected) return "Disconnect"
              var target = vpn.lastLocation && vpn.lastLocation.slug !== vpn.multihopEntry
                           ? vpn.lastLocation : vpn.fastestLocation
              if (!target) return "Connect"
              return "Connect to " + target.city
                     + (vpn.multihop && vpn.entryLocation ? " via " + vpn.entryLocation.city : "")
            }
            selected: !vpn.connected && !Model.isBusy(vpn.state)
            bordered: true
            foreground: root.foreground
            fontFamily: root.fontFamily
            fontSize: Style.font.title
            verticalPadding: Style.space(9)
            hasCursor: root.stopIs("main")
            enabled: vpn.state !== Model.STATE_DISCONNECTING
            onClicked: vpn.openvpnMissing ? vpn.installOpenvpn() : vpn.quickToggle()
            onHovered: function (on) { if (on) root.takeCursor("main") }
          }

          Text {
            width: parent.width
            visible: vpn.openvpnMissing
            text: "OVPN connects through NetworkManager's OpenVPN add-on, networkmanager-openvpn, "
                  + "which Omarchy does not include. Install opens a terminal and asks for your password there."
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          Text {
            width: parent.width
            visible: vpn.connected
            horizontalAlignment: Text.AlignHCenter
            text: {
              var parts = ["Connected " + Model.duration(vpn.status.since, root.now)]
              if (vpn.status.tunnelAddress) parts.push("tunnel " + Model.address(vpn.status.tunnelAddress))
              return parts.join(" · ")
            }
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          Column {
            id: signIn
            width: parent.width
            visible: root.showSignIn
            spacing: Style.space(6)

            PanelSeparator { width: parent.width }

            PanelSectionHeader {
              width: parent.width
              text: vpn.authFailed ? "Wrong username or password"
                    : vpn.signedIn ? "Password for " + vpn.status.username : "Sign in to OVPN"
              foreground: vpn.authFailed ? root.urgent : root.foreground
              fontFamily: root.fontFamily
            }

            Text {
              width: parent.width
              visible: vpn.signedIn && !vpn.authFailed
              text: "Not remembered, so it's asked once per session."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }

            TextField {
              id: userField
              width: parent.width
              // A known username only needs its password; a wrong one needs both.
              visible: !vpn.signedIn || vpn.authFailed
              placeholderText: "Username"
              foreground: root.foreground
              text: vpn.status.username || ""
              onAccepted: passwordField.forceActiveFocus()
              Keys.onEscapePressed: { vpn.cancelSignIn(); keyCatcher.forceActiveFocus() }
            }

            TextField {
              id: passwordField
              width: parent.width
              password: true
              placeholderText: "Password"
              foreground: root.foreground
              onAccepted: root.submitSignIn()
              Keys.onEscapePressed: { vpn.cancelSignIn(); keyCatcher.forceActiveFocus() }
            }

            ToggleRow {
              id: rememberRow
              width: parent.width
              label: "Remember password"
              caption: "Stored encrypted in your login keyring"
              checked: false
              onActivated: checked = !checked
            }

            RowLayout {
              width: parent.width
              spacing: Style.space(6)

              Button {
                Layout.fillWidth: true
                text: vpn.signedIn && !vpn.authFailed ? "Continue" : "Sign in"
                selected: true
                bordered: true
                foreground: root.foreground
                fontFamily: root.fontFamily
                enabled: !vpn.busy
                onClicked: root.submitSignIn()
              }

              Button {
                visible: vpn.needsCredentials
                text: "Cancel"
                bordered: true
                foreground: root.foreground
                fontFamily: root.fontFamily
                onClicked: { vpn.cancelSignIn(); keyCatcher.forceActiveFocus() }
              }
            }
          }

          PanelSeparator { width: parent.width }

          RowLayout {
            width: parent.width
            spacing: Style.space(6)

            Text {
              Layout.fillWidth: true
              text: "Protocol"
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
            }

            Repeater {
              model: [{ key: "udp", label: "UDP" }, { key: "tcp", label: "TCP 443" }]
              Chip {
                label: modelData.label
                selected: vpn.protocol === modelData.key
                opacity: vpn.multihop && modelData.key === "tcp" ? 0.35 : 1.0
                hasCursor: root.stopIs("proto", modelData.key)
                onEntered: root.takeCursor("proto", modelData.key)
                onActivated: if (!(vpn.multihop && modelData.key === "tcp")) vpn.setProtocol(modelData.key)
              }
            }
          }

          ToggleRow {
            width: parent.width
            label: "Multihop"
            caption: vpn.multihop ? "Two locations in a row · UDP only"
                                  : "Route through two locations"
            checked: vpn.multihop
            hasCursor: root.stopIs("multihop")
            onEntered: root.takeCursor("multihop")
            onActivated: root.toggleMultihop()
          }

          CursorSurface {
            id: entryRow
            width: parent.width
            visible: vpn.multihop
            foreground: root.foreground
            bordered: true
            current: root.pickingEntry
            hasCursor: root.stopIs("entry")
            implicitHeight: Math.max(Style.space(30), entryLabel.implicitHeight + Style.spacing.sm * 2)

            MouseArea {
              anchors.fill: parent
              hoverEnabled: true
              onEntered: root.takeCursor("entry")
              onClicked: root.pickingEntry = !root.pickingEntry
            }

            RowLayout {
              anchors.fill: parent
              anchors.leftMargin: Style.spacing.rowPaddingX
              anchors.rightMargin: Style.spacing.rowPaddingX
              spacing: Style.space(8)

              Text {
                text: vpn.entryLocation ? Model.flag(vpn.entryLocation.country) : ""
                font.pixelSize: Style.font.title
                color: root.foreground
              }

              Text {
                id: entryLabel
                Layout.fillWidth: true
                text: "Entry: " + (vpn.entryLocation ? Model.placeName(vpn.entryLocation) : "–")
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                elide: Text.ElideRight
              }

              Text {
                text: root.pickingEntry ? "Pick below" : "Change"
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }
          }

          Text {
            width: parent.width
            visible: vpn.connected && ((vpn.status.protocol && vpn.status.protocol !== vpn.protocol)
                                       || (vpn.status.via || "") !== vpn.multihopEntry)
            text: "Changes apply on the next connect"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          PanelSeparator { width: parent.width }

          Text {
            width: parent.width
            visible: root.pickingEntry
            text: "Choose where your traffic enters OVPN. The location you connect to is where it leaves."
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          TextField {
            id: searchField
            width: parent.width
            placeholderText: root.pickingEntry ? "Search entry locations  ( / )" : "Search locations  ( / )"
            foreground: root.foreground
            text: root.query
            onTextChanged: root.query = text
            onAccepted: {
              if (root.shownLocations.length > 0) root.chooseLocation(root.shownLocations[0].slug)
            }
            Keys.onEscapePressed: {
              if (text !== "") text = ""
              else keyCatcher.forceActiveFocus()
            }
            Keys.onDownPressed: {
              keyCatcher.forceActiveFocus()
              root.cursorActive = true
              root.takeCursor("location", root.pickingEntry && root.shownLocations.length > 0
                                                  ? root.shownLocations[0].slug : Model.FASTEST)
            }
          }

          LocationRow {
            id: fastestRow
            width: parent.width
            visible: root.query === "" && !root.pickingEntry
            slug: Model.FASTEST
            location: vpn.fastestLocation
            title: "Fastest location"
            subtitle: vpn.fastestLocation ? Model.placeName(vpn.fastestLocation) : "Measuring…"
            flagText: "⚡"
          }

          Column {
            width: parent.width
            spacing: Style.space(2)

            Repeater {
              id: locationRepeater
              model: root.shownLocations
              LocationRow {
                width: column.width
                slug: modelData.slug
                location: modelData
                title: modelData.city
                subtitle: modelData.countryName
                flagText: Model.flag(modelData.country)
              }
            }
          }

          Text {
            width: parent.width
            visible: root.shownLocations.length === 0
            text: vpn.locations.length === 0 ? "Loading locations…" : "No location matches “" + root.query + "”"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            horizontalAlignment: Text.AlignHCenter
          }

          Text {
            width: parent.width
            visible: text !== ""
            text: vpn.lastError
            color: root.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          PanelSeparator { width: parent.width; visible: vpn.signedIn }

          Column {
            width: parent.width
            visible: vpn.signedIn
            spacing: Style.space(6)

            RowLayout {
              width: parent.width
              visible: !root.confirmSignOut
              spacing: Style.space(6)

              Text {
                Layout.fillWidth: true
                text: "Signed in as " + vpn.status.username
                      + (vpn.status.hasPassword ? " · password remembered" : "")
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
              }

              Chip {
                label: "Sign out"
                hasCursor: root.stopIs("signout")
                onEntered: root.takeCursor("signout")
                onActivated: root.signOutRequested()
              }
            }

            Column {
              width: parent.width
              visible: root.confirmSignOut
              spacing: Style.space(6)

              Text {
                width: parent.width
                text: "Signing out disconnects you from " + Model.placeName(vpn.currentLocation) + "."
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                wrapMode: Text.WordWrap
              }

              Row {
                spacing: Style.space(6)

                Chip {
                  label: "Sign out and disconnect"
                  hasCursor: root.stopIs("signout")
                  onEntered: root.takeCursor("signout")
                  onActivated: root.signOutRequested()
                }

                Chip {
                  label: "Cancel"
                  onActivated: root.confirmSignOut = false
                }
              }
            }
          }
        }
      }
    }
  }

  function submitSignIn() {
    vpn.signIn(userField.text, passwordField.text, rememberRow.checked)
    passwordField.text = ""
    keyCatcher.forceActiveFocus()
  }

  component LocationRow: CursorSurface {
    id: row
    property string slug: ""
    property var location: null
    property string title: ""
    property string subtitle: ""
    property string flagText: ""

    readonly property bool isFastest: slug === Model.FASTEST
    readonly property bool online: location ? location.online > 0 : false
    readonly property bool isEntry: !isFastest && vpn.multihop && vpn.multihopEntry === slug
    readonly property bool isCurrent: !isFastest && (root.pickingEntry ? isEntry
                                      : vpn.status.location === slug && (vpn.connected || Model.isBusy(vpn.state)))
    readonly property bool favorite: !isFastest && vpn.isFavorite(slug)
    readonly property var ping: location ? vpn.pings[location.slug] : undefined

    foreground: root.foreground
    hasCursor: root.stopIs("location", slug)
    current: isCurrent
    implicitHeight: Math.max(Style.space(34), labels.implicitHeight + Style.spacing.sm * 2)
    opacity: online || isFastest ? 1.0 : 0.45

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      acceptedButtons: Qt.LeftButton | Qt.RightButton
      onEntered: root.takeCursor("location", row.slug)
      onClicked: function (mouse) {
        if (mouse.button === Qt.RightButton) { if (!row.isFastest) vpn.toggleFavorite(row.slug) }
        else if (!root.pickingEntry && row.isCurrent && vpn.connected) vpn.disconnect()
        else root.chooseLocation(row.slug)
      }
    }

    RowLayout {
      anchors.fill: parent
      anchors.leftMargin: Style.spacing.rowPaddingX
      anchors.rightMargin: Style.spacing.rowPaddingX
      spacing: Style.space(8)

      Text {
        text: row.flagText
        font.pixelSize: Style.font.title
        Layout.preferredWidth: Style.space(20)
        horizontalAlignment: Text.AlignHCenter
        color: root.foreground
      }

      Column {
        id: labels
        Layout.fillWidth: true
        spacing: Style.space(1)

        Text {
          width: parent.width
          text: row.title
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          font.bold: row.isCurrent
          elide: Text.ElideRight
        }

        Text {
          width: parent.width
          text: row.subtitle + (!row.isFastest && !row.online ? " · offline" : "")
                + (row.isEntry && !root.pickingEntry ? " · multihop entry" : "")
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }
      }

      Text {
        visible: !row.isFastest
        text: Model.pingText(row.ping)
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        Layout.preferredWidth: Style.space(40)
        horizontalAlignment: Text.AlignRight
      }

      Rectangle {
        visible: !row.isFastest
        Layout.preferredWidth: Style.space(22)
        implicitHeight: Style.space(4)
        radius: height / 2
        color: Qt.darker(root.foreground, 3.2)

        Rectangle {
          width: parent.width * Model.loadFraction(row.location ? row.location.load : null)
          height: parent.height
          radius: height / 2
          color: row.location && row.location.load >= 80 ? root.urgent : root.foreground
        }
      }

      Text {
        visible: !row.isFastest
        text: row.favorite ? "★" : "☆"
        color: row.favorite ? root.accent : root.dim
        font.pixelSize: Style.font.title
        Layout.preferredWidth: Style.space(16)
        horizontalAlignment: Text.AlignHCenter

        MouseArea {
          anchors.fill: parent
          anchors.margins: -Style.space(4)
          onClicked: vpn.toggleFavorite(row.slug)
        }
      }
    }
  }

  component ToggleRow: CursorSurface {
    id: toggleRow
    property string label: ""
    property string caption: ""
    property bool checked: false

    signal entered()
    signal activated()

    foreground: root.foreground
    implicitHeight: Math.max(Style.space(30), toggleLabels.implicitHeight + Style.spacing.sm * 2)

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      onEntered: toggleRow.entered()
      onClicked: toggleRow.activated()
    }

    RowLayout {
      anchors.fill: parent
      anchors.leftMargin: Style.spacing.rowPaddingX
      anchors.rightMargin: Style.spacing.rowPaddingX
      spacing: Style.space(8)

      Column {
        id: toggleLabels
        Layout.fillWidth: true
        spacing: Style.space(1)

        Text {
          width: parent.width
          text: toggleRow.label
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          elide: Text.ElideRight
        }

        Text {
          width: parent.width
          text: toggleRow.caption
          visible: text !== ""
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
        }
      }

      ToggleSwitch {
        checked: toggleRow.checked
        hasCursor: toggleRow.hasCursor
        foreground: root.foreground
        Layout.alignment: Qt.AlignVCenter
        onToggled: toggleRow.activated()
      }
    }
  }

  component Chip: CursorSurface {
    id: chip
    property string label: ""
    property bool selected: false

    signal entered()
    signal activated()

    foreground: root.foreground
    current: selected
    bordered: true
    implicitWidth: chipLabel.implicitWidth + Style.space(18)
    implicitHeight: Math.max(Style.space(26), chipLabel.implicitHeight + Style.spacing.sm * 2)

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      onEntered: chip.entered()
      onClicked: chip.activated()
    }

    Text {
      id: chipLabel
      anchors.centerIn: parent
      text: chip.label
      color: root.foreground
      opacity: chip.selected ? 1.0 : 0.7
      font.family: root.fontFamily
      font.pixelSize: Style.font.body
    }
  }
}
