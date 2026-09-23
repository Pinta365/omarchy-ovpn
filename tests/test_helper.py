"""Tests for the parts of ovpnctl.py that decide what gets connected.

Pure functions only: nothing here touches NetworkManager, the keyring, the
network or the real state directory.

Run with: /usr/bin/python3 -m unittest discover -s tests
"""

import atexit
import os
import re
import shutil
import sys
import tempfile
import unittest

# Point the helper at a sandbox before importing it: its state and cache paths
# are read at import time.
_SANDBOX = tempfile.mkdtemp(prefix="ovpnctl-tests.")
os.environ["XDG_STATE_HOME"] = os.path.join(_SANDBOX, "state")
os.environ["XDG_CACHE_HOME"] = os.path.join(_SANDBOX, "cache")
atexit.register(shutil.rmtree, _SANDBOX, True)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "helper"))
import ovpnctl as o  # noqa: E402


def server(name, ip, load=10, online=True, **extra):
    data = {
        "name": name, "ip": ip, "load": load, "online": online,
        "public_key": "pub-" + name, "wireguard_ports": [9929],
    }
    data.update(extra)
    return data


def datacenter(slug, servers, city=None):
    return {
        "slug": slug, "city": city or slug.title(), "country": "SE",
        "country_name": "Sweden", "pools": ["pool-1.prd.se.%s.ovpn.com" % slug],
        "ping_address": "10.0.0.1", "servers": servers,
    }


class ProfileData(unittest.TestCase):
    """vpn.data is parsed by NetworkManager, and one wrong key silently
    weakens the tunnel."""

    def data(self, proto="udp", **kw):
        """vpn.data as NetworkManager parses it: items are separated by an
        unescaped comma, so a value carrying "\\," stays one item."""
        dc = datacenter("malmo", [server("VPN01", "1.2.3.4")])
        raw = o.vpn_data(dc, proto, "user", **kw)
        items = re.split(r"(?<!\\), ", raw)
        return dict(item.split(" = ", 1) for item in items)

    def test_server_certificates_are_required(self):
        self.assertEqual(self.data()["remote-cert-tls"], "server")

    def test_password_is_never_stored_by_networkmanager(self):
        self.assertEqual(self.data()["password-flags"], "2")

    def test_udp_uses_both_ports_and_tcp_only_443(self):
        self.assertEqual(self.data()["remote"].count("1194"), 1)
        self.assertEqual(self.data()["remote"].count("1195"), 1)
        self.assertNotIn("proto-tcp", self.data())

        tcp = self.data("tcp")
        self.assertIn(":443:tcp-client", tcp["remote"])
        self.assertEqual(tcp["proto-tcp"], "yes")

    def test_values_cannot_inject_another_key(self):
        # A hostile server list must not be able to add vpn.data keys.
        self.assertEqual(o.nm_escape("a,b"), "a\\,b")
        self.assertEqual(o.nm_escape("a\\b"), "a\\\\b")
        injected = self.data(remotes_override=["evil:1194, password-flags = 0"])
        self.assertEqual(injected["password-flags"], "2")


