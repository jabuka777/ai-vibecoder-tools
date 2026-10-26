"""A forced tracked password or runtime file must fail archive construction."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[2] / 'tools/build_release.py'
spec = importlib.util.spec_from_file_location('meeting_release', SOURCE)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def test_private_files_are_rejected_even_when_tracked(self):
        for name in ('meeting-password.txt', 'outbox.json', 'control.json', 'storage_state.json'):
            with self.subTest(name=name), patch.object(release.subprocess, 'check_output',
                    return_value=('zoom-meeting-agent/' + name + '\0').encode()):
                with self.assertRaises(RuntimeError):
                    list(release.release_paths())


if __name__ == '__main__':
    unittest.main()
