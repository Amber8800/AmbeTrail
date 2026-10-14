"""Run the core's real-filesystem/Git tests without third-party packages."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
suite = unittest.defaultTestLoader.discover(str(pathlib.Path(__file__).parent / "tests"))
raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
