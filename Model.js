// Pure helpers shared by the panel and the service.

var STATE_OFF = "off"
var STATE_CONNECTING = "connecting"
var STATE_CONNECTED = "connected"
var STATE_DISCONNECTING = "disconnecting"

var FASTEST = "__fastest__"

// Input hygiene, not a memory bound: the collector has already buffered the
// text by the time this runs. The helper's replies are a few kilobytes.
var MAX_OUTPUT_CHARS = 1024 * 1024

function parse(text) {
  if (!text) return null
  if (String(text).length > MAX_OUTPUT_CHARS) return null
  var lines = String(text).trim().split("\n")
  // Last JSON line wins, so a stray warning above it is ignored.
  for (var i = lines.length - 1; i >= 0; i--) {
    var line = lines[i].trim()
    if (line === "" || line.charAt(0) !== "{") continue
    try { return JSON.parse(line) } catch (e) { return null }
  }
  return null
}

function defaultStatus() {
  return {
    ok: false,
    state: STATE_OFF,
    location: null,
    protocol: null,
    since: null,
    tunnelAddress: null,
    username: "",
    hasPassword: false,
    favorites: [],
    last: null,
    via: null,
    preferredProtocol: "udp",
    preferredVia: null
  }
}

function isBusy(state) {
  return state === STATE_CONNECTING || state === STATE_DISCONNECTING
}

// "SE" -> 🇸🇪
function flag(cc) {
  var code = String(cc || "").toUpperCase()
  if (!/^[A-Z]{2}$/.test(code)) return code
  return String.fromCodePoint(0x1F1E6 + code.charCodeAt(0) - 65,
                              0x1F1E6 + code.charCodeAt(1) - 65)
}

function byslug(locations, slug) {
  for (var i = 0; i < locations.length; i++) {
    if (locations[i].slug === slug) return locations[i]
  }
  return null
}

function routeName(exitLoc, viaLoc) {
  if (!exitLoc) return ""
  return viaLoc ? viaLoc.city + " → " + exitLoc.city : exitLoc.city
}

function placeName(loc) {
  if (!loc) return ""
  return loc.city + ", " + loc.countryName
}

// Lower is better; load weighs in so a busy nearby site can lose.
function score(loc, pings) {
  var ms = pings ? pings[loc.slug] : undefined
  if (ms === undefined || ms === null) return Infinity
  var load = loc.load === null || loc.load === undefined ? 50 : loc.load
  return ms + load * 0.6
}

function fastest(locations, pings, exclude) {
  var best = null
  var bestScore = Infinity
  for (var i = 0; i < locations.length; i++) {
    var loc = locations[i]
    if (!loc.online || loc.slug === exclude) continue
    var s = score(loc, pings)
    if (s < bestScore) { best = loc; bestScore = s }
  }
  return best
}

function matches(loc, query) {
  var q = String(query || "").trim().toLowerCase()
  if (q === "") return true
  return String(loc.city).toLowerCase().indexOf(q) >= 0
      || String(loc.countryName).toLowerCase().indexOf(q) >= 0
      || String(loc.country).toLowerCase() === q
}

// Favorites, then online, then country and city.
function sorted(locations, favorites, query) {
  var favs = favorites || []
  var out = []
  for (var i = 0; i < locations.length; i++) {
    if (matches(locations[i], query)) out.push(locations[i])
  }
  out.sort(function (a, b) {
    var fa = favs.indexOf(a.slug) >= 0 ? 0 : 1
    var fb = favs.indexOf(b.slug) >= 0 ? 0 : 1
    if (fa !== fb) return fa - fb
    var oa = a.online > 0 ? 0 : 1
    var ob = b.online > 0 ? 0 : 1
    if (oa !== ob) return oa - ob
    var c = String(a.countryName).localeCompare(String(b.countryName))
    return c !== 0 ? c : String(a.city).localeCompare(String(b.city))
  })
  return out
}

function protocolLabel(proto) {
  return proto === "wg" ? "WG" : String(proto || "").toUpperCase()
}

function pingText(ms) {
  return ms === undefined || ms === null ? "–" : ms + " ms"
}

function loadFraction(load) {
  if (load === null || load === undefined) return 0
  return Math.max(0, Math.min(1, load / 100))
}

function duration(since, now) {
  if (!since) return ""
  var s = Math.max(0, Math.floor(now / 1000) - since)
  var h = Math.floor(s / 3600)
  var m = Math.floor((s % 3600) / 60)
  var sec = s % 60
  function pad(n) { return n < 10 ? "0" + n : String(n) }
  return (h > 0 ? h + ":" : "") + pad(m) + ":" + pad(sec)
}

function stateTitle(state) {
  if (state === STATE_CONNECTED) return "Protected"
  if (state === STATE_CONNECTING) return "Connecting…"
  if (state === STATE_DISCONNECTING) return "Disconnecting…"
  return "Not protected"
}

