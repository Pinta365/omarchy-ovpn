#!/usr/bin/python3
"""Manage existing NetworkManager OpenVPN profiles, independently of OVPN.com.

Credentials, when required, arrive as one JSON line on stdin. No account,
keyring or provider API is used; imported routes and other tunnels are preserved.
"""
import argparse
import json
import re
import subprocess
import sys

OPENVPN = "org.freedesktop.NetworkManager.openvpn"


class ProfileError(Exception):
    pass


def nmcli(args, input_text=None, timeout=15):
    try:
        result = subprocess.run(["nmcli", *args], input=input_text, text=True,
                                capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProfileError("NetworkManager command unavailable or timed out") from exc
    if result.returncode:
        raise ProfileError(result.stderr.strip() or "NetworkManager command failed")
    return result.stdout.strip()


def split_row(line):
    fields, field, escaped = [], "", False
    for char in line:
        if escaped:
            field += char
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append(field)
            field = ""
        else:
            field += char
    fields.append(field + ("\\" if escaped else ""))
    return fields


def require_openvpn(uuid):
    if not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", uuid):
        raise ProfileError("Invalid profile UUID")
    if nmcli(["-g", "vpn.service-type", "connection", "show", "uuid", uuid]) != OPENVPN:
        raise ProfileError("The selected profile is not OpenVPN")


def profiles():
    rows = nmcli(["-t", "--escape", "yes", "-f", "UUID,NAME,TYPE,DEVICE,STATE", "connection", "show"])
    result = []
    for row in rows.splitlines():
        fields = split_row(row)
        if len(fields) != 5 or fields[2] != "vpn":
            continue
        uuid, name, _, device, state = fields
        if nmcli(["-g", "vpn.service-type", "connection", "show", "uuid", uuid]) != OPENVPN:
            continue
        result.append(dict(uuid=uuid, name=name, device=device, state=state,
                           active=state == "activated", connecting=state == "activating"))
    return sorted(result, key=lambda p: (not p["active"], p["name"].casefold()))


def connect(uuid, credentials):
    require_openvpn(uuid)
    if not isinstance(credentials, dict):
        raise ProfileError("Credentials must be a JSON object")
    username = credentials.get("username", "")
    if not isinstance(username, str) or any(c in username for c in "\r\n\0"):
        raise ProfileError("Username must be a single line")
    password = credentials.get("password", "")
    if not isinstance(password, str) or any(c in password for c in "\r\n\0"):
        raise ProfileError("Password must be a single line")
    if username:
        nmcli(["connection", "modify", "uuid", uuid, "vpn.user-name", username])
    args = ["--wait", "45", "connection", "up", "uuid", uuid]
    if password:
        args += ["passwd-file", "/dev/stdin"]
    # Other active tunnels stay connected; all certificate and routing settings
    # come from the imported profile, and credentials never enter argv or disk.
    nmcli(args, "vpn.secrets.password:" + password + "\n" if password else None, timeout=50)


def disconnect(uuid):
    require_openvpn(uuid)
    nmcli(["--wait", "20", "connection", "down", "uuid", uuid], timeout=25)


def import_profile(path):
    if not path:
        raise ProfileError("Choose an OpenVPN configuration file")
    return nmcli(["connection", "import", "type", "openvpn", "file", path])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    for command in ("connect", "disconnect"):
        sub.add_parser(command).add_argument("uuid")
    sub.add_parser("import").add_argument("path", nargs="?")
    args = parser.parse_args()
    try:
        if args.command == "list":
            output = dict(ok=True, profiles=profiles())
        elif args.command == "connect":
            raw = sys.stdin.readline()
            connect(args.uuid, json.loads(raw) if raw.strip() else {})
            output = dict(ok=True)
        elif args.command == "disconnect":
            disconnect(args.uuid)
            output = dict(ok=True)
        else:
            path = args.path
            if path is None:
                picker = subprocess.run(["omarchy", "file", "select", "--title", "Import OpenVPN profile",
                                         "--extensions", "ovpn conf"], text=True, capture_output=True)
                path = picker.stdout.strip()
                if picker.returncode or not path:
                    print(json.dumps(dict(ok=True, cancelled=True)))
                    return
            output = dict(ok=True, message=import_profile(path))
        print(json.dumps(output))
    except (ProfileError, ValueError, OSError) as exc:
        print(json.dumps(dict(ok=False, error=str(exc))))
        sys.exit(1)


if __name__ == "__main__":
    main()
