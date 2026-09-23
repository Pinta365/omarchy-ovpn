import QtQuick
import Quickshell
import Quickshell.Io
import "Model.js" as Model

// Shared state for every bar instance. A session-only password is kept here
// and passed to the helper on stdin, never in argv.
Item {
  id: root

  property var shell: null
  // Pushed in by the widgets.
  property var settings: ({})
  property bool settingsReady: false
  onSettingsChanged: settingsReady = true
  property int openPanels: 0
  readonly property bool panelOpen: openPanels > 0

  property var status: Model.defaultStatus()
  property var locations: []
  property var pings: ({})
  property string publicIp: ""

  property string lastError: ""
  property bool needsCredentials: false
  property bool authFailed: false
  // Tells a drop from a user disconnect.
  property string intent: ""        // "", "connect", "disconnect"
  property string pendingLocation: ""
  property string sessionPassword: ""
  // Typed but unproven, until a connection confirms the account.
  property string pendingUsername: ""
  property bool pendingRemember: false
  // From a sign-in submit until the connection checking it answers.
  property bool verifying: false
  property bool autoConnectDone: false
  property bool everLoaded: false

  readonly property string state: status.state
  readonly property bool connected: state === Model.STATE_CONNECTED
  readonly property bool busy: Model.isBusy(state) || actionProcess.running
  readonly property bool signedIn: String(status.username || "") !== ""
  readonly property bool canConnect: (signedIn || pendingUsername !== "")
                                     && (status.hasPassword || sessionPassword !== "")
  readonly property bool openvpnMissing: everLoaded && status.openvpnSupport === false
  // In the keyring but turned down by OVPN; not used until confirmed.
  readonly property bool passwordRejected: status.passwordRejected === true
  property bool installing: false
  readonly property string multihopEntry: status.preferredVia || ""
  readonly property bool multihop: multihopEntry !== ""
  readonly property var entryLocation: Model.byslug(locations, multihopEntry)
  readonly property var viaLocation: Model.byslug(locations, status.via)
  // OVPN has no TCP multihop ports, so the two exclude each other.
  readonly property bool tcpBlocked: multihop
  readonly property bool multihopBlocked: protocol === "tcp"
  readonly property var protocols: ["udp", "tcp", "wg"]

  function protocolAllowed(proto) {
    if (proto === "tcp" && tcpBlocked) return false
    return proto === "wg" || !openvpnMissing
  }

  function cycleProtocol() {
    var usable = protocols.filter(protocolAllowed)
    var here = usable.indexOf(protocol)
    setProtocol(usable[(here + 1) % usable.length])
  }

  // A preference that cannot be honoured yields rather than blocking: no TCP
  // ports for multihop, no OpenVPN at all without the add-on.
  readonly property string protocol: {
    var p = status.preferredProtocol || "udp"
    if (openvpnMissing && p !== "wg") return "wg"
    return multihop && p === "tcp" ? "udp" : p
  }
  readonly property var currentLocation: Model.byslug(locations, status.location)
  readonly property var lastLocation: Model.byslug(locations, status.last)
  readonly property var fastestLocation: Model.fastest(locations, pings, multihopEntry)

  readonly property bool autoConnect: setting("autoConnect", false) === true
  readonly property string autoConnectTarget: String(setting("autoConnectTarget", "Last used"))
  readonly property bool notifyOnDrop: setting("notifyOnDrop", true) === true
  readonly property int pingIntervalSec: intSetting("pingIntervalSec", 60, 15, 600)

  readonly property string pluginDir: Qt.resolvedUrl(".").toString().replace(/^file:\/\//, "")
  readonly property string helperPath: pluginDir + "helper/ovpnctl.py"

  function panelOpened() { openPanels += 1 }
  function panelClosed() { openPanels = Math.max(0, openPanels - 1) }

  function notify(title, body, urgency) {
    Quickshell.execDetached(["notify-send", "-a", "OVPN", "-u", urgency || "normal", title, body])
  }

  function setting(name, fallback) {
    var value = settings ? settings[name] : undefined
    return value === undefined || value === null ? fallback : value
  }

  function intSetting(name, fallback, min, max) {
    var n = parseInt(String(setting(name, fallback)), 10)
    if (!isFinite(n)) n = fallback
    return Math.max(min, Math.min(max, n))
  }

  function commandFor(args, timeoutSec) {
    var command = ["/usr/bin/timeout", String(timeoutSec || 20), "/usr/bin/python3", helperPath]
    for (var i = 0; i < args.length; i++) command.push(args[i])
    return command
  }

  function loadServers(refresh) {
    if (serversProcess.running) return
    serversProcess.command = commandFor(refresh ? ["servers", "--refresh"] : ["servers"])
    serversProcess.running = true
  }

  function ping() {
    if (pingProcess.running) return
    pingProcess.command = commandFor(["ping"], 15)
    pingProcess.running = true
  }

  function lookupIp() {
    if (ipProcess.running) return
    ipProcess.command = commandFor(["status", "--ip"], 15)
    ipProcess.running = true
  }

  function startWatch() {
    if (watchProcess.running) return
    watchProcess.command = ["/usr/bin/python3", helperPath, "watch"]
    watchProcess.running = true
  }

  function runAction(kind, args, secretJson, timeoutSec) {
    if (actionProcess.running) return false
    actionProcess.kind = kind
    actionProcess.secret = secretJson || "{}"
    actionProcess.command = commandFor(args, timeoutSec)
    actionProcess.running = true
    return true
  }

  function resolveTarget(slug, exclude) {
    if (slug === Model.FASTEST || slug === "" || slug === undefined) {
      var best = Model.fastest(locations, pings, exclude)
      if (!best && lastLocation && lastLocation.slug !== exclude) best = lastLocation
      return best ? best.slug : ""
    }
    return slug
  }

  function setMultihopEntry(slug) {
    var entry = slug || ""
    if (entry === multihopEntry) return
    var s = Object.assign({}, status)
    s.preferredVia = entry === "" ? null : entry
    status = s
    multihopProcess.command = commandFor(["multihop", entry === "" ? "off" : entry])
    multihopProcess.running = true
  }

  function connectTo(slug) {
    if (actionProcess.running) { verifying = false; return }
    var target = resolveTarget(slug, multihopEntry)
    if (target === "") { verifying = false; lastError = "No locations loaded yet"; return }
    if (multihop && target === multihopEntry) {
      verifying = false
      lastError = "Pick an exit location other than the entry (" + (entryLocation ? entryLocation.city : multihopEntry) + ")"
      return
    }
    lastError = ""
    authFailed = false
    if (!canConnect) {
      pendingLocation = target
      needsCredentials = true
      return
    }
    intent = "connect"
    pendingLocation = ""
    var s = Object.assign({}, status)
    s.state = Model.STATE_CONNECTING
    s.location = target
    s.via = multihop ? multihopEntry : null
    status = s
    var payload = ({})
    if (sessionPassword !== "") {
      payload.password = sessionPassword
      payload.remember = pendingRemember
    }
    if (pendingUsername !== "") payload.username = pendingUsername
    var secret = JSON.stringify(payload)
    var args = ["connect", target, "--timeout", "45"]
    if (multihop) args.push("--via", multihopEntry)
    runAction("connect", args, secret, 60)
  }

  // Own process, so it can cancel a connect in flight.
  function disconnect() {
    if (disconnectProcess.running) return
    intent = "disconnect"
    lastError = ""
    var s = Object.assign({}, status)
    if (s.state !== Model.STATE_OFF) s.state = Model.STATE_DISCONNECTING
    status = s
    disconnectProcess.command = commandFor(["disconnect"], 40)
    disconnectProcess.running = true
  }

  // Runs in a terminal so the package manager can ask for the password there.
  function installOpenvpn() {
    installing = true
    Quickshell.execDetached(["xdg-terminal-exec", "--", "bash", "-c",
      "omarchy pkg add networkmanager-openvpn; echo; read -rsn1 -p 'Press any key to close.'"])
  }

  function quickToggle() {
    if (connected || state === Model.STATE_CONNECTING) disconnect()
    else connectTo(status.last || Model.FASTEST)
  }

  function signIn(username, password, remember) {
    var user = String(username || "").trim()
    if (user === "" || String(password || "") === "") {
      lastError = "Enter your OVPN username and password"
      return
    }
    // Only a connection verifies credentials, so sign-in carries them into
    // one. The helper saves the account only if it works.
    sessionPassword = password
    pendingUsername = user
    pendingRemember = remember === true
    authFailed = false
    verifying = true
    connectTo(pendingLocation !== "" ? pendingLocation : (status.last || Model.FASTEST))
  }

  // Own process: a connect in flight owns the action slot.
  function signOut() {
    if (state !== Model.STATE_OFF) disconnect()
    sessionPassword = ""
    pendingUsername = ""
    pendingRemember = false
    verifying = false
    needsCredentials = false
    var cleared = Object.assign({}, status)
    cleared.username = ""
    cleared.hasPassword = false
    status = cleared
    logoutProcess.command = commandFor(["logout"], 20)
    logoutProcess.running = true
  }

  function cancelSignIn() {
    verifying = false
    needsCredentials = false
    pendingLocation = ""
    pendingUsername = ""
    authFailed = false
  }

  function toggleFavorite(slug) {
    var favs = (status.favorites || []).slice()
    var on = favs.indexOf(slug) < 0
    if (on) favs.push(slug)
    else favs.splice(favs.indexOf(slug), 1)
    var s = Object.assign({}, status)
    s.favorites = favs
    status = s
    favoriteProcess.command = commandFor(on ? ["favorite", slug] : ["favorite", slug, "--off"])
    favoriteProcess.running = true
  }

  function isFavorite(slug) {
    return (status.favorites || []).indexOf(slug) >= 0
  }

  function setProtocol(proto) {
    if (proto === protocol || !protocolAllowed(proto)) return
    var s = Object.assign({}, status)
    s.preferredProtocol = proto
    status = s
    protocolProcess.command = commandFor(["protocol", proto])
    protocolProcess.running = true
  }

  function applyStatus(parsed) {
    if (!parsed || parsed.ok !== true || parsed.state === undefined) return
    var before = status.state
    // Hold the optimistic state while our own connect is starting.
    if (actionProcess.running && actionProcess.kind === "connect" && intent === "connect"
        && parsed.state === Model.STATE_OFF) {
      var keep = Object.assign({}, parsed)
      keep.state = Model.STATE_CONNECTING
      keep.location = status.location
      keep.via = status.via
      parsed = keep
    }
    status = parsed
    everLoaded = true
    if (before !== parsed.state) onStateTransition(before, parsed.state)
    maybeAutoConnect()
  }

  function onStateTransition(before, after) {
    if (after === Model.STATE_CONNECTED || after === Model.STATE_OFF) ipDelay.restart()
    if (after === Model.STATE_CONNECTED) intent = ""

    if (before === Model.STATE_CONNECTED && after === Model.STATE_OFF) {
      if (intent !== "disconnect" && notifyOnDrop) {
        var place = Model.placeName(Model.byslug(locations, status.last))
        notify("OVPN disconnected",
          "The tunnel" + (place !== "" ? " to " + place : "") + " dropped. You are not protected.",
          "critical")
      }
      intent = ""
    }
  }

  function maybeAutoConnect() {
    if (autoConnectDone || !everLoaded || !settingsReady || locations.length === 0) return
    autoConnectDone = true
    if (!autoConnect || state !== Model.STATE_OFF || !canConnect) return
    var target = Model.FASTEST
    if (autoConnectTarget === "Last used" && status.last) target = status.last
    else if (autoConnectTarget === "First favorite" && (status.favorites || []).length > 0) target = status.favorites[0]
    connectTo(target)
  }

  function applyAction(kind, parsed) {
    if (kind === "connect") verifying = false
    if (!parsed) {
      lastError = "The helper returned nothing"
      return
    }
    if (kind === "logout") {
      var cleared = Object.assign({}, status)
      cleared.username = ""
      cleared.hasPassword = false
      status = cleared
      return
    }
    if (parsed.ok) {
      // Proven; the helper has saved the account, keyring included.
      pendingUsername = ""
      pendingRemember = false
      needsCredentials = false
      applyStatus(parsed)
      return
    }
    var cancelled = intent === "disconnect"
    intent = ""
    if (cancelled) {
      var idle = Object.assign({}, status)
      idle.state = Model.STATE_OFF
      status = idle
      return
    }
    if (parsed.missingOpenvpn) {
      var missing = Object.assign({}, status)
      missing.openvpnSupport = false
      missing.state = Model.STATE_OFF
      status = missing
      return
    }
    if (parsed.authFailed || parsed.needsPassword || parsed.needsLogin) {
      sessionPassword = ""
      authFailed = parsed.authFailed === true
      needsCredentials = true
      pendingLocation = status.location || ""
    }
    lastError = parsed.error || "Could not connect"
    var off = Object.assign({}, status)
    off.state = Model.STATE_OFF
    status = off
  }

  onPanelOpenChanged: {
    if (!panelOpen) return
    loadServers(false)
    ping()
    lookupIp()
  }

  Component.onCompleted: {
    loadServers(false)
    ping()
    startWatch()
  }

  Process {
    id: watchProcess
    stdout: SplitParser {
      onRead: function (line) { root.applyStatus(Model.parse(line)) }
    }
    onExited: watchRestart.restart()
  }

  Timer {
    id: watchRestart
    interval: 2000
    onTriggered: root.startWatch()
  }

  Process {
    id: actionProcess
    property string secret: "{}"
    property string kind: ""
    stdinEnabled: true
    stdout: StdioCollector { id: actionOut; waitForEnd: true }
    onStarted: {
      write(secret + "\n")
      secret = "{}"
    }
    onExited: function (exitCode) {
      // The helper prints why even when killed, so prefer its message.
      var parsed = Model.parse(actionOut.text)
      root.applyAction(kind, parsed || { ok: false, error: exitCode === 124 ? "Timed out" : "The helper returned nothing" })
    }
  }

  Process {
    id: serversProcess
    stdout: StdioCollector { id: serversOut; waitForEnd: true }
    onExited: {
      var parsed = Model.parse(serversOut.text)
      if (parsed && parsed.ok && parsed.datacenters) {
        if (root.locations.length === 0 && root.lastError.indexOf("load locations") >= 0) root.lastError = ""
        root.locations = parsed.datacenters
        root.maybeAutoConnect()
      } else if (parsed && parsed.error) {
        root.lastError = parsed.error
      }
    }
  }

  Process {
    id: pingProcess
    stdout: StdioCollector { id: pingOut; waitForEnd: true }
    onExited: {
      var parsed = Model.parse(pingOut.text)
      if (parsed && parsed.ok) root.pings = parsed.ping
    }
  }

  Process {
    id: ipProcess
    stdout: StdioCollector { id: ipOut; waitForEnd: true }
    onExited: {
      var parsed = Model.parse(ipOut.text)
      if (!parsed || !parsed.ok) return
      root.applyStatus(parsed)
      root.publicIp = parsed.ip || ""
    }
  }

  Process {
    id: disconnectProcess
    stdout: StdioCollector { id: disconnectOut; waitForEnd: true }
    onExited: {
      var parsed = Model.parse(disconnectOut.text)
      if (parsed && parsed.ok) root.applyStatus(parsed)
      else if (parsed && parsed.error) root.lastError = parsed.error
    }
  }

  Process {
    id: logoutProcess
    stdout: StdioCollector { id: logoutOut; waitForEnd: true }
    onExited: {
      var parsed = Model.parse(logoutOut.text)
      if (parsed && parsed.ok === false && parsed.error) root.lastError = parsed.error
    }
  }

  Process { id: favoriteProcess }
  Process { id: multihopProcess }
  Process { id: protocolProcess }

  Timer {
    interval: 3000
    repeat: true
    running: root.installing && root.openvpnMissing
    onTriggered: root.lookupIp()
  }

  onOpenvpnMissingChanged: if (!openvpnMissing) installing = false

  // Routes settle shortly after NetworkManager reports the change.
  Timer {
    id: ipDelay
    interval: 1500
    onTriggered: root.lookupIp()
  }

  Timer {
    interval: root.pingIntervalSec * 1000
    running: root.panelOpen
    repeat: true
    onTriggered: root.ping()
  }

  Timer {
    interval: 30000
    repeat: true
    running: root.locations.length === 0
    onTriggered: root.loadServers(false)
  }

  // Load figures feed the fastest pick.
  Timer {
    interval: 3600 * 1000
    running: true
    repeat: true
    onTriggered: root.loadServers(true)
  }
}
