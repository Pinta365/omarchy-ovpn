# OVPN for the Omarchy shell

A bar widget and panel for [OVPN](https://www.ovpn.com/) that works like the
official desktop client: one-click connect, a location list with ping, load
and favorites, UDP or TCP 443, a drop notification and auto-connect.

NetworkManager does the tunnelling (`networkmanager-openvpn`), so there is no
daemon and nothing runs as root.

## Install

```bash
omarchy plugin add https://github.com/Pinta365/omarchy-ovpn.git --enable
```

Requirements: `networkmanager-openvpn`, which a stock Omarchy install does not
include. Everything else it uses (NetworkManager, `gnome-keyring`, `libsecret`,
`python-gobject`, `iputils`) ships with Omarchy.

## Remove

```bash
~/.config/omarchy/plugins/pinta365.ovpn/helper/ovpnctl.py uninstall
omarchy plugin remove pinta365.ovpn
```

The first command disconnects and deletes what the plugin created outside its
folder: the `OVPN (Omarchy)` NetworkManager profile, the remembered password
in your keyring, and `~/.local/state/ovpn-omarchy` and `~/.cache/ovpn-omarchy`.
Your own NetworkManager connections are never touched.

## Using it

- Click the shield to open the panel; middle-click connects to the last
  location or disconnects.
- Multihop: switch it on, pick the entry location, then connect to the exit
  location as usual. Traffic enters OVPN at the entry and leaves from the exit
  (UDP only: the multihop ports do not accept TCP).
- Right-click a location or press `f` to star it. `/` searches, `t` flips the
  protocol, `c` connects, `d` disconnects, `r` refreshes.
- IPC: `qs ipc -p $OMARCHY_PATH/shell call ovpn <open|close|toggle|connect|connectTo <slug>|disconnect|quickToggle|multihop <entry|off>|status>`

## Credentials

Your OVPN username is kept in `~/.local/state/ovpn-omarchy/state.json`. The
password is:

- held in memory for the session by default, or
- stored in the Secret Service keyring (gnome-keyring) when you tick
  *Remember password*.

It never appears on a command line, in the plugin directory, or in
`shell.json`. It reaches NetworkManager over a pipe at each connect, the
managed profile `OVPN (Omarchy)` is created with `password-flags=2` (never
saved), and the copy NetworkManager keeps in memory after activation is
cleared as soon as the tunnel is up.

## Helper

`helper/ovpnctl.py` is the whole backend and works on its own:

```bash
helper/ovpnctl.py servers            # locations (cached for an hour)
helper/ovpnctl.py ping               # latency per location
helper/ovpnctl.py status --ip        # state and public IP
echo '{"username":"me","password":"…","remember":false}' | helper/ovpnctl.py login
echo '{"password":"…"}' | helper/ovpnctl.py connect sthlm --proto tcp
echo '{"password":"…"}' | helper/ovpnctl.py connect oslo --via sthlm   # multihop
helper/ovpnctl.py disconnect
helper/ovpnctl.py watch              # one JSON line per state change
```

## Development

Develop in a checkout, copy it into `~/.config/omarchy/plugins/pinta365.ovpn`
and run `omarchy restart shell`. The shell's in-place plugin reload does not
follow a symlinked plugin directory and keeps running the old compiled QML,
so a restart is the reliable way to see a change.

## Not yet

WireGuard and the kill switch are planned.
