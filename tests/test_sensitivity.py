"""偏移敏感性扫描的纯标准库测试（无需 FastAPI / 网络）。

用法:  python tests/test_sensitivity.py
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA_DIR = tempfile.mkdtemp(prefix="audit-sens-test-")
os.environ["DATA_DIR"] = DATA_DIR

from app.db import init_db  # noqa: E402
from app import sensitivity  # noqa: E402
from app.engine import compute  # noqa: E402
from app.importer import import_payload, reset_all  # noqa: E402

init_db(load_sample=False)

EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "examples",
                       "login-anomaly.json")


class ScanTest(unittest.TestCase):
    def setUp(self):
        reset_all()
        with open(EXAMPLE, encoding="utf-8") as f:
            import_payload(json.load(f))

    def scan(self, sid="app-server", lo=-120, hi=120, overrides=None):
        return sensitivity.scan_offset_sensitivity(sid, lo, hi, overrides)

    def test_critical_points_analytic(self):
        """手工推算：app-server 相关 4 条跨来源边 × 2 个端点 = 8 个临界点。

        以 08:00:00 为 0 秒：g1=100±0.3, g2=140±0.3, g3=200±0.3,
        a1=170±1, a2=230±1, a3=250±1（随 offset x 平移）, m1=90±5.2, m2=105±5.3。
        g1→a1: 68.7/71.3；a1→m1: 73.8/86.2；g2→a2: 88.7/91.3；g3→a3: 48.7/51.3。
        """
        d = self.scan()
        offsets = sorted({c["offset"] for c in d["critical_points"]})
        expected = [48.7, 51.3, 68.7, 71.3, 73.8, 86.2, 88.7, 91.3]
        self.assertEqual(len(offsets), len(expected))
        for got, exp in zip(offsets, expected):
            self.assertAlmostEqual(got, exp, places=4)
        # 8 个临界点把 [-120, 120] 切成 9 个稳定区间
        self.assertEqual(len(d["intervals"]), 9)
        # 临界点元数据可定位到边与穿越端点
        cp = next(c for c in d["critical_points"]
                  if abs(c["offset"] - 48.7) < 1e-4)
        self.assertEqual((cp["from"], cp["to"], cp["which"]), ("g3", "a3", "gap_lo"))

    def test_offset_cancellation_same_source(self):
        """两端同属被扫描来源的边（m1→m2 因果边 + source_order 边）offset 抵消。"""
        d = self.scan("mobile-app", -200, 200)
        keys = {(c["from"], c["to"]) for c in d["critical_points"]}
        self.assertEqual(keys, {("a1", "m1"), ("m2", "g2")})
        self.assertNotIn(("m1", "m2"), keys)  # 不制造临界点
        self.assertEqual(d["edges"]["invariant"], 2)
        self.assertEqual(d["edges"]["scanned"], 2)

    def test_interval_diff_against_baseline(self):
        d = self.scan()
        iv = next(i for i in d["intervals"]
                  if abs(i["lo"] - 51.3) < 1e-4 and abs(i["hi"] - 68.7) < 1e-4)
        self.assertAlmostEqual(iv["representative"], 60.0, places=4)
        # offset=60：g3→a3 新暴露为不可能，a1→m1 仍不可能（需 >73.8 才解除）
        self.assertEqual({c["signature"] for c in iv["conflicts_added"]},
                         {"impossible:g3->a3"})
        self.assertEqual(iv["conflicts_removed"], [])
        self.assertEqual({(e["from"], e["to"]) for e in iv["impossible_added"]},
                         {("g3", "a3")})
        self.assertEqual(iv["impossible_removed"], [])
        self.assertEqual(iv["conflicts_persisted"], 1)
        # 首区间（-120, 48.7）与基线（offset=0）状态一致：无新增/消失
        first = d["intervals"][0]
        self.assertEqual(first["conflicts_added"], [])
        self.assertEqual(first["conflicts_removed"], [])
        self.assertEqual(first["impossible_added"], [])
        self.assertEqual(first["impossible_removed"], [])
        self.assertTrue(first["contains_current"])  # 当前 offset=0 落在首段

    def test_interval_matches_direct_compute(self):
        """每段代表值直接喂给引擎，结果必须与分段记录一致。"""
        d = self.scan()
        for iv in d["intervals"]:
            res = compute({"app-server": {"clock_offset": iv["representative"]}})
            imp = {(e["from"], e["to"], e["kind"])
                   for e in res["edges"] if e["status"] == "impossible"}
            self.assertEqual(imp, {(e["from"], e["to"], e["kind"])
                                   for e in iv["impossible_edges"]})
            sigs = {c["signature"] for c in res["conflicts"]}
            self.assertEqual(sigs, set(iv["conflict_signatures"]))

    def test_overrides_shift_critical_points(self):
        """页面未保存 overrides（mobile-app offset=-30）参与基线，临界点随之平移。"""
        d = self.scan(overrides={"mobile-app": {"clock_offset": -30}})
        offsets = {c["offset"] for c in d["critical_points"]
                   if (c["from"], c["to"]) == ("a1", "m1")}
        self.assertEqual(len(offsets), 2)
        self.assertTrue(any(abs(x - 56.2) < 1e-4 for x in offsets))
        self.assertTrue(any(abs(x - 43.8) < 1e-4 for x in offsets))
        # 基线反映未保存调整
        self.assertEqual(d["baseline"]["offset"], 0.0)

    def test_baseline_reflects_current(self):
        d = self.scan()
        self.assertEqual(d["baseline"]["offset"], 0.0)
        self.assertTrue(d["baseline"]["offset_in_range"])
        sigs = {c["signature"] for c in d["baseline"]["conflicts"]}
        self.assertIn("impossible:a1->m1", sigs)
        self.assertEqual({(e["from"], e["to"])
                          for e in d["baseline"]["impossible_edges"]},
                         {("a1", "m1")})

    def test_invalid_bounds(self):
        for lo, hi in ((10, 10), (20, 10), (float("nan"), 10),
                       (float("inf"), 20), (-4e8, 10), (-10, 4e8)):
            with self.assertRaises(sensitivity.SensitivityError,
                                   msg=f"lo={lo}, hi={hi}"):
                self.scan(lo=lo, hi=hi)
        with self.assertRaises(sensitivity.SensitivityError):
            self.scan(lo="abc", hi=10)

    def test_unknown_source(self):
        with self.assertRaises(sensitivity.SensitivityError) as ctx:
            self.scan("ghost")
        self.assertIn("不存在", str(ctx.exception))
        with self.assertRaises(sensitivity.SensitivityError):
            self.scan("")

    def test_no_critical_in_range_message(self):
        d = self.scan(lo=0, hi=10)
        self.assertEqual(d["intervals"], [])
        self.assertIsNotNone(d["message"])
        self.assertIn("没有临界点", d["message"])
        self.assertIn("48.7", d["message"])  # 提示最近的临界点位置

    def test_source_without_edges_message(self):
        import_payload({"events": [
            {"id": "lonely", "source": "lone-src",
             "time": "2026-09-18T09:00:00Z"}]})
        d = self.scan("lone-src", -10, 10)
        self.assertEqual(d["intervals"], [])
        self.assertIn("未参与任何约束边", d["message"])

    def test_interval_cap(self):
        old = sensitivity.MAX_INTERVALS
        sensitivity.MAX_INTERVALS = 3
        try:
            with self.assertRaises(sensitivity.SensitivityError) as ctx:
                self.scan()
            self.assertIn("缩小扫描范围", str(ctx.exception))
        finally:
            sensitivity.MAX_INTERVALS = old

    def test_response_shape(self):
        d = self.scan()
        for key in ("scan_type", "source", "range", "baseline", "edges",
                    "critical_points", "intervals", "message"):
            self.assertIn(key, d)
        iv = d["intervals"][0]
        for key in ("lo", "hi", "representative", "contains_current", "stats",
                    "impossible_edges", "impossible_added", "impossible_removed",
                    "conflict_signatures", "conflicts_added", "conflicts_removed",
                    "conflicts_persisted"):
            self.assertIn(key, iv)


if __name__ == "__main__":
    unittest.main(verbosity=2)