class Multihop(unittest.TestCase):
    """Entry server, exit server's port: getting this backwards would route
    traffic through the wrong country."""

    def setUp(self):
        self.entry = datacenter("sthlm", [server("VPN02", "5.6.7.8", multihop_openvpn_port=20002,
                                                 multihop_wireguard_port=30002)])
        self.exit = datacenter("oslo", [server("VPN39", "9.9.9.9", multihop_openvpn_port=20039,
                                               multihop_wireguard_port=30039)])

    def test_openvpn_uses_entry_ip_with_exit_port(self):
        remotes, exit_server = o.pick_multihop(self.entry, self.exit)
        self.assertEqual(remotes, ["5.6.7.8:20039"])
        self.assertEqual(exit_server["name"], "VPN39")

    def test_wireguard_uses_entry_ip_with_exit_port_and_exit_key(self):
        endpoint, peer_key, exit_server = o.wg_peer(self.exit, self.entry)
        self.assertEqual(endpoint, "5.6.7.8:30039")
        self.assertEqual(peer_key, "pub-VPN39")
        self.assertEqual(exit_server["name"], "VPN39")

    def test_direct_wireguard_uses_the_exit_server_itself(self):
        endpoint, peer_key, _ = o.wg_peer(self.exit)
        self.assertEqual(endpoint, "9.9.9.9:9929")
        self.assertEqual(peer_key, "pub-VPN39")

    def test_entry_and_exit_must_differ(self):
        for call in (lambda: o.pick_multihop(self.exit, self.exit),
                     lambda: o.wg_peer(self.exit, self.exit)):
            with self.assertRaises(o.HelperError):
                call()

    def test_servers_without_a_multihop_port_are_skipped(self):
        mixed = datacenter("oslo", [
            server("VPN40", "9.9.9.10", load=1),  # least loaded, but no port
            server("VPN39", "9.9.9.9", load=50, multihop_wireguard_port=30039),
        ])
        endpoint, peer_key, _ = o.wg_peer(mixed, self.entry)
        self.assertEqual(endpoint, "5.6.7.8:30039")
        self.assertEqual(peer_key, "pub-VPN39")


class ServerList(unittest.TestCase):
    def test_a_null_load_does_not_crash_the_list(self):
        dc = datacenter("malmo", [server("VPN01", "1.2.3.4", load=None),
                                  server("VPN02", "1.2.3.5", load=30)])
        self.assertEqual(o.summarize(dc)["load"], 15)
        self.assertEqual(o.online_servers(dc)[0]["name"], "VPN02")

    def test_offline_servers_are_counted_but_not_offered(self):
        dc = datacenter("malmo", [server("VPN01", "1.2.3.4"),
                                  server("VPN02", "1.2.3.5", online=False)])
        summary = o.summarize(dc)
        self.assertEqual((summary["servers"], summary["online"]), (2, 1))
        self.assertEqual([s["name"] for s in o.online_servers(dc)], ["VPN01"])

    def test_unknown_location_is_an_error(self):
        with self.assertRaises(o.HelperError):
            o.find_datacenter({"datacenters": []}, "nowhere")


class ConnectionState(unittest.TestCase):
    """What the panel believes about the tunnel comes from here."""

    def test_openvpn_is_connected_only_at_vpn_state_5(self):
        self.assertEqual(o.vpn_state({"GENERAL.STATE": "activated",
                                      "VPN.VPN-STATE": "5 - VPN connected"}), "connected")
        self.assertEqual(o.vpn_state({"GENERAL.STATE": "activated",
                                      "VPN.VPN-STATE": "3 - VPN connecting"}), "connecting")

    def test_wireguard_is_a_device_and_has_no_vpn_state(self):
        self.assertEqual(o.vpn_state({"GENERAL.STATE": "activated",
                                      "GENERAL.VPN": "no"}), "connected")

    def test_nothing_active_is_off(self):
        self.assertEqual(o.vpn_state(None), "off")
        self.assertEqual(o.vpn_state({"GENERAL.STATE": "deactivating"}), "disconnecting")


class State(unittest.TestCase):
    def test_updates_apply_to_the_newest_state_on_disk(self):
        o.update_state(username="a", favorites=["malmo"])
        o.update_state(username="b")
        state = o.load_state()
        self.assertEqual(state["username"], "b")
        self.assertEqual(state["favorites"], ["malmo"])

    def test_a_broken_state_file_reads_as_empty(self):
        os.makedirs(o.STATE_DIR, exist_ok=True)
        with open(o.STATE_FILE, "w") as f:
            f.write("{ not json")
        self.assertEqual(o.load_state(), {})


if __name__ == "__main__":
    unittest.main()
