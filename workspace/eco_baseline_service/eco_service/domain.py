"""领域模型与纯函数:状态枚举、快照哈希、基线合成。

设计要点:
- ECO 快照哈希只覆盖 ECO 自身内容(变更集 / 受影响模块 / 验证矩阵),
  检查结果另绑定 (base_baseline_id, constraint_epoch),四元组共同构成
  "同一基线" 的判定键。
- 基线合成是纯函数,便于审计与重放。
"""

import copy
import hashlib
import json

CHECK_TYPES = ("timing", "power", "formal", "layout")

# 默认检查依赖图:power / layout 依赖 timing 通过后才触发。
DEFAULT_DAG = {
    "timing": [],
    "formal": [],
    "power": ["timing"],
    "layout": ["timing"],
}

# 双角色签核:两个角色缺一不可,且必须由不同人完成。
SIGNOFF_ROLES = ("DESIGN_AUTHORITY", "VERIFICATION_AUTHORITY")


class EcoStatus:
    DRAFT = "DRAFT"                        # 草稿,可修改
    FROZEN = "FROZEN"                      # 已冻结,检查进行中
    BLOCKED = "BLOCKED"                    # 因模块重叠被排队阻塞
    READY_FOR_SIGNOFF = "READY_FOR_SIGNOFF"  # 全部必需检查通过
    SIGNOFF_PARTIAL = "SIGNOFF_PARTIAL"    # 已有一方签核
    SIGNOFF_COMPLETE = "SIGNOFF_COMPLETE"  # 双角色签核完成,可发布
    RELEASING = "RELEASING"                # 发布进行中(崩溃可恢复)
    RELEASED = "RELEASED"                  # 已形成新发布基线
    WITHDRAWN = "WITHDRAWN"
    REJECTED = "REJECTED"
    MERGED = "MERGED"                      # 被合并裁决并入其他 ECO


TERMINAL_ECO_STATUSES = frozenset(
    {EcoStatus.RELEASED, EcoStatus.WITHDRAWN, EcoStatus.REJECTED, EcoStatus.MERGED}
)

# 正在推进、会占用模块所有权的 ECO 状态(用于冲突检测)
ACTIVE_ECO_STATUSES = frozenset(
    {
        EcoStatus.FROZEN,
        EcoStatus.READY_FOR_SIGNOFF,
        EcoStatus.SIGNOFF_PARTIAL,
        EcoStatus.SIGNOFF_COMPLETE,
        EcoStatus.RELEASING,
    }
)


class RunStatus:
    PENDING = "PENDING"          # 等待依赖
    DISPATCHED = "DISPATCHED"    # 已派发(持有本次派发 token)
    PASSED = "PASSED"
    FAILED = "FAILED"
    INVALIDATED = "INVALIDATED"  # 被失效(绑定变化),结果不得复用


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(obj) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()[:16]


def module_hash(netlist: str) -> str:
    return hashlib.sha256(netlist.encode("utf-8")).hexdigest()[:16]


def snapshot_hash(changes: dict, affected_modules: list, matrix: list) -> str:
    """ECO 内容快照哈希:任何修订都会改变它,从而使旧检查/签核失效。"""
    return content_hash(
        {
            "changes": changes,
            "affected_modules": sorted(affected_modules),
            "matrix": matrix,
        }
    )


def normalize_modules(modules: dict) -> dict:
    """{name: {"netlist": str}} -> 补全 netlist_hash。"""
    out = {}
    for name, m in modules.items():
        netlist = m["netlist"]
        out[name] = {"netlist": netlist, "netlist_hash": module_hash(netlist)}
    return out


def deep_merge(base: dict, updates: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (updates or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def compose_baseline(base: dict, changes: dict):
    """在基线内容上应用 ECO 变更,返回 (modules, constraints)。

    changes 结构:
      module_updates:   {name: {"netlist": str}}
      module_removals:  [name]
      constraint_updates: {key: value} (深合并)
    """
    modules = copy.deepcopy(base["modules"])
    for name in changes.get("module_removals", []):
        modules.pop(name, None)
    for name, upd in changes.get("module_updates", {}).items():
        netlist = upd["netlist"]
        modules[name] = {"netlist": netlist, "netlist_hash": module_hash(netlist)}
    constraints = deep_merge(base["constraints"], changes.get("constraint_updates", {}))
    return modules, constraints


def flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in (d or {}).items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(flatten(v, key))
        else:
            out[key] = v
    return out
