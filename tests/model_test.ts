// Tests for Model.js.
//
// Model.js is loaded as a plain script rather than imported, because QML's
// `import "Model.js" as Model` needs a file with no export statements. The
// Function wrapper gives us the same top-level bindings QML would see.

import { assertEquals } from "jsr:@std/assert@1"

const source = await Deno.readTextFile(new URL("../Model.js", import.meta.url))
const Model = new Function(`
  ${source}
  return { parse, flag, sorted, fastest, protocolLabel, duration, routeName, stateTitle }
`)() as Record<string, any>

const loc = (slug: string, country = "SE", online = 1, load = 10) => ({
  slug, city: slug, country, countryName: country, online, load,
})

Deno.test("parse takes the last JSON line, ignoring noise above it", () => {
  assertEquals(Model.parse('warning: something\n{"ok":true,"state":"off"}').state, "off")
  assertEquals(Model.parse("not json at all"), null)
  assertEquals(Model.parse(""), null)
})

Deno.test("fastest weighs load and skips offline and excluded locations", () => {
  const near = loc("near"), far = loc("far"), busy = loc("busy", "SE", 1, 100)
  const offline = loc("offline", "SE", 0)
  const pings = { near: 20, far: 60, busy: 10, offline: 1 }

  assertEquals(Model.fastest([near, far, busy, offline], pings).slug, "near")
  // The entry of a multihop pair cannot also be the exit.
  assertEquals(Model.fastest([near, far], pings, "near").slug, "far")
  // Nothing pingable means no pick rather than a wrong one.
  assertEquals(Model.fastest([near], {}), null)
})

Deno.test("sorted puts favorites first, then online, and filters by query", () => {
  const list = [loc("berlin", "DE"), loc("malmo"), loc("oslo", "NO", 0)]
  assertEquals(Model.sorted(list, ["malmo"], "").map((l: any) => l.slug),
    ["malmo", "berlin", "oslo"])
  assertEquals(Model.sorted(list, [], "mal").map((l: any) => l.slug), ["malmo"])
})

Deno.test("flag and protocolLabel render what the panel shows", () => {
  assertEquals(Model.flag("SE"), "🇸🇪")
  assertEquals(Model.flag("?"), "?")
  assertEquals(Model.protocolLabel("wg"), "WG")
  assertEquals(Model.protocolLabel("udp"), "UDP")
})

Deno.test("duration counts from the connect time", () => {
  const now = 1_000_000_000_000
  assertEquals(Model.duration(now / 1000 - 65, now), "01:05")
  assertEquals(Model.duration(now / 1000 - 3665, now), "1:01:05")
  assertEquals(Model.duration(null, now), "")
})
