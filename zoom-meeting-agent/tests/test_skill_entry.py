"""The novice entry point works outside the checkout and forwards subcommand help."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ENTRY = Path(__file__).resolve().parents[2] / 'skills/zoom-meeting-agent/scripts/zoom.py'


class SkillEntryTests(unittest.TestCase):
    def test_paths_and_subcommand_help_from_unrelated_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(ENTRY), 'paths'], cwd=directory,
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(Path(json.loads(result.stdout)['core']).is_dir())
            help_result = subprocess.run([sys.executable, str(ENTRY), 'setup', '--help'], cwd=directory,
                                         capture_output=True, text=True, timeout=15)
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            self.assertIn('--download-model', help_result.stdout)
            self.assertIn('--install-mcp', help_result.stdout)
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
