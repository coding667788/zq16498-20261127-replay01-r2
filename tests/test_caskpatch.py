"""caskpatch 单元测试:check/arrange/verify + 三阶段杀进程断电恢复。"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from caskpatch_core import CaseError, run_arrange, run_check, run_verify  # noqa: E402

CLI = ROOT / "caskpatch.py"
FIX = ROOT / "fixtures"


def load_fixture(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class CheckTests(unittest.TestCase):
    def test_ok_case(self):
        rep = run_check(load_fixture("ok_case.json"))
        self.assertEqual(rep["status"], "ok")
        self.assertIsNone(rep["first_unsafe"])
        self.assertEqual(rep["baseline"]["space_total"], 2)
        self.assertEqual(rep["space_peak"]["value"], 3)
        # 同摘要双槽位 a/a2:唯一块 2,重复引用 1
        self.assertEqual(rep["final"]["unique_blocks"], 2)
        self.assertEqual(rep["final"]["duplicate_refs"], 1)
        # 每一步重算可见根
        self.assertEqual(rep["steps"][0]["visible_roots"], {"main": "old"})
        self.assertEqual(rep["steps"][2]["visible_roots"], {"main": "new"})

    def test_unsafe_release(self):
        rep = run_check(load_fixture("unsafe_release_case.json"))
        self.assertEqual(rep["status"], "unsafe")
        first = rep["first_unsafe"]
        self.assertEqual(first["step"], 0)
        self.assertEqual(first["reason"], "premature-release")
        self.assertEqual(first["reference_chain"], ["r", "b"])

    def test_protection_violation(self):
        rep = run_check(load_fixture("protection_case.json"))
        self.assertEqual(rep["status"], "unsafe")
        self.assertEqual(rep["first_unsafe"]["reason"], "protection-violation")
        self.assertEqual(rep["first_unsafe"]["reference_chain"], ["r2", "x2"])

    def test_external_trace_override(self):
        case = load_fixture("ok_case.json")
        rep = run_check(case, trace=[{"id": "u1", "op": "release", "slot": "b"}])
        self.assertEqual(rep["status"], "unsafe")
        self.assertEqual(rep["first_unsafe"]["reason"], "premature-release")


class ArrangeTests(unittest.TestCase):
    def setUp(self):
        self.rep = run_arrange(load_fixture("arrange_case.json"))

    def test_status_and_verification(self):
        self.assertEqual(self.rep["status"], "ok")
        self.assertTrue(self.rep["verification"]["all_breakpoints_safe"])
        self.assertTrue(self.rep["verification"]["snapshots_satisfied"])

    def test_optimality(self):
        m = self.rep["metrics"]
        # PA 满:i3 先整组回家;i1 跨域到 PB;i2 等 o1 释放后回家
        self.assertEqual(m["space_peak_additional"], 2)
        self.assertEqual(m["cross_domain_ops"], 1)
        self.assertEqual(m["switch_order"], ["sw_c", "sw_a", "sw_b"])

    def test_retained_snapshot_kept(self):
        ids = self.rep["metrics"]["action_order"]
        self.assertNotIn("release-o3", ids)  # 保留快照钉住 o3
        self.assertIn("release-o1", ids)
        self.assertIn("release-o2", ids)

    def test_switch_limit(self):
        case = load_fixture("arrange_case.json")
        case["switches"] = [dict(s, action_id=f"sw{i:02d}")
                            for i, s in enumerate(case["switches"] * 6)]
        with self.assertRaises(CaseError):
            run_arrange(case)


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.rep = run_verify(load_fixture("verify_case.json"))

    def test_manifest_digest_audit(self):
        bad = [d for d in self.rep["manifest_digest_audit"] if d["slot"] == "b_bad"]
        self.assertEqual(len(bad), 1)
        self.assertNotEqual(bad[0]["declared"], bad[0]["recomputed"])
        self.assertFalse(self.rep["manifest_digest_ok"])

    def test_trace_audit_ok(self):
        self.assertEqual(self.rep["trace_audit"]["status"], "ok")

    def test_midway_digest_counterexample(self):
        ce = next(c for c in self.rep["counterexamples"] if c["name"] == "midway-digest")
        # 终态看似合理,中途暴露错误摘要
        self.assertTrue(ce["final_state_looks_valid"])
        self.assertEqual(ce["failing_step"], 0)
        self.assertEqual(ce["reason"], "digest-mismatch")
        self.assertEqual(run_check(ce["case"])["status"], "unsafe")

    def test_shared_old_counterexample(self):
        ce = next(c for c in self.rep["counterexamples"]
                  if c["name"] == "shared-old-early-release")
        self.assertEqual(ce["shared_slot"], "X")
        self.assertTrue(ce["crash_simulation"]["rollback_failure"])
        self.assertEqual(ce["crash_simulation"]["missing_blocks"], ["X"])
        self.assertEqual(run_check(ce["case"])["status"], "unsafe")


class CrashRecoveryTests(unittest.TestCase):
    """在三个阶段分别杀掉子进程,再重开其创建的本地数据库。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dbdir = Path(self.tmp.name) / "db"
        subprocess.run([sys.executable, str(CLI), "db-create", str(self.dbdir),
                        str(FIX / "ok_case.json")],
                       check=True, capture_output=True, text=True, encoding="utf-8")
        plan = {"actions": [
            {"id": "s1", "op": "stage", "slot": "b2", "partition": "P1",
             "version": "new"},
            {"id": "s2", "op": "stage", "slot": "a2", "partition": "P0",
             "version": "new"},
            {"id": "c1", "op": "commit_root", "index": "main", "to": "new"},
            {"id": "r1", "op": "release", "slot": "b"}]}
        self.planfile = Path(self.tmp.name) / "plan.json"
        self.planfile.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")

    def inspect(self):
        out = subprocess.run([sys.executable, str(CLI), "inspect", str(self.dbdir)],
                             check=True, capture_output=True, text=True,
                             encoding="utf-8")
        return json.loads(out.stdout)

    def kill_at(self, marker):
        proc = subprocess.Popen(
            [sys.executable, str(CLI), "commit", str(self.dbdir),
             str(self.planfile), "--pause"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1)
        try:
            while True:
                line = proc.stdout.readline()
                if line == "" or line.strip() == f"PHASE {marker}":
                    break
                if line.startswith("PHASE"):  # 放行到下一个阶段
                    proc.stdin.write("\n")
                    proc.stdin.flush()
        finally:
            proc.kill()
            proc.wait()
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    stream.close()
                except Exception:
                    pass
        return self.inspect()

    def test_kill_after_stage_rolls_back(self):
        info = self.kill_at("staged")
        self.assertEqual(info["roots"], {"main": "old"})  # 只见完整旧版
        self.assertEqual(info["candidates"], [])
        self.assertIn("b", info["blocks"])
        self.assertEqual(info["recovered"]["recovery"], "rollback")

    def test_kill_after_root_rolls_forward(self):
        info = self.kill_at("root")
        self.assertEqual(info["roots"], {"main": "new"})  # 只见完整新版
        self.assertNotIn("b", info["blocks"])
        self.assertIn("b2", info["blocks"])
        self.assertEqual(info["recovered"]["recovery"], "roll-forward")

    def test_kill_after_release_is_idempotent(self):
        info = self.kill_at("released")
        self.assertEqual(info["roots"], {"main": "new"})
        self.assertNotIn("b", info["blocks"])
        self.assertEqual(sorted(info["blocks"]), ["a", "a2", "b2"])

    def test_clean_commit(self):
        out = subprocess.run([sys.executable, str(CLI), "commit", str(self.dbdir),
                              str(self.planfile)],
                             check=True, capture_output=True, text=True,
                             encoding="utf-8")
        self.assertIn("DONE", out.stdout)
        info = self.inspect()
        self.assertEqual(info["roots"], {"main": "new"})
        self.assertIsNone(info["recovered"])
        self.assertEqual(sorted(info["blocks"]), ["a", "a2", "b2"])


class CliTests(unittest.TestCase):
    def test_check_cli_utf8_json(self):
        out = subprocess.run([sys.executable, str(CLI), "check",
                              str(FIX / "ok_case.json")],
                             capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(out.returncode, 0, out.stderr)
        rep = json.loads(out.stdout)
        self.assertEqual(rep["case_id"], "隔离档案柜-ok-01")

    def test_check_cli_unsafe_exit_code(self):
        out = subprocess.run([sys.executable, str(CLI), "check",
                              str(FIX / "unsafe_release_case.json")],
                             capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(out.returncode, 3)
        self.assertEqual(json.loads(out.stdout)["status"], "unsafe")

    def test_arrange_and_verify_cli(self):
        out = subprocess.run([sys.executable, str(CLI), "arrange",
                              str(FIX / "arrange_case.json")],
                             capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["metrics"]["cross_domain_ops"], 1)
        out = subprocess.run([sys.executable, str(CLI), "verify",
                              str(FIX / "verify_case.json")],
                             capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(len(json.loads(out.stdout)["counterexamples"]), 2)


if __name__ == "__main__":
    unittest.main()
