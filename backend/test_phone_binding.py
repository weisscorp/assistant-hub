import json
from pathlib import Path
import tempfile
import unittest
import uuid

from home_link import HomeLinkStore
from phone_link_mcp import DeviceBinding


class DeviceBindingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.file = self.root / 'phone.json'
        self.device, self.other = str(uuid.uuid4()), str(uuid.uuid4())
        self.binding = DeviceBinding(device_file=self.file)

    def tearDown(self):
        self.temp.cleanup()

    def test_unpaired_does_not_guess_the_only_synced_phone(self):
        store = HomeLinkStore(self.root / 'state')
        store.sync(self.device, dict(version=1, revision=1, sources=[], people=[], note=None))
        with self.assertRaises(FileNotFoundError):
            self.binding.resolve()
        self.assertEqual(self.device, DeviceBinding(device=self.device).resolve())

    def test_pairing_and_revocation_are_effective_without_restart(self):
        self.file.write_text(json.dumps({'device': self.device}))
        self.assertEqual(self.device, self.binding.resolve())
        self.file.write_text(json.dumps({'device': self.other}))
        self.assertEqual(self.other, self.binding.resolve())
        self.file.unlink()
        with self.assertRaises(FileNotFoundError):
            self.binding.resolve()

    def test_invalid_binding_cannot_select_a_device(self):
        for value in ({}, {'device': None}, {'device': '../elsewhere'},
                      {'device': self.device.upper()}, {'device': self.device, 'automatic': True},
                      {'device': self.device, 'padding': 'x' * 100}, self.device):
            self.file.write_text(json.dumps(value))
            with self.assertRaises((ValueError, TypeError)):
                self.binding.resolve()
        for args in ({}, {'device': self.device, 'device_file': self.file}):
            with self.assertRaises(ValueError):
                DeviceBinding(**args)


if __name__ == '__main__': unittest.main()
