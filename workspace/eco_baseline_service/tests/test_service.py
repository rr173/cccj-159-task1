"""ECO 签核基线服务测试套件。

覆盖:
- 正常流:冻结 -> 依赖图触发检查 -> 双角色签核 -> 发布新基线
- 迟到结果拒绝(修订 / 约束更新 / 基线推进后)
- 重叠模块冲突:排队、合并、拆分、驳回
- 发布中断恢复
- 基线差异包
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eco_service import Engine, Store, EcoStatus, RunStatus, DomainError
from eco_service import diff as diff_mod
from eco_service import workbench as wb
from eco_service.api import Api

MATRIX = [
    {"type": "timing", "config": {"corners": ["ss_0p9_125c", "ff_1p1_m40c"]}},
    {"type": "formal"},
    {"type": "power"},
    {"type": "layout", "config": {"checks": ["drc", "lvs", "antenna"]}},
]

MODULES = {
    "alu": {"netlist": "module alu; ... endmodule"},
    "decoder": {"netlist": "module decoder; ... endmodule"},
    "csr": {"netlist": "module csr; ... endmodule"},
}

CONSTRAINTS = {
    "clocks": {"clk": {"period_ns": 1.0}},
    "max_transition_ns": 0.10,
    "power_budget_mw": 500,
}


def make_engine():
    store = Store(":memory:")
    engine = Engine(store)
    base = engine.seed_baseline(MODULES, CONSTRAINTS, MATRIX, note="initial")
    return engine, base


def pass_all_checks(engine, eco_id):
    """按依赖顺序通过当前所有 DISPATCHED 的检查。"""
    for _ in range(20):
        view = engine.get_eco_view(eco_id)
        dispatched = [
            r for r in view["runs"]
            if r["status"] == RunStatus.DISPATCHED and r["is_current_binding"]
        ]
        if not dispatched:
            return
        for r in dispatched:
            engine.submit_check_result(eco_id, r["id"], r["token"], True,
                                       {"tool": "stub"})
    raise AssertionError("检查调度未收敛")


def eco_change(module, text):
    return {"module_updates": {module: {"netlist": text}}}


class TestHappyPath(unittest.TestCase):
    def setUp(self):
        self.engine, self.base = make_engine()

    def test_full_flow_to_release(self):
        e = self.engine.create_eco(
            "fix alu overflow", eco_change("alu", "module alu; /*fixed*/ endmodule"),
            ["alu"], created_by="designer1")
        self.assertEqual(e["status"], EcoStatus.DRAFT)

        e = self.engine.freeze_eco(e["id"])
        self.assertEqual(e["status"], EcoStatus.FROZEN)
        self.assertIsNotNone(e["snapshot_hash"])

        # 依赖图:timing/formal 先派发,power/layout 等待 timing
        view = self.engine.get_eco_view(e["id"])
        by_name = {r["name"]: r for r in view["runs"]}
        self.assertEqual(by_name["timing"]["status"], RunStatus.DISPATCHED)
        self.assertEqual(by_name["formal"]["status"], RunStatus.DISPATCHED)
        self.assertEqual(by_name["power"]["status"], RunStatus.PENDING)
        self.assertEqual(by_name["layout"]["status"], RunStatus.PENDING)

        # timing 通过 -> power/layout 被依赖图触发
        t = by_name["timing"]
        self.engine.submit_check_result(e["id"], t["id"], t["token"], True)
        view = self.engine.get_eco_view(e["id"])
        by_name = {r["name"]: r for r in view["runs"]}
        self.assertEqual(by_name["power"]["status"], RunStatus.DISPATCHED)
        self.assertEqual(by_name["layout"]["status"], RunStatus.DISPATCHED)

        pass_all_checks(self.engine, e["id"])
        self.assertEqual(self.engine._eco(e["id"])["status"],
                         EcoStatus.READY_FOR_SIGNOFF)

        # 双角色签核
        self.engine.sign_off(e["id"], "DESIGN_AUTHORITY", "alice")
        self.assertEqual(self.engine._eco(e["id"])["status"],
                         EcoStatus.SIGNOFF_PARTIAL)
        self.engine.sign_off(e["id"], "VERIFICATION_AUTHORITY", "bob")
        self.assertEqual(self.engine._eco(e["id"])["status"],
                         EcoStatus.SIGNOFF_COMPLETE)

        bl = self.engine.release(e["id"])
        self.assertEqual(bl["seq"], self.base["seq"] + 1)
        self.assertEqual(bl["created_from_eco"], e["id"])
        self.assertEqual(self.engine._eco(e["id"])["status"], EcoStatus.RELEASED)
        self.assertEqual(self.engine._head()["id"], bl["id"])

    def test_dual_signoff_same_user_rejected(self):
        e = self.engine.create_eco("x", eco_change("alu", "n"), ["alu"])
        self.engine.freeze_eco(e["id"])
        pass_all_checks(self.engine, e["id"])
        self.engine.sign_off(e["id"], "DESIGN_AUTHORITY", "alice")
        with self.assertRaises(DomainError) as ctx:
            self.engine.sign_off(e["id"], "VERIFICATION_AUTHORITY", "alice")
        self.assertEqual(ctx.exception.code, "same_user")

    def test_release_blocked_before_signoff(self):
        e = self.engine.create_eco("x", eco_change("alu", "n"), ["alu"])
        self.engine.freeze_eco(e["id"])
        pass_all_checks(self.engine, e["id"])
        with self.assertRaises(DomainError) as ctx:
            self.engine.release(e["id"])
        self.assertEqual(ctx.exception.code, "invalid_state")


class TestStaleness(unittest.TestCase):
    """迟到结果 / 旧结果不得拼接复用。"""

    def setUp(self):
        self.engine, self.base = make_engine()

    def test_late_result_rejected_after_amend(self):
        e = self.engine.create_eco("x", eco_change("alu", "n1"), ["alu"])
        self.engine.freeze_eco(e["id"])
        view = self.engine.get_eco_view(e["id"])
        run = next(r for r in view["runs"] if r["name"] == "timing")
        old_token = run["token"]

        # 修订 -> 旧运行失效
        self.engine.amend_eco(e["id"], changes=eco_change("alu", "n2"))
        self.assertEqual(self.engine._eco(e["id"])["status"], EcoStatus.DRAFT)

        # 迟到的结果(旧 token)必须被拒绝,且不被计入
        with self.assertRaises(DomainError) as ctx:
            self.engine.submit_check_result(e["id"], run["id"], old_token, True)
        self.assertEqual(ctx.exception.code, "stale_result")

        run_after = self.engine.store.get("check_runs", run["id"])
        self.assertEqual(run_after["status"], RunStatus.INVALIDATED)

        # 审计日志留痕
        kinds = [ev["kind"] for ev in self.engine.store.find("events")]
        self.assertIn("stale_result_rejected", kinds)
        self.assertIn("checks_invalidated", kinds)

    def test_constraint_update_invalidates_inflight(self):
        e = self.engine.create_eco("x", eco_change("alu", "n1"), ["alu"])
        self.engine.freeze_eco(e["id"])
        view = self.engine.get_eco_view(e["id"])
        run = next(r for r in view["runs"] if r["name"] == "formal")
        old_token = run["token"]

        new_head = self.engine.update_base_constraints(
            {"max_transition_ns": 0.08}, note="foundry SDC update")
        self.assertEqual(new_head["constraint_epoch"], 1)

        # ECO 被重置回 DRAFT,旧运行全部失效
        eco = self.engine._eco(e["id"])
        self.assertEqual(eco["status"], EcoStatus.DRAFT)
        self.assertEqual(eco["constraint_epoch"], 1)
        runs = self.engine.store.find("check_runs", "eco_id=?", (e["id"],))
        self.assertTrue(all(r["status"] == RunStatus.INVALIDATED for r in runs))

        # 迟到的 formal 结果被拒绝
        with self.assertRaises(DomainError) as ctx:
            self.engine.submit_check_result(e["id"], run["id"], old_token, True)
        self.assertEqual(ctx.exception.code, "stale_result")

        # 重新冻结后可以在新纪元上完整走完
        self.engine.freeze_eco(e["id"])
        pass_all_checks(self.engine, e["id"])
        self.engine.sign_off(e["id"], "DESIGN_AUTHORITY", "alice")
        self.engine.sign_off(e["id"], "VERIFICATION_AUTHORITY", "bob")
        bl = self.engine.release(e["id"])
        self.assertEqual(bl["constraint_epoch"], 1)

    def test_signoff_revoked_on_amend(self):
        e = self.engine.create_eco("x", eco_change("alu", "n1"), ["alu"])
        self.engine.freeze_eco(e["id"])
        pass_all_checks(self.engine, e["id"])
        self.engine.sign_off(e["id"], "DESIGN_AUTHORITY", "alice")
        self.engine.sign_off(e["id"], "VERIFICATION_AUTHORITY", "bob")
        self.engine.amend_eco(e["id"], changes=eco_change("alu", "n2"))
        sos = self.engine.store.find("signoffs", "eco_id=?", (e["id"],))
        self.assertTrue(all(s["revoked"] == 1 for s in sos))

    def test_no_splice_across_bindings(self):
        """快照 A 的通过结果不能拼接到快照 B 上。"""
        e = self.engine.create_eco("x", eco_change("alu", "n1"), ["alu"])
        self.engine.freeze_eco(e["id"])
        # 快照 A 上通过 formal
        view = self.engine.get_eco_view(e["id"])
        formal = next(r for r in view["runs"] if r["name"] == "formal")
        self.engine.submit_check_result(e["id"], formal["id"], formal["token"], True)
        # 修订 -> 快照 B
        self.engine.amend_eco(e["id"], changes=eco_change("alu", "n2"))
        self.engine.freeze_eco(e["id"])
        # formal 必须重新运行,不能复用快照 A 的通过结果
        view = self.engine.get_eco_view(e["id"])
        current = [r for r in view["runs"] if r["is_current_binding"]]
        formal_runs = [r for r in current if r["name"] == "formal"]
        self.assertEqual(len(formal_runs), 1)
        self.assertEqual(formal_runs[0]["status"], RunStatus.DISPATCHED)
        # 未全部通过前不能签核
        with self.assertRaises(DomainError):
            self.engine.sign_off(e["id"], "DESIGN_AUTHORITY", "alice")

    def test_release_cascade_invalidates_other_ecos(self):
        """A 发布后,其他在途 ECO 的基线变化,旧结果失效。"""
        a = self.engine.create_eco("A", eco_change("alu", "a2"), ["alu"])
        c = self.engine.create_eco("C", eco_change("csr", "c2"), ["csr"])
        self.engine.freeze_eco(a["id"])
        self.engine.freeze_eco(c["id"])
        pass_all_checks(self.engine, c["id"])  # C 在旧基线上全绿
        self.engine.sign_off(c["id"], "DESIGN_AUTHORITY", "alice")
        self.engine.sign_off(c["id"], "VERIFICATION_AUTHORITY", "bob")

        pass_all_checks(self.engine, a["id"])
        self.engine.sign_off(a["id"], "DESIGN_AUTHORITY", "alice")
        self.engine.sign_off(a["id"], "VERIFICATION_AUTHORITY", "bob")
        self.engine.release(a["id"])

        # C 被重置:签核撤销、运行失效、回到 DRAFT 需重新冻结
        c_after = self.engine._eco(c["id"])
        self.assertEqual(c_after["status"], EcoStatus.DRAFT)
        self.assertEqual(c_after["base_baseline_id"], self.engine._head()["id"])
        runs = self.engine.store.find("check_runs", "eco_id=?", (c["id"],))
        self.assertTrue(all(r["status"] == RunStatus.INVALIDATED for r in runs))
        sos = self.engine.store.find("signoffs", "eco_id=?", (c["id"],))
        self.assertTrue(all(s["revoked"] == 1 for s in sos))


class TestConflicts(unittest.TestCase):
    def setUp(self):
        self.engine, self.base = make_engine()

    def _make_pair(self):
        a = self.engine.create_eco("A", eco_change("decoder", "d2"), ["decoder"])
        b = self.engine.create_eco("B", eco_change("decoder", "d3"), ["decoder"])
        self.engine.freeze_eco(a["id"])
        b = self.engine.freeze_eco(b["id"])
        return a, b

    def test_overlap_blocks_and_queues(self):
        a, b = self._make_pair()
        self.assertEqual(b["status"], EcoStatus.BLOCKED)
        self.assertEqual(b["blocked_by"], a["id"])
        conflicts = self.engine.store.find("conflicts", "status='PENDING'")
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["modules"], '["decoder"]')

        # 显式排队裁决
        self.engine.adjudicate(conflicts[0]["id"], "queue", by="lead")
        # A 完成发布 -> B 自动在新基线上重冻结并派发检查
        pass_all_checks(self.engine, a["id"])
        self.engine.sign_off(a["id"], "DESIGN_AUTHORITY", "alice")
        self.engine.sign_off(a["id"], "VERIFICATION_AUTHORITY", "bob")
        new_bl = self.engine.release(a["id"])

        b_after = self.engine._eco(b["id"])
        self.assertEqual(b_after["status"], EcoStatus.FROZEN)
        self.assertEqual(b_after["base_baseline_id"], new_bl["id"])
        runs = self.engine.store.find("check_runs", "eco_id=?", (b["id"],))
        self.assertTrue(any(r["status"] == RunStatus.DISPATCHED for r in runs))
        self.assertTrue(all(
            r["base_baseline_id"] == new_bl["id"] for r in runs))

    def test_merge_adjudication(self):
        a, b = self._make_pair()
        cf = self.engine.store.find("conflicts", "status='PENDING'")[0]
        self.engine.adjudicate(cf["id"], "merge", by="lead")
        a_after = self.engine._eco(a["id"])
        b_after = self.engine._eco(b["id"])
        self.assertEqual(b_after["status"], EcoStatus.MERGED)
        self.assertEqual(a_after["status"], EcoStatus.DRAFT)  # 需重新冻结
        # b 的变更并入 a(同模块以 b 为准)
        import json
        changes = json.loads(a_after["changes"])
        self.assertEqual(changes["module_updates"]["decoder"]["netlist"], "d3")

    def test_partition_adjudication(self):
        a = self.engine.create_eco("A", eco_change("decoder", "d2"), ["decoder"])
        b_changes = {
            "module_updates": {
                "decoder": {"netlist": "d3"},
                "csr": {"netlist": "c2"},
            }
        }
        b = self.engine.create_eco("B", b_changes, ["decoder", "csr"])
        self.engine.freeze_eco(a["id"])
        self.engine.freeze_eco(b["id"])
        cf = self.engine.store.find("conflicts", "status='PENDING'")[0]
        self.engine.adjudicate(
            cf["id"], "partition", by="lead", params={"modules": ["csr"]})
        b_after = self.engine._eco(b["id"])
        self.assertEqual(b_after["status"], EcoStatus.DRAFT)
        import json
        self.assertEqual(json.loads(b_after["affected_modules"]), ["csr"])
        self.assertNotIn("decoder",
                         json.loads(b_after["changes"])["module_updates"])
        # 拆分后不再冲突,可正常冻结
        self.assertEqual(self.engine.freeze_eco(b["id"])["status"], EcoStatus.FROZEN)

    def test_reject_adjudication(self):
        a, b = self._make_pair()
        cf = self.engine.store.find("conflicts", "status='PENDING'")[0]
        self.engine.adjudicate(cf["id"], "reject", by="lead")
        self.assertEqual(self.engine._eco(b["id"])["status"], EcoStatus.REJECTED)

    def test_withdraw_unblocks_queue(self):
        a, b = self._make_pair()
        self.engine.withdraw_eco(a["id"])
        self.assertEqual(self.engine._eco(b["id"])["status"], EcoStatus.FROZEN)


class TestReleaseRecovery(unittest.TestCase):
    def setUp(self):
        self.engine, self.base = make_engine()
        self.eco = self.engine.create_eco("x", eco_change("alu", "n1"), ["alu"])
        self.engine.freeze_eco(self.eco["id"])
        pass_all_checks(self.engine, self.eco["id"])
        self.engine.sign_off(self.eco["id"], "DESIGN_AUTHORITY", "alice")
        self.engine.sign_off(self.eco["id"], "VERIFICATION_AUTHORITY", "bob")

    def test_crash_after_intent_recovers(self):
        def crash(phase):
            if phase == "after_intent":
                raise RuntimeError("power failure")

        with self.assertRaises(RuntimeError):
            self.engine.release(self.eco["id"], _crash_hook=crash)

        # 没有半成品基线
        self.assertEqual(self.engine._head()["id"], self.base["id"])
        self.assertEqual(self.engine._eco(self.eco["id"])["status"],
                         EcoStatus.RELEASING)

        recovered = self.engine.recover()
        self.assertEqual(recovered, [self.eco["id"]])
        self.assertEqual(self.engine._eco(self.eco["id"])["status"],
                         EcoStatus.SIGNOFF_COMPLETE)

        # 恢复后可重试并成功
        bl = self.engine.release(self.eco["id"])
        self.assertEqual(self.engine._head()["id"], bl["id"])

    def test_crash_inside_commit_rolls_back(self):
        def crash(phase):
            if phase == "inside_commit":
                raise RuntimeError("disk full")

        with self.assertRaises(RuntimeError):
            self.engine.release(self.eco["id"], _crash_hook=crash)
        self.assertEqual(self.engine._head()["id"], self.base["id"])
        self.assertEqual(len(self.engine.store.find("baselines")), 1)
        self.engine.recover()
        bl = self.engine.release(self.eco["id"])
        self.assertEqual(bl["seq"], 2)


class TestDiffAndWorkbench(unittest.TestCase):
    def setUp(self):
        self.engine, self.base = make_engine()

    def _release_one(self):
        e = self.engine.create_eco(
            "alu fix + constraint", {
                "module_updates": {"alu": {"netlist": "module alu; v2 endmodule"}},
                "constraint_updates": {"max_transition_ns": 0.09},
            }, ["alu"])
        self.engine.freeze_eco(e["id"])
        pass_all_checks(self.engine, e["id"])
        self.engine.sign_off(e["id"], "DESIGN_AUTHORITY", "alice")
        self.engine.sign_off(e["id"], "VERIFICATION_AUTHORITY", "bob")
        return e, self.engine.release(e["id"])

    def test_diff_package(self):
        e, bl2 = self._release_one()
        d = diff_mod.diff_baselines(self.engine.store, self.base["id"], bl2["id"])
        self.assertEqual(d["modules"]["changed"][0]["module"], "alu")
        self.assertEqual(d["constraints"]["changed"][0]["key"], "max_transition_ns")
        self.assertEqual(d["provenance"][0]["eco"]["id"], e["id"])
        self.assertEqual(len(d["provenance"][0]["signoffs"]), 2)
        self.assertEqual(len(d["provenance"][0]["check_evidence"]), 4)

        with tempfile.TemporaryDirectory() as tmp:
            zip_path = diff_mod.export_diff_package(
                self.engine.store, self.base["id"], bl2["id"], tmp)
            self.assertTrue(zip_path.endswith(".zip"))
            import zipfile
            with zipfile.ZipFile(zip_path) as zf:
                names = zf.namelist()
            self.assertTrue(any("diff_package.json" in n for n in names))

    def test_workbench_pending(self):
        e = self.engine.create_eco("x", eco_change("alu", "n1"), ["alu"])
        self.engine.freeze_eco(e["id"])
        pass_all_checks(self.engine, e["id"])
        pending = wb.pending_signoffs(self.engine)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["action"], "SIGN")
        self.assertEqual(len(pending[0]["signoffs_pending"]), 2)
        self.engine.sign_off(e["id"], "DESIGN_AUTHORITY", "alice")
        self.engine.sign_off(e["id"], "VERIFICATION_AUTHORITY", "bob")
        pending = wb.pending_signoffs(self.engine)
        self.assertEqual(pending[0]["action"], "RELEASE")

        dash = wb.eco_dashboard(self.engine, e["id"])
        self.assertTrue(dash["readiness"]["releasable"])
        self.assertEqual(len(dash["checks"]), 4)


class TestApi(unittest.TestCase):
    def setUp(self):
        self.engine, self.base = make_engine()
        self.api = Api(self.engine)

    def test_api_flow(self):
        status, eco = self.api.handle("POST", "/ecos", {
            "title": "api eco", "changes": eco_change("alu", "n1"),
            "affected_modules": ["alu"], "created_by": "tester"})
        self.assertEqual(status, 200)
        eco_id = eco["id"]

        status, eco = self.api.handle("POST", f"/ecos/{eco_id}/freeze")
        self.assertEqual(status, 200)
        self.assertEqual(eco["status"], "FROZEN")

        for _ in range(10):
            status, view = self.api.handle("GET", f"/ecos/{eco_id}")
            dispatched = [r for r in view["runs"]
                          if r["status"] == "DISPATCHED" and r["is_current_binding"]]
            if not dispatched:
                break
            for r in dispatched:
                s, _ = self.api.handle(
                    "POST", f"/ecos/{eco_id}/checks/{r['id']}/results",
                    {"token": r["token"], "passed": True})
                self.assertEqual(s, 200)

        for role, user in (("DESIGN_AUTHORITY", "alice"),
                            ("VERIFICATION_AUTHORITY", "bob")):
            s, _ = self.api.handle("POST", f"/ecos/{eco_id}/signoffs",
                                   {"role": role, "user": user})
            self.assertEqual(s, 200)

        s, bl = self.api.handle("POST", f"/ecos/{eco_id}/release")
        self.assertEqual(s, 200)
        self.assertEqual(bl["seq"], 2)

        s, d = self.api.handle(
            "GET", f"/baselines/{self.base['id']}/diff/{bl['id']}")
        self.assertEqual(s, 200)
        self.assertEqual(d["modules"]["changed"][0]["module"], "alu")

        s, pending = self.api.handle("GET", "/workbench/pending")
        self.assertEqual(s, 200)
        self.assertEqual(pending["pending"], [])

    def test_api_error_mapping(self):
        s, body = self.api.handle("GET", "/ecos/eco-nonexistent")
        self.assertEqual(s, 404)
        s, body = self.api.handle("POST", "/ecos/eco-nonexistent/freeze")
        self.assertEqual(s, 404)
        s, body = self.api.handle("GET", "/nope")
        self.assertEqual(s, 404)


if __name__ == "__main__":
    unittest.main()
