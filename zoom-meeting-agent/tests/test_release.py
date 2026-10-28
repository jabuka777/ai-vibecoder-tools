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
        for name in ('meeting-password.txt', 'outbox.json', 'control.json', 'storage_state.json',
                     '.setup-state.json', '.setup.lock', '.sdk-setup.lock', 'sdk-installation.json', 'installation.json',
                     'meetings/demo.json', 'runs/123/meeting.json', 'accounts/personal/state.json',
                     'accounts/assistant/login-backups/old.json', 'login-backups/old.json',
                     '.sdk-backups/123/static/index.html', '.sdk-stage-123/index.html'):
            with self.subTest(name=name), patch.object(release.subprocess, 'check_output',
                    return_value=('zoom-meeting-agent/' + name + '\0').encode()):
                with self.assertRaises(RuntimeError):
                    list(release.release_paths())

    def test_skill_and_onboarding_are_included(self):
        names = ['START_HERE.md', 'SECURITY.md', 'skills/zoom-meeting-agent/SKILL.md',
                 'skills/zoom-meeting-agent/agents/openai.yaml', 'skills/zoom-meeting-agent/scripts/zoom.py',
                 'tools/install_zoom_skill.py', 'zoom-meeting-agent/requirements-mcp.txt']
        with patch.object(release.subprocess, 'check_output', return_value=('\0'.join(names) + '\0').encode()):
            self.assertEqual(list(release.release_paths()), [Path(name) for name in names])


if __name__ == '__main__':
    unittest.main()
