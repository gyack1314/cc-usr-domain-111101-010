import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class CliTests(unittest.TestCase):
    def test_real_inspection(self):
        result=subprocess.run([sys.executable,'-m','datapack.cli','inspect','samples'],cwd=ROOT,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(json.loads(result.stdout)['files'])
    def test_invalid_path_is_failure(self):
        result=subprocess.run([sys.executable,'-m','datapack.cli','inspect','missing-directory'],cwd=ROOT,capture_output=True,text=True)
        self.assertEqual(result.returncode,2);self.assertEqual(result.stdout,'')
    def test_gc_plan_and_stats(self):
        with tempfile.TemporaryDirectory() as d:
            seed=subprocess.run([sys.executable,'-m','datapack.seed',d],cwd=ROOT,capture_output=True,text=True)
            self.assertEqual(seed.returncode,0,seed.stderr)
            plan=subprocess.run([sys.executable,'-m','datapack.cli','gc-plan',d,'--clock','120'],cwd=ROOT,capture_output=True,text=True)
            self.assertEqual(plan.returncode,0,plan.stderr)
            self.assertTrue(json.loads(plan.stdout)['plan_id'])
            stats=subprocess.run([sys.executable,'-m','datapack.cli','gc-stats',d],cwd=ROOT,capture_output=True,text=True)
            self.assertEqual(stats.returncode,0,stats.stderr)
            self.assertIn('live',json.loads(stats.stdout))
