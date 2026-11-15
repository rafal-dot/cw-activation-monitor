"""The branded entry point and old user commands both remain usable."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class BrandingTests(unittest.TestCase):
    def test_primary_and_legacy_launchers_offer_the_same_cw_hunter_cli(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            env = dict(os.environ, XDG_CONFIG_HOME=temporary)
            for launcher in ("cw_hunter.py", "pota_alert.py"):
                result = subprocess.run([sys.executable, launcher, "--help"], cwd=root,
                                        env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("CW Hunter", result.stdout)
                self.assertIn("--friends-file", result.stdout)
                demo = subprocess.run([sys.executable, launcher, "--demo", "--qth", "IO91AA"], cwd=root,
                                      env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(demo.returncode, 0, demo.stderr)
                self.assertIn("ACTIVE", demo.stdout)
