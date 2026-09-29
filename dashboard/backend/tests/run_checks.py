"""Run the dashboard's synthetic regression suites without duplicate imports."""
import importlib
from pathlib import Path
import sys
import unittest

folder = Path(__file__).resolve().parent
sys.path.insert(0, str(folder.parent))
sys.path.insert(0, str(folder))
suite = unittest.TestSuite()
for path in sorted(folder.glob('test_*.py')):
    module = importlib.import_module(path.stem)
    for value in vars(module).values():
        if isinstance(value, type) and issubclass(value, unittest.TestCase) and value.__module__ == module.__name__:
            suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(value))
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
