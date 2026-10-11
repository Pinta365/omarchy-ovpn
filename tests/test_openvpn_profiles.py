"""OpenVPN profile isolation and credential handling; no live network changes."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('profiles', Path(__file__).parents[1] / 'helper/openvpn_profiles.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)
UUID = '12345678-1234-1234-1234-123456789abc'


class Profiles(unittest.TestCase):
    def test_lists_only_openvpn_with_escaped_names(self):
        rows = f'{UUID}:Work\\:VPN:vpn:tun0:activated\nother:Other:vpn:--:deactivated\nwg:WG:wireguard:wg0:activated'
        with patch.object(p, 'nmcli', side_effect=[rows, p.OPENVPN, 'org.freedesktop.NetworkManager.openconnect']):
            self.assertEqual(p.profiles(), [dict(uuid=UUID, name='Work:VPN', device='tun0', state='activated', active=True, connecting=False)])

    def test_rejects_other_vpn_backend_before_disconnect(self):
        with patch.object(p, 'nmcli', return_value='other') as nm:
            with self.assertRaises(p.ProfileError): p.disconnect(UUID)
            self.assertEqual(nm.call_count, 1)

    def test_rejects_malformed_uuid_without_running_nmcli(self):
        with patch.object(p, 'nmcli') as nm:
            with self.assertRaises(p.ProfileError): p.disconnect('--help')
            nm.assert_not_called()

    def test_password_uses_stdin_and_does_not_disconnect_other_tunnels(self):
        with patch.object(p, 'nmcli', side_effect=[p.OPENVPN, '']) as nm:
            p.connect(UUID, {'password': 'secret $ ` value'})
            args, secret = nm.call_args.args
            self.assertNotIn('secret $ ` value', args)
            self.assertEqual(args[-2:], ['passwd-file', '/dev/stdin'])
            self.assertEqual(secret, 'vpn.secrets.password:secret $ ` value\n')
            self.assertEqual(nm.call_count, 2)
            self.assertNotIn('down', args)

    def test_certificate_profiles_need_no_password(self):
        with patch.object(p, 'nmcli', side_effect=[p.OPENVPN, '']) as nm:
            p.connect(UUID, {})
            self.assertIsNone(nm.call_args.args[1])
            self.assertNotIn('passwd-file', nm.call_args.args[0])

    def test_rejects_multiline_secrets(self):
        with patch.object(p, 'nmcli', return_value=p.OPENVPN) as nm:
            with self.assertRaises(p.ProfileError): p.connect(UUID, {'password': 'x\nvpn.secrets.other:y'})
            self.assertEqual(nm.call_count, 1)

    def test_import_passes_filename_as_one_argument(self):
        with patch.object(p, 'nmcli', return_value='imported') as nm:
            p.import_profile('/tmp/work VPN $(id).ovpn')
            self.assertEqual(nm.call_args.args[0], ['connection', 'import', 'type', 'openvpn', 'file', '/tmp/work VPN $(id).ovpn'])

    def test_explicit_username_is_saved_without_password(self):
        with patch.object(p, 'nmcli', side_effect=[p.OPENVPN, '', '']) as nm:
            p.connect(UUID, {'username': 'work-user', 'password': 'session-secret'})
            self.assertEqual(nm.call_args_list[1].args[0], ['connection', 'modify', 'uuid', UUID, 'vpn.user-name', 'work-user'])
            self.assertNotIn('session-secret', str(nm.call_args_list[1]))
