#!/usr/bin/python3
"""OVPN control helper for the Omarchy shell plugin.

Each command prints one JSON object (``watch`` one per line). Secrets are read
from stdin or the keyring, never from arguments.
"""

import argparse
import contextlib
import json
import os
import fcntl
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

API = "https://www.ovpn.com/v2/api/client"
KEYS_API = "https://www.ovpn.com/v4/api/keys"
PROFILE = "OVPN (Omarchy)"
PROFILE_WG = "OVPN (Omarchy) WG"
WG_IFNAME = "ovpn-wg"
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
WG_SCHEMA_NAME = "se.ovpn.omarchy.wgkey"

# WireGuard: OVPN's resolvers, and the MTU their own client configures.
WG_DNS = "46.227.67.134, 192.165.9.158"
WG_MTU = 1320

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


@contextlib.contextmanager
def state_lock():
    """Serialise read-modify-write of state.json across concurrent helpers."""
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, "state.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def try_update_state(**changes):
    """Best effort, never blocks. For signal handlers: taking a lock the
    interrupted thread may hold would deadlock."""
    try:
        with open(os.path.join(STATE_DIR, "state.lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            state = load_state()
            state.update(changes)
            save_state(state)
            fcntl.flock(lock, fcntl.LOCK_UN)
    except (OSError, ValueError):
        pass


def update_state(**changes):
    """Apply changes to the newest state on disk."""
    with state_lock():
        state = load_state()
        state.update(changes)
        save_state(state)
        return state


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
    load = round(sum(s.get("load") or 0 for s in online) / len(online)) if online else None
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


_KEYRING_CACHE = {}


def keyring_lookup(username, max_age=0):
    """Cached briefly: a lookup on a locked collection blocks and can prompt,
    and `watch` asks on every event."""
    if not username:
        return None
    hit = _KEYRING_CACHE.get(username)
    if hit and max_age and time.time() - hit[0] < max_age:
        return hit[1]
    try:
        Secret, schema = _secret()
        password = Secret.password_lookup_sync(schema, {"username": username}, None)
    except Exception:
        password = None
    _KEYRING_CACHE[username] = (time.time(), password)
    return password


def keyring_store(username, password):
    _KEYRING_CACHE.pop(username, None)
    Secret, schema = _secret()
    ok = Secret.password_store_sync(
        schema, {"username": username}, Secret.COLLECTION_DEFAULT, "OVPN (%s)" % username, password, None
    )
    if not ok:
        raise HelperError("could not store the password in the keyring")


def keyring_clear(username):
    _KEYRING_CACHE.pop(username, None)
    try:
        Secret, schema = _secret()
        Secret.password_clear_sync(schema, {"username": username}, None)
    except Exception:
        pass


def _wg_schema():
    Secret, _ = _secret()
    return Secret, Secret.Schema.new(
        WG_SCHEMA_NAME, Secret.SchemaFlags.NONE, {"username": Secret.SchemaAttributeType.STRING}
    )


def wg_key_lookup(username):
    try:
        Secret, schema = _wg_schema()
        return Secret.password_lookup_sync(schema, {"username": username}, None)
    except Exception:
        return None


def wg_key_store(username, private_key):
    Secret, schema = _wg_schema()
    Secret.password_store_sync(
        schema, {"username": username}, Secret.COLLECTION_DEFAULT,
        "OVPN WireGuard key (%s)" % username, private_key, None,
    )


def wg_key_forget(username):
    try:
        Secret, schema = _wg_schema()
        Secret.password_clear_sync(schema, {"username": username}, None)
    except Exception:
        pass


def generate_keypair():
    """X25519 via openssl: the last 32 bytes of each DER blob are the key."""
    import base64

    try:
        gen = subprocess.run(
            ["openssl", "genpkey", "-algorithm", "X25519", "-outform", "DER"],
            capture_output=True, timeout=10,
        )
        pub = subprocess.run(
            ["openssl", "pkey", "-inform", "DER", "-pubout", "-outform", "DER"],
            input=gen.stdout, capture_output=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HelperError("could not run openssl to make a WireGuard key: %s" % e)
    der, pub = gen.stdout, pub.stdout if pub.returncode == 0 else b""
    if gen.returncode != 0 or len(der) < 32 or len(pub) < 32:
        raise HelperError("could not generate a WireGuard key")
    return base64.b64encode(der[-32:]).decode(), base64.b64encode(pub[-32:]).decode()


def keys_api(method, fields, timeout=20):
    """Form-encoded call to the WireGuard key API. Returns (status, data)."""
    import urllib.parse

    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        KEYS_API, data=body, method=method,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, TimeoutError) as e:
        raise HelperError("could not reach OVPN: %s" % e)


def wg_register(username, password):
    """Register a fresh key. Returns the stored record."""
    private_key, public_key = generate_keypair()
    status, data = keys_api("POST", {
        "username": username, "password": password, "new_key": public_key,
    })
    if status == 423:
        raise HelperError("OVPN is rate-limiting key changes. Try again in a minute.")
    if status in (401, 403):
        update_state(passwordRejected=True)
        fail("OVPN rejected the username or password", authFailed=True)
    if status != 201:
        errors = data.get("errors") or {}
        detail = "; ".join(v[0] for v in errors.values() if v) if errors else ""
        message = detail or (data.get("error") or {}).get("message") or "could not register the key"
        if "more than" in message:
            message += " Remove one on ovpn.com/account/wireguard/keys."
        raise HelperError(message)

    key = data.get("data") or {}
    record = {
        "wgUser": username,
        "wgPublic": key.get("key") or public_key,
        "wgId": key.get("id"),
        "wgIpv4": key.get("ipv4"),
        "wgIpv6": key.get("ipv6"),
    }
    wg_key_store(username, private_key)
    update_state(**record)
    return dict(record, private=private_key)


def wg_ensure_key(username, password):
    """The account's key, registering one only when there is none to reuse."""
    state = load_state()
    private_key = wg_key_lookup(username)
    if (private_key and state.get("wgUser") == username
            and state.get("wgPublic") and state.get("wgIpv4")):
        return {
            "wgPublic": state["wgPublic"], "wgIpv4": state["wgIpv4"],
            "wgIpv6": state.get("wgIpv6"), "private": private_key,
        }
    return wg_register(username, password)


def wg_forget_key(username, password=None):
    """Drop our key locally. Returns True when OVPN also dropped its copy;
    without a password we cannot ask, and the key stays on the account."""
    state = load_state()
    public_key = state.get("wgPublic")
    removed = False
    if public_key and password and state.get("wgUser"):
        try:
            status, _ = keys_api("DELETE", {
                "username": state["wgUser"], "password": password, "key": public_key,
            }, timeout=10)
            # 404: already gone, which is the state we wanted.
            removed = status in (200, 204, 404)
        except HelperError:
            removed = False
    wg_key_forget(state.get("wgUser") or username or "")
    update_state(wgUser=None, wgPublic=None, wgId=None, wgIpv4=None, wgIpv6=None)
    return removed


def tunnel_carries_traffic(timeout=8):
    """Ask OVPN who we are. Anything but an answer means the tunnel is dead."""
    try:
        data = http_json(API + "/ptr", timeout=timeout)
    except HelperError:
        return False
    return bool(data.get("success")) or bool(IP_RE.search(json.dumps(data)))


def wg_peer(dc, entry_dc=None):
    """(endpoint, peer public key). Multihop enters at one datacenter and
    leaves at another, so the endpoint is the entry server on the exit
    server's multihop port while the peer key stays the exit server's."""
    exits = [s for s in online_servers(dc) if s.get("public_key")]
    if not exits:
        raise HelperError("no WireGuard server in %s" % dc.get("city"))
    exit_server = exits[0]
    if entry_dc is None:
        ports = exit_server.get("wireguard_ports") or []
        if not ports:
            raise HelperError("no WireGuard port for %s" % dc.get("city"))
        return "%s:%d" % (exit_server["ip"], int(ports[0])), exit_server["public_key"], exit_server
    if entry_dc.get("slug") == dc.get("slug"):
        raise HelperError("entry and exit must be different locations")
    hops = [s for s in exits if s.get("multihop_wireguard_port")]
    entries = online_servers(entry_dc)
    if not hops or not entries:
        raise HelperError("multihop is not available for that pair")
    exit_server = hops[0]
    return ("%s:%d" % (entries[0]["ip"], int(exit_server["multihop_wireguard_port"])),
            exit_server["public_key"], exit_server)


def write_wg_profile(dc, key, entry_dc=None):
    """Import a fresh profile. The config carries the private key, so it is
    written 0600 in the runtime dir and removed once NetworkManager has it."""
    endpoint, peer_key, exit_server = wg_peer(dc, entry_dc)
    addresses = ", ".join(a for a in (key.get("wgIpv4"), key.get("wgIpv6")) if a)
    conf = "\n".join([
        "[Interface]",
        "PrivateKey = %s" % key["private"],
        "Address = %s" % addresses,
        "DNS = %s" % WG_DNS,
        "MTU = %d" % WG_MTU,
        "",
        "[Peer]",
        "PublicKey = %s" % peer_key,
        "AllowedIPs = 0.0.0.0/0, ::/0",
        "Endpoint = %s" % endpoint,
        "",
    ])

    # The file holds the private key until NetworkManager has imported it, so
    # it lives in a private directory: a fixed path in a shared one could be
    # pre-created or symlinked by another local user. The name inside sets the
    # interface name, which cannot exceed 15 characters.
    runtime = os.environ.get("XDG_RUNTIME_DIR") or STATE_DIR
    os.makedirs(runtime, exist_ok=True)
    old_umask = os.umask(0o077)
    workdir = tempfile.mkdtemp(dir=runtime, prefix="ovpn-wg.")
    path = os.path.join(workdir, "%s.conf" % WG_IFNAME)
    try:
        with open(path, "w") as f:
            f.write(conf)
        if wg_profile_exists():
            nmcli(["connection", "delete", PROFILE_WG], timeout=30)
        nmcli(["connection", "delete", WG_IFNAME], timeout=30)
        proc = nmcli(["connection", "import", "type", "wireguard", "file", path], timeout=30)
        if proc.returncode != 0:
            raise HelperError("could not import the WireGuard profile: " + proc.stderr.strip())
    finally:
        os.umask(old_umask)
        shutil.rmtree(workdir, ignore_errors=True)

    proc = nmcli(["connection", "modify", WG_IFNAME,
                  "connection.id", PROFILE_WG, "connection.autoconnect", "no",
                  "ipv4.dns-priority", "-50", "ipv6.dns-priority", "-50"], timeout=30)
    if proc.returncode != 0:
        raise HelperError("could not configure the WireGuard profile: " + proc.stderr.strip())
    return exit_server


def wg_profile_exists():
    return nmcli(["-g", "connection.id", "connection", "show", PROFILE_WG]).returncode == 0


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
    return sorted(servers, key=lambda s: s.get("load") if s.get("load") is not None else 100)


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
        # Without this, any cert chaining to OVPN's CA is accepted.
        "remote-cert-tls": "server",
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
        # Wholesale, so switching to UDP drops proto-tcp.
        proc = nmcli(["connection", "modify", PROFILE, "vpn.data", data])
    else:
        proc = nmcli([
            "connection", "add", "type", "vpn", "con-name", PROFILE, "vpn-type", "openvpn",
            "vpn.data", data, "connection.autoconnect", "no",
        ])
    if proc.returncode != 0:
        raise HelperError("could not write the NetworkManager profile: " + proc.stderr.strip())


def _watch_auth_failed(journal, flag):
    """Read the journal on a thread: lines sitting in Python's buffer are
    invisible to select() on the underlying fd."""
    try:
        for line in journal.stdout:
            # The full message: a bare token could appear in any log line.
            if "AUTH: Received control message: AUTH_FAILED" in line:
                flag.set()
                return
    except (OSError, ValueError):
        pass


def activate(password, timeout):
    """Returns (ok, error, authFailed). OpenVPN retries a rejected password
    forever, so watch the journal for AUTH_FAILED instead of waiting it out."""
    import threading

    journal = None
    auth_failed = threading.Event()
    reader = None
    up = None
    try:
        try:
            journal = subprocess.Popen(
                ["journalctl", "-f", "-n", "0", "-o", "cat", "SYSLOG_IDENTIFIER=nm-openvpn"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
                preexec_fn=die_with_parent,
            )
            reader = threading.Thread(target=_watch_auth_failed, args=(journal, auth_failed), daemon=True)
            reader.start()
        except OSError:
            journal = None

        try:
            up = subprocess.Popen(
                ["nmcli", "--wait", str(timeout), "connection", "up", "id", PROFILE,
                 "passwd-file", "/dev/stdin"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            up.stdin.write("vpn.secrets.password:%s\n" % password)
            up.stdin.close()
        except (OSError, ValueError) as e:
            return False, "could not run nmcli: %s" % e, False

        # Inside the caller's timeout, so cleanup still runs.
        deadline = time.time() + timeout + 5
        while up.poll() is None and time.time() < deadline:
            if auth_failed.is_set():
                break
            time.sleep(0.1)
    finally:
        if journal is not None:
            journal.terminate()

    if auth_failed.is_set():
        up.terminate()
        return False, "OVPN rejected the username or password", True
    if up.poll() is None:
        up.terminate()
        return False, "timed out while connecting", False

    stdout, stderr = up.communicate()
    if up.returncode != 0:
        # nmcli's last line is usually a journalctl hint; the error is above it.
        lines = [l.strip() for l in (stderr or stdout).strip().splitlines() if l.strip()]
        errors = [l for l in lines if l.startswith("Error:")] or lines
        message = errors[0].replace("Error: ", "") if errors else "activation failed"
        return False, message, False
    return True, None, False


def forget_cached_secret():
    # NM keeps the activation's secret in memory even with password-flags=2.
    nmcli(["connection", "modify", PROFILE, "vpn.secrets", ""])


def active_details(profile=PROFILE):
    """Active profile fields, or None. VPN.VPN-STATE is only in the full -t listing."""
    proc = nmcli(["-t", "connection", "show", "--active", profile])
    if proc.returncode != 0:
        return None
    fields = {}
    for line in proc.stdout.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields.setdefault(key, value)
    # An inactive profile exits 0 with no output.
    return fields or None


def vpn_state(details):
    if details is None:
        return "off"
    state = details.get("GENERAL.STATE", "")
    if state == "deactivating":
        return "disconnecting"
    # WireGuard is a device, not a VPN service: no VPN-STATE, GENERAL.VPN "no".
    if details.get("GENERAL.VPN") == "no":
        return "connected" if state == "activated" else "connecting"
    # NMVpnConnectionState 5 = activated.
    if details.get("VPN.VPN-STATE", "").startswith("5"):
        return "connected"
    if state in ("activating", "activated"):
        return "connecting"
    return "off"


def live_details():
    """Whichever of the two profiles is up."""
    return active_details() or active_details(PROFILE_WG)


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
    details = live_details()
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
        # A rejected password stays in the keyring but is not offered.
        "hasPassword": (bool(keyring_lookup(state.get("username"), max_age=30))
                        and not state.get("passwordRejected")),
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


def cmd_logout(args):
    state = load_state()
    if state.get("wgPublic"):
        wg_forget_key(state.get("username"), keyring_lookup(state.get("username")))
    # The profile stores the private key, so it goes with the account.
    if wg_profile_exists():
        nmcli(["connection", "down", "id", PROFILE_WG], timeout=20)
        nmcli(["connection", "delete", "id", PROFILE_WG], timeout=20)
    with state_lock():
        state = load_state()
        username = state.pop("username", None)
        state.pop("passwordRejected", None)
        save_state(state)
    if username:
        keyring_clear(username)
    emit({"ok": True})


def cmd_connect(args):

    # Credentials may arrive on stdin for an account not saved yet; only a
    # successful connection commits them.
    data = read_stdin_json()
    state = load_state()
    offered = str(data.get("username") or "").strip()
    username = offered or state.get("username")
    if not username:
        fail("not signed in", needsLogin=True)

    password = data.get("password") or keyring_lookup(username)
    if not password:
        emit({"ok": False, "error": "password required", "needsPassword": True})
        sys.exit(1)
    remember = bool(data.get("remember")) and bool(data.get("password"))

    proto = args.proto or state.get("protocol") or "udp"
    if proto != "wg":
        if not os.path.isfile(NM_OPENVPN_MARKER):
            fail("networkmanager-openvpn is not installed", missingOpenvpn=True)
        if not (os.path.isfile(CA_FILE) and os.path.isfile(TA_FILE)):
            raise HelperError("CA or tls-auth file missing from %s" % ASSETS)
    entry, _ = fetch_entry()
    dc = find_datacenter(entry, args.location)
    remotes = None
    exit_server = None
    # One tunnel at a time: the other profile would keep its own default route.
    other = PROFILE if proto == "wg" else PROFILE_WG
    if active_details(other) is not None:
        nmcli(["connection", "down", "id", other], timeout=30)

    if proto == "wg":
        key = wg_ensure_key(username, password)
        exit_server = write_wg_profile(dc, key, find_datacenter(entry, args.via) if args.via else None)
    else:
        if args.via:
            remotes, exit_server = pick_multihop(find_datacenter(entry, args.via), dc)
            proto = "udp"
        write_profile(dc, proto, username, remotes)

    update_state(
        current=dc["slug"],
        currentVia=args.via or None,
        currentServer=exit_server["name"] if exit_server else None,
        currentProtocol=proto,
        connectedSince=None,
    )

    # On SIGTERM from the caller's `timeout`: tear down, clear the secret NM
    # holds, and stay short. Runs in the main thread, so it never blocks on the
    # state lock.
    def on_term(signum, frame):
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        nmcli(["connection", "down", "id", PROFILE_WG if proto == "wg" else PROFILE], timeout=8)
        if proto != "wg":
            nmcli(["connection", "modify", PROFILE, "vpn.secrets", ""], timeout=8)
        try_update_state(current=None, currentVia=None, currentServer=None, connectedSince=None)
        emit({"ok": False, "error": "connecting was cut short"})
        os._exit(1)

    signal.signal(signal.SIGTERM, on_term)

    if proto == "wg":
        # The key registration already proved the account, and WireGuard has
        # no auth step of its own.
        proc = nmcli(["--wait", str(args.timeout), "connection", "up", PROFILE_WG],
                     timeout=args.timeout + 10)
        ok = proc.returncode == 0
        auth_failed = False
        error = None
        if not ok:
            lines = [l.strip() for l in (proc.stderr or proc.stdout).strip().splitlines() if l.strip()]
            errors = [l for l in lines if l.startswith("Error:")] or lines
            error = errors[0].replace("Error: ", "") if errors else "activation failed"
        elif not tunnel_carries_traffic():
            # WireGuard has no handshake to fail: a revoked or unknown key
            # activates fine and then blackholes everything. Drop the key so
            # the next attempt registers a fresh one.
            ok = False
            error = "OVPN did not answer over WireGuard. Connect again to register a new key."
            wg_key_forget(username)
            update_state(wgUser=None, wgPublic=None, wgId=None, wgIpv4=None, wgIpv6=None)
    else:
        ok, error, auth_failed = activate(password, args.timeout)
    # The tunnel is decided; a teardown would be wrong from here.
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    if not ok:
        # Otherwise it keeps retrying and holds the default route.
        nmcli(["connection", "down", "id", PROFILE if proto != "wg" else PROFILE_WG], timeout=30)
        forget_cached_secret()
        update_state(current=None, currentVia=None, currentServer=None, connectedSince=None)
        if auth_failed:
            # Flagged, not deleted: the rejection may be temporary, and the
            # panel asks instead of retrying the same password.
            update_state(passwordRejected=True)
        if args.via and not auth_failed and "timed out" in (error or ""):
            error = "Multihop did not answer. It needs the Multihop add-on on your OVPN account."
        emit({"ok": False, "error": error, "authFailed": auth_failed})
        sys.exit(1)

    forget_cached_secret()
    # Proven, so the account may be saved.
    if remember:
        keyring_store(username, str(password))
    # Re-read under the lock: the snapshot above is up to a minute old.
    with state_lock():
        fresh = load_state()
        previous = fresh.get("username")
        fresh.update({"username": username, "last": dc["slug"],
                      "lastVia": args.via or None, "passwordRejected": False,
                      "connectedSince": int(time.time())})
        save_state(fresh)
    if previous and previous != username:
        keyring_clear(previous)
    emit(status_payload())


def cmd_disconnect(args):
    # Unconditional: a connect NetworkManager has not registered yet is
    # invisible to active_details(). A non-zero exit only matters if the
    # profile is still up afterwards.
    failures = []
    for name in (PROFILE, PROFILE_WG):
        proc = nmcli(["connection", "down", "id", name], timeout=30)
        if proc.returncode != 0 and active_details(name) is not None:
            failures.append(proc.stderr.strip() or "could not disconnect %s" % name)
    update_state(current=None, currentVia=None, currentServer=None, connectedSince=None)
    if failures:
        raise HelperError(failures[0])
    emit(status_payload())


def cmd_protocol(args):
    update_state(protocol=args.proto)
    emit({"ok": True, "preferredProtocol": args.proto})


def cmd_multihop(args):
    via = None
    if args.entry != "off":
        entry, _ = fetch_entry()
        via = find_datacenter(entry, args.entry)["slug"]
    update_state(via=via)
    emit({"ok": True, "preferredVia": via})


def cmd_favorite(args):
    with state_lock():
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
    state = load_state()
    username = state.get("username")
    if state.get("wgPublic"):
        if wg_forget_key(username, keyring_lookup(username)):
            removed.append("WireGuard key (also from your OVPN account)")
        else:
            removed.append("WireGuard key locally; remove it on ovpn.com/account/wireguard/keys")
    for name in (PROFILE, PROFILE_WG):
        if active_details(name) is not None:
            nmcli(["connection", "down", "id", name], timeout=30)
        if nmcli(["connection", "delete", "id", name]).returncode == 0:
            removed.append("NetworkManager profile '%s'" % name)
    if username and keyring_lookup(username):
        keyring_clear(username)
        removed.append("keyring entry for %s" % username)
    for path in (STATE_DIR, CACHE_DIR):
        if os.path.isdir(path):
            shutil.rmtree(path)
            removed.append(path)
    emit({"ok": True, "removed": removed})


def die_with_parent():
    """The shell kills helpers without warning; a child must not outlive one."""
    try:
        import ctypes

        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except Exception:
        pass


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
    mon = subprocess.Popen(["nmcli", "monitor"], stdout=subprocess.PIPE, text=True, bufsize=1,
                           preexec_fn=die_with_parent)
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

    sub.add_parser("logout", help="forget credentials").set_defaults(func=cmd_logout)

    p = sub.add_parser("connect", help="connect to a location")
    p.add_argument("location")
    p.add_argument("--proto", choices=["udp", "tcp", "wg"], help="default: the saved preference")
    p.add_argument("--via", metavar="ENTRY", help="multihop: enter at this location, exit at LOCATION")
    p.add_argument("--timeout", type=int, default=60)
    p.set_defaults(func=cmd_connect)

    sub.add_parser("disconnect", help="disconnect").set_defaults(func=cmd_disconnect)
    sub.add_parser(
        "uninstall", help="remove the NM profile, keyring entry, state and cache"
    ).set_defaults(func=cmd_uninstall)

    p = sub.add_parser("protocol", help="save the preferred protocol")
    p.add_argument("proto", choices=["udp", "tcp", "wg"])
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
