#!/usr/bin/python3
"""OVPN control helper for the Omarchy shell plugin.

Each command prints one JSON object (``watch`` one per line). Secrets are read
from stdin or the keyring, never from arguments.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

API = "https://www.ovpn.com/v2/api/client"
PROFILE = "OVPN (Omarchy)"
USER_AGENT = "omarchy-ovpn/0.1"

HERE = os.path.dirname(os.path.realpath(__file__))
ASSETS = os.path.normpath(os.path.join(HERE, "..", "assets"))
CA_FILE = os.path.join(ASSETS, "ovpn-ca.pem")
TA_FILE = os.path.join(ASSETS, "ovpn-ta.key")

CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "ovpn-omarchy")
STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "ovpn-omarchy")
ENTRY_CACHE = os.path.join(CACHE_DIR, "entry.json")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
ENTRY_TTL = 3600

# OVPN serves OpenVPN on UDP 1194/1195 and TCP 443 only.
PORTS = {"udp": [1194, 1195], "tcp": [443]}
DATA_CIPHERS = "CHACHA20-POLY1305:AES-256-GCM:AES-256-CBC:AES-128-GCM"

KEYRING_SCHEMA_NAME = "se.ovpn.omarchy"

# Installed by networkmanager-openvpn, which Omarchy does not ship.
NM_OPENVPN_MARKER = os.environ.get(
    "OVPN_OMARCHY_NM_OPENVPN", "/usr/lib/NetworkManager/VPN/nm-openvpn-service.name"
)


class HelperError(Exception):
    pass


def emit(obj):
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def fail(message, **extra):
    emit(dict({"ok": False, "error": message}, **extra))
    sys.exit(1)


def load_state():
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=STATE_DIR, prefix=".state.")
    with os.fdopen(fd, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, STATE_FILE)


def read_stdin_json():
    """One line of JSON: the shell writes to the pipe but never closes it."""
    if sys.stdin is None or sys.stdin.closed or sys.stdin.isatty():
        return {}
    raw = sys.stdin.readline()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        raise HelperError("stdin is not valid JSON")
    if not isinstance(data, dict):
        raise HelperError("stdin must be a JSON object")
    return data


def http_json(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except ValueError:
            raise HelperError("HTTP %d from %s" % (e.code, url))
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise HelperError("request to %s failed: %s" % (url, e))


def fetch_entry(refresh=False):
    """Return (entry, fromCache). Falls back to a stale cache when offline."""
    cached = None
    try:
        with open(ENTRY_CACHE) as f:
            cached = json.load(f)
        age = time.time() - os.path.getmtime(ENTRY_CACHE)
        if not refresh and age < ENTRY_TTL:
            return cached, True
    except (OSError, ValueError):
        pass

    try:
        entry = http_json(API + "/entry")
        if not entry.get("success") or not isinstance(entry.get("datacenters"), list):
            raise HelperError("unexpected server list response")
    except HelperError:
        if cached is not None:
            return cached, True
        raise HelperError("Can't reach OVPN to load locations. Check your internet connection.")

    os.makedirs(CACHE_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=CACHE_DIR, prefix=".entry.")
    with os.fdopen(fd, "w") as f:
        json.dump(entry, f)
    os.replace(tmp, ENTRY_CACHE)
    return entry, False


def summarize(dc):
    servers = dc.get("servers") or []
    online = [s for s in servers if s.get("online")]
    load = round(sum(s.get("load", 0) for s in online) / len(online)) if online else None
    return {
        "slug": dc.get("slug"),
        "city": dc.get("city"),
        "country": dc.get("country"),
        "countryName": dc.get("country_name"),
        "pingAddress": dc.get("ping_address"),
        "servers": len(servers),
        "online": len(online),
        "load": load,
    }


def find_datacenter(entry, slug):
    for dc in entry.get("datacenters", []):
        if dc.get("slug") == slug:
            return dc
    raise HelperError("unknown location '%s'" % slug)


PING_TIME = re.compile(r"time[=<]([\d.]+)\s*ms")


def ping_one(address):
    try:
        out = subprocess.run(
            ["ping", "-n", "-c", "1", "-W", "1", address],
            capture_output=True, text=True, timeout=3,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = PING_TIME.search(out)
    return round(float(m.group(1))) if m else None


def ping_all(entry):
    targets = [(dc["slug"], dc.get("ping_address")) for dc in entry.get("datacenters", []) if dc.get("ping_address")]
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = pool.map(lambda t: (t[0], ping_one(t[1])), targets)
    return dict(results)


def _secret():
    try:
        import gi

        gi.require_version("Secret", "1")
        from gi.repository import Secret
    except (ImportError, ValueError):
        raise HelperError("libsecret GObject bindings missing (run under /usr/bin/python3 with libsecret installed)")

    schema = Secret.Schema.new(
        KEYRING_SCHEMA_NAME, Secret.SchemaFlags.NONE, {"username": Secret.SchemaAttributeType.STRING}
    )
    return Secret, schema


def keyring_lookup(username):
    if not username:
        return None
    try:
        Secret, schema = _secret()
        return Secret.password_lookup_sync(schema, {"username": username}, None)
    except Exception:
        return None


def keyring_store(username, password):
    Secret, schema = _secret()
    ok = Secret.password_store_sync(
        schema, {"username": username}, Secret.COLLECTION_DEFAULT, "OVPN (%s)" % username, password, None
    )
    if not ok:
        raise HelperError("could not store the password in the keyring")


def keyring_clear(username):
    try:
        Secret, schema = _secret()
        Secret.password_clear_sync(schema, {"username": username}, None)
    except Exception:
        pass


def nmcli(args, input_text=None, timeout=90):
    try:
        proc = subprocess.run(
            ["nmcli"] + args, input=input_text, capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        raise HelperError("nmcli not found; NetworkManager is required")
    except subprocess.TimeoutExpired:
        raise HelperError("nmcli timed out")
    return proc


def nm_escape(value):
    return str(value).replace("\\", "\\\\").replace(",", "\\,")


def online_servers(dc):
    servers = [s for s in dc.get("servers") or [] if s.get("online") and s.get("ip")]
    return sorted(servers, key=lambda s: s.get("load", 100))


def pick_multihop(entry_dc, exit_dc):
    """Multihop: entry servers' IPs on the exit server's port (VPN37 -> 20037), UDP only."""
    if entry_dc.get("slug") == exit_dc.get("slug"):
        raise HelperError("entry and exit must be different locations")
    entries = online_servers(entry_dc)[:3]
    exits = [s for s in online_servers(exit_dc) if s.get("multihop_openvpn_port")]
    if not entries:
        raise HelperError("no online server in %s" % entry_dc.get("city"))
    if not exits:
        raise HelperError("no multihop-capable server in %s" % exit_dc.get("city"))
    exit_server = exits[0]
    port = int(exit_server["multihop_openvpn_port"])
    return ["%s:%d" % (s["ip"], port) for s in entries], exit_server


def vpn_data(dc, proto, username, remotes_override=None):
    if remotes_override:
        remotes = ", ".join(remotes_override)
    else:
        hosts = dc.get("pools") or []
        if not hosts:
            raise HelperError("location '%s' has no connection pools" % dc.get("slug"))
        remotes = ", ".join(
            "%s:%d%s" % (host, port, ":tcp-client" if proto == "tcp" else "")
            for host in hosts for port in PORTS[proto]
        )
    items = {
        "allow-compression": "asym",
        "ca": CA_FILE,
        "challenge-response-flags": "2",
        "cipher": "CHACHA20-POLY1305",
        "connection-type": "password",
        "data-ciphers": DATA_CIPHERS,
        "dev": "tun",
        # Never saved; supplied on each activation.
        "password-flags": "2",
        "remote": remotes,
        "remote-random": "yes",
        "reneg-seconds": "0",
        "ta": TA_FILE,
        "ta-dir": "1",
        "username": username,
    }
    if proto == "tcp":
        items["proto-tcp"] = "yes"
    return ", ".join("%s = %s" % (k, nm_escape(v)) for k, v in items.items())


def profile_exists():
    proc = nmcli(["-g", "connection.id", "connection", "show", PROFILE])
    return proc.returncode == 0


def write_profile(dc, proto, username, remotes_override=None):
    data = vpn_data(dc, proto, username, remotes_override)
    if profile_exists():
        # Replaced wholesale so switching to UDP drops proto-tcp.
        proc = nmcli(["connection", "modify", PROFILE, "vpn.data", data])
    else:
        proc = nmcli([
            "connection", "add", "type", "vpn", "con-name", PROFILE, "vpn-type", "openvpn",
            "vpn.data", data, "connection.autoconnect", "no",
        ])
    if proc.returncode != 0:
        raise HelperError("could not write the NetworkManager profile: " + proc.stderr.strip())


def activate(password, timeout):
    """Returns (ok, error, authFailed). OpenVPN retries a rejected password
    forever, so watch the journal for AUTH_FAILED instead of waiting it out."""
    import select

    journal = None
    try:
        journal = subprocess.Popen(
            ["journalctl", "-f", "-n", "0", "-o", "cat", "SYSLOG_IDENTIFIER=nm-openvpn"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
    except OSError:
        journal = None

    up = subprocess.Popen(
        ["nmcli", "--wait", str(timeout), "connection", "up", "id", PROFILE, "passwd-file", "/dev/stdin"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    up.stdin.write("vpn.secrets.password:%s\n" % password)
    up.stdin.close()

    auth_failed = False
    deadline = time.time() + timeout + 10
    try:
        while up.poll() is None and time.time() < deadline:
            if journal is None:
                time.sleep(0.2)
                continue
            ready, _, _ = select.select([journal.stdout], [], [], 0.2)
            if ready:
                line = journal.stdout.readline()
                if "AUTH_FAILED" in line:
                    auth_failed = True
                    break
    finally:
        if journal is not None:
            journal.terminate()

    if auth_failed:
        up.terminate()
        return False, "OVPN rejected the username or password", True
    if up.poll() is None:
        up.terminate()
        return False, "timed out while connecting", False

    stdout, stderr = up.communicate()
    if up.returncode != 0:
        lines = (stderr or stdout).strip().splitlines()
        return False, (lines[-1] if lines else "activation failed"), False
    return True, None, False


def forget_cached_secret():
    # NM keeps the activation's secret in memory even with password-flags=2.
    nmcli(["connection", "modify", PROFILE, "vpn.secrets", ""])


def active_details():
    """Active profile fields, or None. VPN.VPN-STATE is only in the full -t listing."""
    proc = nmcli(["-t", "connection", "show", "--active", PROFILE])
    if proc.returncode != 0:
        return None
    fields = {}
    for line in proc.stdout.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields.setdefault(key, value)
    return fields


def vpn_state(details):
    if details is None:
        return "off"
    state = details.get("GENERAL.STATE", "")
    if state == "deactivating":
        return "disconnecting"
    # NMVpnConnectionState 5 = activated.
    if details.get("VPN.VPN-STATE", "").startswith("5"):
        return "connected"
    if state in ("activating", "activated"):
        return "connecting"
    return "off"


IP_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")


def public_ip():
    """Off OVPN the lookup fails, but its error message still carries our IP."""
    try:
        data = http_json(API + "/ptr", timeout=6)
    except HelperError:
        return {"ip": None, "protected": None}
    text = json.dumps(data)
    m = IP_RE.search(text)
    return {
        "ip": data.get("ip") or (m.group(1) if m else None),
        "ptr": data.get("ptr"),
        "protected": bool(data.get("success")),
    }


def status_payload(with_ip=False):
    state = load_state()
    details = active_details()
    vs = vpn_state(details)
    address = (details or {}).get("IP4.ADDRESS[1]") if vs == "connected" else None
    payload = {
        "ok": True,
        "state": vs,
        "location": state.get("current") if vs != "off" else None,
        "protocol": state.get("currentProtocol") if vs != "off" else None,
        "via": state.get("currentVia") if vs != "off" else None,
        "server": state.get("currentServer") if vs != "off" else None,
        "lastVia": state.get("lastVia"),
        "since": state.get("connectedSince") if vs == "connected" else None,
        "tunnelAddress": address,
        "username": state.get("username") or "",
        "hasPassword": bool(keyring_lookup(state.get("username"))),
        "openvpnSupport": os.path.isfile(NM_OPENVPN_MARKER),
        "favorites": state.get("favorites") or [],
        "preferredProtocol": state.get("protocol") or "udp",
        "preferredVia": state.get("via") or None,
        "last": state.get("last"),
    }
    if with_ip:
        payload.update(public_ip())
    return payload


def cmd_servers(args):
    entry, cached = fetch_entry(args.refresh)
    emit({
        "ok": True,
        "cached": cached,
        "datacenters": [summarize(dc) for dc in entry.get("datacenters", [])],
    })


def cmd_ping(args):
    entry, _ = fetch_entry()
    emit({"ok": True, "ping": ping_all(entry)})


def cmd_status(args):
    emit(status_payload(with_ip=args.ip))


def cmd_login(args):
    data = read_stdin_json()
    username = str(data.get("username") or "").strip()
    password = data.get("password")
    remember = bool(data.get("remember"))
    if not username:
        raise HelperError("username is required")

    state = load_state()
    previous = state.get("username")
    if previous and previous != username:
        keyring_clear(previous)
    state["username"] = username
    save_state(state)

    if remember and password:
        keyring_store(username, str(password))
    elif not remember:
        keyring_clear(username)
    emit({"ok": True, "username": username, "hasPassword": bool(keyring_lookup(username))})


def cmd_logout(args):
    state = load_state()
    username = state.pop("username", None)
    if username:
        keyring_clear(username)
    save_state(state)
    emit({"ok": True})


def cmd_connect(args):
    if not os.path.isfile(NM_OPENVPN_MARKER):
        fail("networkmanager-openvpn is not installed", missingOpenvpn=True)
    if not (os.path.isfile(CA_FILE) and os.path.isfile(TA_FILE)):
        raise HelperError("CA or tls-auth file missing from %s" % ASSETS)

    state = load_state()
    username = state.get("username")
    if not username:
        fail("not signed in", needsLogin=True)

    password = read_stdin_json().get("password") or keyring_lookup(username)
    if not password:
        emit({"ok": False, "error": "password required", "needsPassword": True})
        sys.exit(1)

    proto = args.proto or state.get("protocol") or "udp"
    entry, _ = fetch_entry()
    dc = find_datacenter(entry, args.location)
    remotes = None
    exit_server = None
    if args.via:
        remotes, exit_server = pick_multihop(find_datacenter(entry, args.via), dc)
        proto = "udp"
    write_profile(dc, proto, username, remotes)

    state.update({
        "current": dc["slug"],
        "currentVia": args.via or None,
        "currentServer": exit_server["name"] if exit_server else None,
        "currentProtocol": proto,
        "connectedSince": None,
    })
    save_state(state)

    ok, error, auth_failed = activate(password, args.timeout)
    if not ok:
        # A failed attempt would otherwise keep retrying and hold the route.
        nmcli(["connection", "down", "id", PROFILE], timeout=30)
        forget_cached_secret()
        state = load_state()
        state.update({"current": None, "currentVia": None, "currentServer": None, "connectedSince": None})
        save_state(state)
        if args.via and not auth_failed and "timed out" in (error or ""):
            error = "Multihop did not answer. It needs the Multihop add-on on your OVPN account."
        emit({"ok": False, "error": error, "authFailed": auth_failed})
        sys.exit(1)

    forget_cached_secret()
    state = load_state()
    state.update({"last": dc["slug"], "lastVia": args.via or None, "connectedSince": int(time.time())})
    save_state(state)
    emit(status_payload())


def cmd_disconnect(args):
    if active_details() is not None:
        proc = nmcli(["connection", "down", "id", PROFILE], timeout=30)
        if proc.returncode != 0:
            raise HelperError(proc.stderr.strip() or "could not disconnect")
    state = load_state()
    state.update({"current": None, "currentVia": None, "currentServer": None, "connectedSince": None})
    save_state(state)
    emit(status_payload())


def cmd_protocol(args):
    state = load_state()
    state["protocol"] = args.proto
    save_state(state)
    emit({"ok": True, "preferredProtocol": args.proto})


def cmd_multihop(args):
    state = load_state()
    if args.entry == "off":
        state["via"] = None
    else:
        entry, _ = fetch_entry()
        state["via"] = find_datacenter(entry, args.entry)["slug"]
    save_state(state)
    emit({"ok": True, "preferredVia": state["via"]})


def cmd_favorite(args):
    state = load_state()
    favorites = [f for f in (state.get("favorites") or []) if f != args.location]
    if args.on:
        favorites.append(args.location)
    state["favorites"] = favorites
    save_state(state)
    emit({"ok": True, "favorites": favorites})


def cmd_uninstall(args):
    """Remove everything the plugin created outside its own folder."""
    removed = []
    if active_details() is not None:
        nmcli(["connection", "down", "id", PROFILE], timeout=30)
    if profile_exists() and nmcli(["connection", "delete", "id", PROFILE]).returncode == 0:
        removed.append("NetworkManager profile '%s'" % PROFILE)
    username = load_state().get("username")
    if username and keyring_lookup(username):
        keyring_clear(username)
        removed.append("keyring entry for %s" % username)
    for path in (STATE_DIR, CACHE_DIR):
        if os.path.isdir(path):
            shutil.rmtree(path)
            removed.append(path)
    emit({"ok": True, "removed": removed})


def cmd_watch(args):
    """Print status on each NetworkManager change, coalescing bursts."""
    import select

    last = None

    def push():
        nonlocal last
        payload = status_payload()
        key = json.dumps(payload, sort_keys=True)
        if key != last:
            last = key
            emit(payload)

    push()
    mon = subprocess.Popen(["nmcli", "monitor"], stdout=subprocess.PIPE, text=True, bufsize=1)
    try:
        while True:
            ready, _, _ = select.select([mon.stdout], [], [], args.heartbeat)
            if ready:
                if not mon.stdout.readline():
                    break
                while select.select([mon.stdout], [], [], 0.3)[0]:
                    if not mon.stdout.readline():
                        break
            push()
    except KeyboardInterrupt:
        pass
    finally:
        mon.terminate()


def main():
    parser = argparse.ArgumentParser(prog="ovpnctl", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("servers", help="list locations")
    p.add_argument("--refresh", action="store_true", help="ignore the cache")
    p.set_defaults(func=cmd_servers)

    sub.add_parser("ping", help="latency to every location").set_defaults(func=cmd_ping)

    p = sub.add_parser("status", help="connection state")
    p.add_argument("--ip", action="store_true", help="also look up the public IP")
    p.set_defaults(func=cmd_status)

    sub.add_parser("login", help="store credentials (JSON on stdin)").set_defaults(func=cmd_login)
    sub.add_parser("logout", help="forget credentials").set_defaults(func=cmd_logout)

    p = sub.add_parser("connect", help="connect to a location")
    p.add_argument("location")
    p.add_argument("--proto", choices=["udp", "tcp"], help="default: the saved preference")
    p.add_argument("--via", metavar="ENTRY", help="multihop: enter at this location, exit at LOCATION")
    p.add_argument("--timeout", type=int, default=60)
    p.set_defaults(func=cmd_connect)

    sub.add_parser("disconnect", help="disconnect").set_defaults(func=cmd_disconnect)
    sub.add_parser(
        "uninstall", help="remove the NM profile, keyring entry, state and cache"
    ).set_defaults(func=cmd_uninstall)

    p = sub.add_parser("protocol", help="save the preferred protocol")
    p.add_argument("proto", choices=["udp", "tcp"])
    p.set_defaults(func=cmd_protocol)

    p = sub.add_parser("multihop", help="save the multihop entry location, or 'off'")
    p.add_argument("entry")
    p.set_defaults(func=cmd_multihop)

    p = sub.add_parser("favorite", help="add or remove a favorite")
    p.add_argument("location")
    p.add_argument("--off", dest="on", action="store_false")
    p.set_defaults(func=cmd_favorite)

    p = sub.add_parser("watch", help="stream status changes")
    p.add_argument("--heartbeat", type=float, default=30.0)
    p.set_defaults(func=cmd_watch)

    args = parser.parse_args()
    try:
        args.func(args)
    except HelperError as e:
        fail(str(e))


if __name__ == "__main__":
    main()
