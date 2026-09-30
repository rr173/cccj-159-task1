#!/usr/bin/env python3
"""端到端演示:ECO 提交 -> 冻结 -> 依赖图检查 -> 双角色签核 -> 发布基线,
以及迟到结果拒绝、约束更新失效、冲突排队/合并、发布中断恢复、差异包导出。"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from eco_service import Engine, Store, EcoStatus, RunStatus, DomainError
from eco_service.diff import export_diff_package

MATRIX = [
    {"type": "timing", "config": {"corners": ["ss_0p9_125c", "ff_1p1_m40c"]}},
    {"type": "formal"},
    {"type": "power"},
    {"type": "layout", "config": {"checks": ["drc", "lvs", "antenna"]}},
]
MODULES = {
    "alu": {"netlist": "module alu; /*v1*/ endmodule"},
    "decoder": {"netlist": "module decoder; /*v1*/ endmodule"},
    "csr": {"netlist": "module csr; /*v1*/ endmodule"},
}
CONSTRAINTS = {"clocks": {"clk": {"period_ns": 1.0}},
               "max_transition_ns": 0.10, "power_budget_mw": 500}


def show(title, obj=None):
    print(f"\n=== {title} ===")
    if obj is not None:
        print(json.dumps(obj, ensure_ascii=False, indent=2, default=str)[:1200])


def run_checks(engine, eco_id):
    while True:
        view = engine.get_eco_view(eco_id)
        dispatched = [r for r in view["runs"]
                      if r["status"] == RunStatus.DISPATCHED
                      and r["is_current_binding"]]
        if not dispatched:
            break
        for r in dispatched:
            engine.submit_check_result(eco_id, r["id"], r["token"], True,
                                       {"tool": "stub", "wcns_ps": 12})


def main():
    engine = Engine(Store(":memory:"))
    base = engine.seed_baseline(MODULES, CONSTRAINTS, MATRIX, note="MPW-0")
    show("1. 初始基线", {"id": base["id"], "seq": base["seq"],
                        "modules": list(MODULES)})

    # ---- 场景 1:正常流 ----
    eco1 = engine.create_eco(
        "ECO-101 alu 溢出修复",
        {"module_updates": {"alu": {"netlist": "module alu; /*v2 fix*/ endmodule"}}},
        ["alu"], created_by="designer_wang")
    engine.freeze_eco(eco1["id"])
    view = engine.get_eco_view(eco1["id"])
    show("2. ECO-101 冻结后按依赖图派发检查(timing/formal 先行,power/layout 等待)",
         {r["name"]: r["status"] for r in view["runs"]})
    run_checks(engine, eco1["id"])
    engine.sign_off(eco1["id"], "DESIGN_AUTHORITY", "alice")
    engine.sign_off(eco1["id"], "VERIFICATION_AUTHORITY", "bob")
    bl2 = engine.release(eco1["id"])
    show("3. ECO-101 双角色签核完成,发布新基线",
         {"baseline": bl2["id"], "seq": bl2["seq"]})

    # ---- 场景 2:迟到结果拒绝 ----
    eco2 = engine.create_eco(
        "ECO-102 csr 位宽调整",
        {"module_updates": {"csr": {"netlist": "module csr; /*v2*/ endmodule"}}},
        ["csr"])
    engine.freeze_eco(eco2["id"])
    view = engine.get_eco_view(eco2["id"])
    late = next(r for r in view["runs"] if r["name"] == "timing")
    engine.amend_eco(eco2["id"], changes={
        "module_updates": {"csr": {"netlist": "module csr; /*v3*/ endmodule"}}})
    try:
        engine.submit_check_result(eco2["id"], late["id"], late["token"], True)
    except DomainError as e:
        show("4. ECO 修订后,迟到的 timing 结果被拒绝(不拼接复用)",
             {"error": e.code, "message": str(e)})
    engine.freeze_eco(eco2["id"])  # 重新冻结,全新一轮检查

    # ---- 场景 3:基础约束更新,在途结果全部失效 ----
    eco3 = engine.create_eco(
        "ECO-103 decoder 时序优化",
        {"module_updates": {"decoder": {"netlist": "module decoder; /*v2*/ endmodule"}}},
        ["decoder"])
    engine.freeze_eco(eco3["id"])
    view = engine.get_eco_view(eco3["id"])
    stale = next(r for r in view["runs"] if r["name"] == "formal")
    head = engine.update_base_constraints({"max_transition_ns": 0.08},
                                          note="foundry SDC 更新")
    try:
        engine.submit_check_result(eco3["id"], stale["id"], stale["token"], True)
    except DomainError as e:
        show("5. 约束纪元推进后,迟到的 formal 结果被拒绝",
             {"new_epoch": head["constraint_epoch"],
              "error": e.code, "message": str(e)})

    # ---- 场景 4:重叠模块冲突 -> 排队裁决 ----
    engine.freeze_eco(eco3["id"])  # ECO-103 在新纪元重新冻结
    eco4 = engine.create_eco(
        "ECO-104 decoder 功能扩展",
        {"module_updates": {"decoder": {"netlist": "module decoder; /*v3*/ endmodule"}}},
        ["decoder"])
    eco4 = engine.freeze_eco(eco4["id"])
    conflicts = engine.store.find("conflicts", "status='PENDING'")
    show("6. ECO-104 与 ECO-103 修改重叠模块 decoder -> 自动排队",
         {"eco4_status": eco4["status"], "blocked_by": eco4["blocked_by"],
          "conflict_modules": conflicts[0]["modules"]})
    engine.adjudicate(conflicts[0]["id"], "queue", by="lead_chen")

    # ECO-103 走完发布,ECO-104 自动在新基线上解冻重跑
    run_checks(engine, eco3["id"])
    engine.sign_off(eco3["id"], "DESIGN_AUTHORITY", "alice")
    engine.sign_off(eco3["id"], "VERIFICATION_AUTHORITY", "bob")
    bl3 = engine.release(eco3["id"])
    eco4 = engine._eco(eco4["id"])
    show("7. ECO-103 发布后,ECO-104 自动在新基线上重冻结",
         {"eco4_status": eco4["status"], "base": eco4["base_baseline_id"],
          "new_head": bl3["id"]})

    # ---- 场景 5:发布中断恢复 ----
    run_checks(engine, eco4["id"])
    engine.sign_off(eco4["id"], "DESIGN_AUTHORITY", "alice")
    engine.sign_off(eco4["id"], "VERIFICATION_AUTHORITY", "bob")

    def boom(phase):
        if phase == "after_intent":
            raise RuntimeError("模拟发布中断")

    try:
        engine.release(eco4["id"], _crash_hook=boom)
    except RuntimeError:
        pass
    recovered = engine.recover()
    bl4 = engine.release(eco4["id"])
    show("8. 发布中断 -> recover 回滚 -> 重试成功(无半成品基线)",
         {"recovered": recovered, "final_baseline": bl4["id"],
          "seq": bl4["seq"]})

    # ---- 场景 6:基线差异包 ----
    out = tempfile.mkdtemp(prefix="eco_diff_")
    zip_path = export_diff_package(engine.store, base["id"], bl4["id"], out)
    from eco_service.diff import diff_baselines
    d = diff_baselines(engine.store, base["id"], bl4["id"])
    show("9. 基线差异包(初始基线 -> 最新发布)",
         {"package": zip_path,
          "modules_changed": [c["module"] for c in d["modules"]["changed"]],
          "constraints_changed": d["constraints"]["changed"],
          "released_ecos": [p["eco"]["id"] for p in d["provenance"]]})

    print("\n演示完成。差异包目录:", out)


if __name__ == "__main__":
    main()
