"""核心编排引擎。

不变量:
1. 检查结果绑定 (eco_id, snapshot_hash, base_baseline_id, constraint_epoch)。
   只有与 ECO 当前绑定完全一致的 PASSED 结果才计入签核就绪与发布判定。
2. 任何改变绑定的事件(ECO 修订、基础约束更新、基线推进)都会使旧检查
   运行与旧签核失效;迟到的结果一律拒绝并记入审计日志,绝不拼接复用。
3. 冲突(两个 ECO 修改重叠模块)必须显式裁决:queue(排队)/ merge(合并)/
   partition(拆分)/ reject(驳回)。
4. 发布两段式 + 发布日志;崩溃后 recover() 回滚到发布前状态,绝不产生
   半成品基线。
"""

import uuid
from datetime import datetime, timezone

from . import domain
from .domain import (
    ACTIVE_ECO_STATUSES,
    CHECK_TYPES,
    DEFAULT_DAG,
    SIGNOFF_ROLES,
    TERMINAL_ECO_STATUSES,
    EcoStatus,
    RunStatus,
)
from .store import dumps, loads


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class DomainError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class Engine:
    def __init__(self, store, dag=None):
        self.store = store
        self.dag = dag or dict(DEFAULT_DAG)

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------
    def _emit(self, event_kind: str, entity_id: str, **payload):
        self.store.insert(
            "events",
            {"ts": _now(), "kind": event_kind, "entity_id": entity_id,
             "payload": dumps(payload)},
        )

    def _eco(self, eco_id: str) -> dict:
        row = self.store.get("ecos", eco_id)
        if not row:
            raise DomainError("not_found", f"ECO {eco_id} 不存在")
        return row

    def _head(self) -> dict:
        head_id = self.store.meta_get("head_baseline_id")
        if not head_id:
            raise DomainError("no_baseline", "尚未建立初始基线,请先 seed")
        return self.store.get("baselines", head_id)

    def _epoch(self) -> int:
        return int(self.store.meta_get("constraint_epoch", "0"))

    @staticmethod
    def _binding(eco: dict):
        return (eco["snapshot_hash"], eco["base_baseline_id"], eco["constraint_epoch"])

    def _current_runs(self, eco: dict):
        snap, base, epoch = self._binding(eco)
        return self.store.find(
            "check_runs",
            "eco_id=? AND snapshot_hash=? AND base_baseline_id=? "
            "AND constraint_epoch=? AND status!=?",
            (eco["id"], snap, base, epoch, RunStatus.INVALIDATED),
        )

    @staticmethod
    def _entries(eco: dict):
        """验证矩阵展开为检查项 [{name, type, config}]。"""
        matrix = loads(eco["matrix"], []) or []
        total, seen, entries = {}, {}, []
        for e in matrix:
            total[e["type"]] = total.get(e["type"], 0) + 1
        for e in matrix:
            t = e["type"]
            seen[t] = seen.get(t, 0) + 1
            name = t if total[t] == 1 else f"{t}#{seen[t]}"
            entries.append({"name": name, "type": t, "config": e.get("config", {})})
        return entries

    # ------------------------------------------------------------------
    # 基线引导
    # ------------------------------------------------------------------
    def seed_baseline(self, modules: dict, constraints: dict, matrix: list, note: str = ""):
        if self.store.meta_get("head_baseline_id"):
            raise DomainError("invalid_state", "初始基线已存在")
        for e in matrix:
            if e["type"] not in CHECK_TYPES:
                raise DomainError("bad_matrix", f"未知检查类型 {e['type']}")
        with self.store.tx():
            bl = {
                "id": _id("bl"),
                "seq": 1,
                "parent_id": None,
                "constraint_epoch": self._epoch(),
                "modules": dumps(domain.normalize_modules(modules)),
                "constraints": dumps(constraints),
                "matrix": dumps(matrix),
                "created_from_eco": None,
                "kind": "SEED",
                "note": note,
                "status": "ACTIVE",
                "created_at": _now(),
            }
            self.store.insert("baselines", bl)
            self.store.meta_set("head_baseline_id", bl["id"])
            self._emit("baseline_created", bl["id"], kind="SEED", seq=1)
        return self.store.get("baselines", bl["id"])

    # ------------------------------------------------------------------
    # ECO 生命周期
    # ------------------------------------------------------------------
    def create_eco(
        self,
        title: str,
        changes: dict,
        affected_modules: list,
        matrix: list = None,
        created_by: str = "",
        conflict_policy: str = "queue",
        base_baseline_id: str = None,
    ):
        if not affected_modules:
            raise DomainError("bad_request", "affected_modules 不能为空")
        if conflict_policy not in ("queue", "merge"):
            raise DomainError("bad_request", "conflict_policy 须为 queue|merge")
        if matrix is not None:
            self._validate_matrix(matrix)
        base = (
            self.store.get("baselines", base_baseline_id) if base_baseline_id else self._head()
        )
        if not base:
            raise DomainError("not_found", "基准基线不存在")
        eco = {
            "id": _id("eco"),
            "title": title,
            "status": EcoStatus.DRAFT,
            "base_baseline_id": base["id"],
            "base_seq": base["seq"],
            "constraint_epoch": self._epoch(),
            "snapshot_hash": None,
            "changes": dumps(changes),
            "affected_modules": dumps(sorted(affected_modules)),
            "matrix": dumps(matrix) if matrix is not None else None,
            "conflict_policy": conflict_policy,
            "blocked_by": None,
            "dispatch_gen": 0,
            "created_by": created_by,
            "created_at": _now(),
            "updated_at": _now(),
        }
        with self.store.tx():
            self.store.insert("ecos", eco)
            self._emit(
                "eco_created", eco["id"],
                title=title, base=base["id"], modules=sorted(affected_modules),
            )
        return self._eco(eco["id"])

    @staticmethod
    def _validate_matrix(matrix):
        if not matrix:
            raise DomainError("bad_matrix", "验证矩阵不能为空")
        for e in matrix:
            if e.get("type") not in CHECK_TYPES:
                raise DomainError("bad_matrix", f"未知检查类型 {e.get('type')}")

    def freeze_eco(self, eco_id: str):
        """冻结网表/约束/受影响模块/验证矩阵,计算快照并按依赖图触发检查。

        若与推进中的 ECO 存在模块重叠,则进入 BLOCKED 排队并登记冲突。
        """
        eco = self._eco(eco_id)
        if eco["status"] != EcoStatus.DRAFT:
            raise DomainError("invalid_state", f"当前状态 {eco['status']} 不能冻结")
        with self.store.tx():
            eco = self._eco(eco_id)
            head = self._head()
            updates = {"updated_at": _now()}
            if eco["base_baseline_id"] != head["id"]:
                updates.update(
                    base_baseline_id=head["id"],
                    base_seq=head["seq"],
                    constraint_epoch=self._epoch(),
                )
                self._emit("eco_rebased", eco_id, to_baseline=head["id"], reason="freeze")
            matrix = loads(eco["matrix"]) or loads(head["matrix"], [])
            self._validate_matrix(matrix)
            changes = loads(eco["changes"], {})
            modules = loads(eco["affected_modules"], [])
            snap = domain.snapshot_hash(changes, modules, matrix)
            updates.update(snapshot_hash=snap, matrix=dumps(matrix))
            self.store.update("ecos", eco_id, updates)
            eco = self._eco(eco_id)

            # 旧的未决冲突记录在重新评估前先标记作废
            for cf in self.store.find(
                "conflicts", "status='PENDING' AND (eco_a=? OR eco_b=?)", (eco_id, eco_id)
            ):
                self.store.update(
                    "conflicts", cf["id"],
                    {"status": "RESOLVED", "resolution": "superseded_by_refreeze",
                     "resolved_at": _now()},
                )

            blockers = self._find_overlaps(eco)
            if blockers:
                first = blockers[0]
                self.store.update(
                    "ecos", eco_id,
                    {"status": EcoStatus.BLOCKED, "blocked_by": first["id"],
                     "updated_at": _now()},
                )
                for other in blockers:
                    overlap = sorted(
                        set(modules) & set(loads(other["affected_modules"], []))
                    )
                    self.store.insert(
                        "conflicts",
                        {"id": _id("cf"), "eco_a": other["id"], "eco_b": eco_id,
                         "modules": dumps(overlap), "status": "PENDING",
                         "resolution": None, "created_at": _now(),
                         "resolved_at": None, "resolved_by": None},
                    )
                    self._emit("conflict_detected", eco_id,
                               other=other["id"], overlap=overlap)
                return self._eco(eco_id)

            self.store.update(
                "ecos", eco_id,
                {"status": EcoStatus.FROZEN, "blocked_by": None, "updated_at": _now()},
            )
            self._emit("eco_frozen", eco_id, snapshot=snap, base=head["id"],
                       epoch=eco["constraint_epoch"])
            eco = self._eco(eco_id)
            self._create_runs(eco)
            self._maybe_dispatch(eco)
        return self._eco(eco_id)

    def _find_overlaps(self, eco: dict):
        modules = set(loads(eco["affected_modules"], []))
        out = []
        candidates = self.store.find(
            "ecos",
            "status IN ('FROZEN','READY_FOR_SIGNOFF','SIGNOFF_PARTIAL',"
            "'SIGNOFF_COMPLETE','RELEASING','BLOCKED')",
            order="created_at",
        )
        for other in candidates:
            if other["id"] == eco["id"]:
                continue
            if modules & set(loads(other["affected_modules"], [])):
                out.append(other)
        return out

    def amend_eco(self, eco_id: str, changes=None, affected_modules=None, matrix=None):
        """修订 ECO:旧检查运行与签核全部失效,回到 DRAFT 需重新冻结。"""
        eco = self._eco(eco_id)
        allowed = {
            EcoStatus.DRAFT, EcoStatus.FROZEN, EcoStatus.BLOCKED,
            EcoStatus.READY_FOR_SIGNOFF, EcoStatus.SIGNOFF_PARTIAL,
            EcoStatus.SIGNOFF_COMPLETE,
        }
        if eco["status"] not in allowed:
            raise DomainError("invalid_state", f"状态 {eco['status']} 不允许修订")
        if matrix is not None:
            self._validate_matrix(matrix)
        with self.store.tx():
            eco = self._eco(eco_id)
            updates = {"updated_at": _now()}
            if changes is not None:
                updates["changes"] = dumps(changes)
            if affected_modules is not None:
                if not affected_modules:
                    raise DomainError("bad_request", "affected_modules 不能为空")
                updates["affected_modules"] = dumps(sorted(affected_modules))
            if matrix is not None:
                updates["matrix"] = dumps(matrix)
            self.store.update("ecos", eco_id, updates)
            eco = self._eco(eco_id)
            self._invalidate_locked(eco, "eco_amended")
            self.store.update(
                "ecos", eco_id,
                {"status": EcoStatus.DRAFT, "blocked_by": None, "updated_at": _now()},
            )
            self._emit("eco_amended", eco_id)
        return self._eco(eco_id)

    def withdraw_eco(self, eco_id: str):
        eco = self._eco(eco_id)
        if eco["status"] in TERMINAL_ECO_STATUSES or eco["status"] == EcoStatus.RELEASING:
            raise DomainError("invalid_state", f"状态 {eco['status']} 不能撤回")
        with self.store.tx():
            eco = self._eco(eco_id)
            self._invalidate_locked(eco, "eco_withdrawn")
            self.store.update(
                "ecos", eco_id,
                {"status": EcoStatus.WITHDRAWN, "updated_at": _now()},
            )
            self._emit("eco_withdrawn", eco_id)
            unblocked = self._release_queue_locked(eco_id)
        for uid in unblocked:  # 事务外自动重冻结
            self.freeze_eco(uid)
        return self._eco(eco_id)

    # ------------------------------------------------------------------
    # 检查调度与结果回收
    # ------------------------------------------------------------------
    def _create_runs(self, eco: dict):
        snap, base, epoch = self._binding(eco)
        required_types = {e["type"] for e in self._entries(eco)}
        for entry in self._entries(eco):
            deps = [
                d for d in self.dag.get(entry["type"], [])
                if d in required_types and d != entry["type"]
            ]
            self.store.insert(
                "check_runs",
                {"id": _id("run"), "eco_id": eco["id"], "check_type": entry["type"],
                 "name": entry["name"], "status": RunStatus.PENDING,
                 "snapshot_hash": snap, "base_baseline_id": base,
                 "constraint_epoch": epoch, "token": None, "generation": None,
                 "depends_on": dumps(deps), "result": None,
                 "dispatched_at": None, "completed_at": None,
                 "invalidated_reason": None},
            )

    def _dep_satisfied(self, eco: dict, dep_type: str) -> bool:
        runs = [r for r in self._current_runs(eco) if r["check_type"] == dep_type]
        return bool(runs) and all(r["status"] == RunStatus.PASSED for r in runs)

    def _maybe_dispatch(self, eco: dict):
        for run in self._current_runs(eco):
            if run["status"] != RunStatus.PENDING:
                continue
            deps = loads(run["depends_on"], [])
            if all(self._dep_satisfied(eco, d) for d in deps):
                gen = eco["dispatch_gen"] + 1
                self.store.update("ecos", eco["id"], {"dispatch_gen": gen})
                eco = self._eco(eco["id"])
                token = uuid.uuid4().hex
                self.store.update(
                    "check_runs", run["id"],
                    {"status": RunStatus.DISPATCHED, "token": token,
                     "generation": gen, "dispatched_at": _now()},
                )
                self._emit("check_dispatched", eco["id"], run=run["id"],
                           check=run["name"], token=token, generation=gen)

    def submit_check_result(self, eco_id: str, run_id: str, token: str,
                            passed: bool, details: dict = None):
        """回收检查结果。任何与当前绑定不符的(迟到的)结果一律拒绝。"""
        eco = self._eco(eco_id)
        run = self.store.get("check_runs", run_id)
        if not run or run["eco_id"] != eco_id:
            raise DomainError("not_found", f"检查运行 {run_id} 不存在")

        stale_reason = None
        if run["status"] != RunStatus.DISPATCHED:
            stale_reason = f"运行状态为 {run['status']},非 DISPATCHED"
        elif run["token"] != token:
            stale_reason = "派发 token 不匹配"
        elif (run["snapshot_hash"], run["base_baseline_id"], run["constraint_epoch"]) \
                != self._binding(eco):
            stale_reason = "结果绑定(快照/基线/约束纪元)与 ECO 当前绑定不一致"
        elif eco["status"] not in (
            EcoStatus.FROZEN, EcoStatus.READY_FOR_SIGNOFF,
            EcoStatus.SIGNOFF_PARTIAL, EcoStatus.SIGNOFF_COMPLETE,
        ):
            stale_reason = f"ECO 状态 {eco['status']} 不接收结果"
        if stale_reason:
            self._emit("stale_result_rejected", eco_id, run=run_id,
                       check=run["name"], reason=stale_reason)
            raise DomainError("stale_result", f"迟到/失效结果被拒绝:{stale_reason}")

        with self.store.tx():
            eco = self._eco(eco_id)
            status = RunStatus.PASSED if passed else RunStatus.FAILED
            self.store.update(
                "check_runs", run_id,
                {"status": status, "result": dumps(details or {}),
                 "completed_at": _now()},
            )
            self._emit("check_result_accepted", eco_id, run=run_id,
                       check=run["name"], passed=passed)
            if passed:
                self._maybe_dispatch(eco)
                eco = self._eco(eco_id)
                if self._all_green(eco) and eco["status"] == EcoStatus.FROZEN:
                    self.store.update(
                        "ecos", eco_id,
                        {"status": EcoStatus.READY_FOR_SIGNOFF, "updated_at": _now()},
                    )
                    self._emit("eco_ready_for_signoff", eco_id)
            else:
                # 任一检查失败:签核基础不复存在,撤销已有签核
                self._revoke_signoffs_locked(eco, "check_failed")
                if eco["status"] in (
                    EcoStatus.READY_FOR_SIGNOFF, EcoStatus.SIGNOFF_PARTIAL,
                    EcoStatus.SIGNOFF_COMPLETE,
                ):
                    self.store.update(
                        "ecos", eco_id,
                        {"status": EcoStatus.FROZEN, "updated_at": _now()},
                    )
                self._emit("eco_check_failed", eco_id, check=run["name"])
        return self.store.get("check_runs", run_id)

    def _all_green(self, eco: dict) -> bool:
        runs = self._current_runs(eco)
        entries = self._entries(eco)
        return (
            len(runs) == len(entries)
            and all(r["status"] == RunStatus.PASSED for r in runs)
        )

    # ------------------------------------------------------------------
    # 签核
    # ------------------------------------------------------------------
    def sign_off(self, eco_id: str, role: str, user: str):
        eco = self._eco(eco_id)
        if eco["status"] not in (EcoStatus.READY_FOR_SIGNOFF, EcoStatus.SIGNOFF_PARTIAL):
            raise DomainError("invalid_state", f"状态 {eco['status']} 不能签核")
        if role not in SIGNOFF_ROLES:
            raise DomainError("bad_request", f"角色须为 {SIGNOFF_ROLES}")
        if not user:
            raise DomainError("bad_request", "签核人不能为空")
        with self.store.tx():
            eco = self._eco(eco_id)
            active = self.store.find(
                "signoffs", "eco_id=? AND revoked=0", (eco_id,))
            if any(s["role"] == role for s in active):
                raise DomainError("duplicate_signoff", f"角色 {role} 已签核")
            if any(s["user"] == user for s in active):
                raise DomainError("same_user", "双角色签核必须由不同人完成")
            self.store.insert(
                "signoffs",
                {"id": _id("sf"), "eco_id": eco_id, "role": role, "user": user,
                 "snapshot_hash": eco["snapshot_hash"], "created_at": _now(),
                 "revoked": 0, "revoke_reason": None},
            )
            self._emit("signoff_added", eco_id, role=role, user=user)
            roles = {s["role"] for s in active} | {role}
            new_status = (
                EcoStatus.SIGNOFF_COMPLETE
                if roles == set(SIGNOFF_ROLES)
                else EcoStatus.SIGNOFF_PARTIAL
            )
            self.store.update(
                "ecos", eco_id, {"status": new_status, "updated_at": _now()})
            if new_status == EcoStatus.SIGNOFF_COMPLETE:
                self._emit("eco_signoff_complete", eco_id)
        return self._eco(eco_id)

    # ------------------------------------------------------------------
    # 发布(两段式,可恢复)
    # ------------------------------------------------------------------
    def release(self, eco_id: str, _crash_hook=None):
        """形成新发布基线。_crash_hook(phase) 仅用于测试注入崩溃。"""
        eco = self._eco(eco_id)
        self._release_precheck(eco)
        target_id = _id("bl")

        # 阶段 1:登记发布意向(崩溃后 recover 可识别)
        with self.store.tx():
            with self.store.lock:
                self.store.conn.execute(
                    "DELETE FROM release_journal WHERE eco_id=?", (eco_id,))
            self.store.insert(
                "release_journal",
                {"eco_id": eco_id, "target_baseline_id": target_id,
                 "state": "INTENT", "started_at": _now(), "finished_at": None},
            )
            self.store.update(
                "ecos", eco_id,
                {"status": EcoStatus.RELEASING, "updated_at": _now()})
            self._emit("release_started", eco_id, target=target_id)
        if _crash_hook:
            _crash_hook("after_intent")

        # 阶段 2:单事务生成新基线(崩溃则整体回滚,不留半成品)
        eco = self._eco(eco_id)
        head = self._head()
        with self.store.tx():
            modules, constraints = domain.compose_baseline(
                {"modules": loads(head["modules"], {}),
                 "constraints": loads(head["constraints"], {})},
                loads(eco["changes"], {}),
            )
            bl = {
                "id": target_id, "seq": head["seq"] + 1, "parent_id": head["id"],
                "constraint_epoch": self._epoch(),
                "modules": dumps(modules), "constraints": dumps(constraints),
                "matrix": eco["matrix"], "created_from_eco": eco_id,
                "kind": "ECO_RELEASE", "note": eco["title"], "status": "ACTIVE",
                "created_at": _now(),
            }
            if _crash_hook:
                _crash_hook("inside_commit")
            self.store.insert("baselines", bl)
            self.store.update("baselines", head["id"], {"status": "SUPERSEDED"})
            self.store.meta_set("head_baseline_id", target_id)
            self.store.update(
                "ecos", eco_id,
                {"status": EcoStatus.RELEASED, "updated_at": _now()})
            self.store.update(
                "release_journal", eco_id,
                {"state": "COMMITTED", "finished_at": _now()}, id_col="eco_id")
            self._emit("baseline_created", target_id, kind="ECO_RELEASE",
                       seq=bl["seq"], eco=eco_id)
            self._emit("release_committed", eco_id, baseline=target_id)
        if _crash_hook:
            _crash_hook("after_commit")

        # 级联:其余在途 ECO 的基线已变化,旧结果一律失效
        self._post_release_cascade(self._eco(eco_id), self.store.get("baselines", target_id))
        return self.store.get("baselines", target_id)

    def _release_precheck(self, eco: dict):
        if eco["status"] != EcoStatus.SIGNOFF_COMPLETE:
            raise DomainError("invalid_state", "双角色签核未完成,不能发布")
        head = self._head()
        if eco["base_baseline_id"] != head["id"]:
            raise DomainError(
                "stale_base",
                f"ECO 基于 {eco['base_baseline_id']},当前头基线为 {head['id']},"
                "需重新冻结(rebase)",
            )
        runs = self._current_runs(eco)
        by_name = {r["name"]: r for r in runs}
        for entry in self._entries(eco):
            r = by_name.get(entry["name"])
            if not r or r["status"] != RunStatus.PASSED:
                raise DomainError(
                    "checks_not_green",
                    f"检查 {entry['name']} 未针对当前基线绑定通过",
                )
        for r in runs:
            if r["status"] in (RunStatus.PENDING, RunStatus.DISPATCHED):
                raise DomainError("checks_in_flight", f"检查 {r['name']} 仍在进行")
        active = self.store.find("signoffs", "eco_id=? AND revoked=0", (eco["id"],))
        if {s["role"] for s in active} != set(SIGNOFF_ROLES):
            raise DomainError("signoff_incomplete", "双角色签核不完整")
        if any(s["snapshot_hash"] != eco["snapshot_hash"] for s in active):
            raise DomainError("signoff_stale", "签核针对的是旧快照")
        pending = self.store.find(
            "conflicts",
            "status='PENDING' AND (eco_a=? OR eco_b=?)", (eco["id"], eco["id"]))
        if pending:
            raise DomainError("unresolved_conflict", "存在未裁决的模块冲突")

    def _post_release_cascade(self, released: dict, new_head: dict):
        to_refreeze = []
        with self.store.tx():
            open_status = (
                "'DRAFT','FROZEN','BLOCKED','READY_FOR_SIGNOFF',"
                "'SIGNOFF_PARTIAL','SIGNOFF_COMPLETE'"
            )
            for x in self.store.find("ecos", f"status IN ({open_status})"):
                if x["id"] == released["id"]:
                    continue
                if x["status"] in ACTIVE_ECO_STATUSES:
                    # 基线已推进:旧结果不得拼接到新基线上
                    self._invalidate_locked(x, "base_changed")
                    self._rebase_locked(x, new_head, EcoStatus.DRAFT)
                else:
                    self._rebase_locked(x, new_head, x["status"])
            to_refreeze = self._release_queue_locked(released["id"])
        for eco_id in to_refreeze:
            self.freeze_eco(eco_id)  # 队列中的 ECO 自动在新基线上重冻结

    def _release_queue_locked(self, terminal_eco_id: str):
        """某 ECO 终结后,释放排在它后面的 ECO。返回待重冻结的 eco id。"""
        released = []
        for x in self.store.find("ecos", "blocked_by=?", (terminal_eco_id,)):
            for cf in self.store.find(
                "conflicts", "status='PENDING' AND eco_b=?", (x["id"],)
            ):
                self.store.update(
                    "conflicts", cf["id"],
                    {"status": "RESOLVED", "resolution": "queue_satisfied",
                     "resolved_at": _now(), "resolved_by": "system"})
            self.store.update(
                "ecos", x["id"],
                {"status": EcoStatus.DRAFT, "blocked_by": None, "updated_at": _now()})
            self._emit("eco_unblocked", x["id"], by=terminal_eco_id)
            released.append(x["id"])
        return released

    def _rebase_locked(self, eco: dict, new_head: dict, to_status: str):
        self.store.update(
            "ecos", eco["id"],
            {"base_baseline_id": new_head["id"], "base_seq": new_head["seq"],
             "constraint_epoch": self._epoch(), "status": to_status,
             "updated_at": _now()},
        )
        self._emit("eco_rebased", eco["id"], to_baseline=new_head["id"],
                   reason="head_moved")

    def recover(self):
        """启动/运维恢复:回滚所有未完成的发布,绝不留下半成品基线。"""
        recovered = []
        with self.store.tx():
            for j in self.store.find("release_journal", "state='INTENT'"):
                eco = self.store.get("ecos", j["eco_id"])
                if eco and eco["status"] == EcoStatus.RELEASING:
                    self.store.update(
                        "ecos", eco["id"],
                        {"status": EcoStatus.SIGNOFF_COMPLETE, "updated_at": _now()})
                self.store.update(
                    "release_journal", j["eco_id"],
                    {"state": "ROLLED_BACK", "finished_at": _now()}, id_col="eco_id")
                self._emit("release_rolled_back", j["eco_id"])
                recovered.append(j["eco_id"])
        return recovered

    # ------------------------------------------------------------------
    # 基础约束更新(约束纪元推进,全部在途结果失效)
    # ------------------------------------------------------------------
    def update_base_constraints(self, changes: dict, note: str = ""):
        head = self._head()
        with self.store.tx():
            epoch = self._epoch() + 1
            self.store.meta_set("constraint_epoch", str(epoch))
            constraints = domain.deep_merge(loads(head["constraints"], {}), changes)
            bl = {
                "id": _id("bl"), "seq": head["seq"] + 1, "parent_id": head["id"],
                "constraint_epoch": epoch, "modules": head["modules"],
                "constraints": dumps(constraints), "matrix": head["matrix"],
                "created_from_eco": None, "kind": "CONSTRAINT_UPDATE",
                "note": note, "status": "ACTIVE", "created_at": _now(),
            }
            self.store.insert("baselines", bl)
            self.store.update("baselines", head["id"], {"status": "SUPERSEDED"})
            self.store.meta_set("head_baseline_id", bl["id"])
            self._emit("constraints_updated", bl["id"], epoch=epoch,
                       changes=changes, note=note)
            self._emit("baseline_created", bl["id"], kind="CONSTRAINT_UPDATE",
                       seq=bl["seq"])
            new_head = self.store.get("baselines", bl["id"])
            open_status = (
                "'DRAFT','FROZEN','BLOCKED','READY_FOR_SIGNOFF',"
                "'SIGNOFF_PARTIAL','SIGNOFF_COMPLETE'"
            )
            for x in self.store.find("ecos", f"status IN ({open_status})"):
                if x["status"] in ACTIVE_ECO_STATUSES:
                    self._invalidate_locked(x, "constraint_epoch_changed")
                # 排队中的 ECO 一并释放:约束已变,原排队依据作废,重新冻结排队
                if x["status"] == EcoStatus.BLOCKED:
                    self._resolve_conflicts_locked(x["id"], "constraint_reset")
                    self.store.update(
                        "ecos", x["id"], {"blocked_by": None, "updated_at": _now()})
                self._rebase_locked(x, new_head, EcoStatus.DRAFT)
        return new_head

    def _resolve_conflicts_locked(self, eco_id: str, resolution: str):
        for cf in self.store.find(
            "conflicts", "status='PENDING' AND (eco_a=? OR eco_b=?)", (eco_id, eco_id)
        ):
            self.store.update(
                "conflicts", cf["id"],
                {"status": "RESOLVED", "resolution": resolution,
                 "resolved_at": _now(), "resolved_by": "system"})

    # ------------------------------------------------------------------
    # 冲突裁决
    # ------------------------------------------------------------------
    def adjudicate(self, conflict_id: str, resolution: str, by: str, params: dict = None):
        cf = self.store.get("conflicts", conflict_id)
        if not cf:
            raise DomainError("not_found", f"冲突 {conflict_id} 不存在")
        if cf["status"] != "PENDING":
            raise DomainError("invalid_state", "冲突已裁决")
        params = params or {}
        refreeze = []
        with self.store.tx():
            a = self._eco(cf["eco_a"])
            b = self._eco(cf["eco_b"])
            if resolution == "queue":
                # 显式确认排队:b 继续阻塞在 a 之后
                self.store.update(
                    "ecos", b["id"],
                    {"status": EcoStatus.BLOCKED, "blocked_by": a["id"],
                     "updated_at": _now()})
            elif resolution == "merge":
                # 合并裁决:b 的变更并入 a,b 终结
                merged = self._merge_changes(
                    loads(a["changes"], {}), loads(b["changes"], {}))
                modules = sorted(
                    set(loads(a["affected_modules"], []))
                    | set(loads(b["affected_modules"], [])))
                self.store.update(
                    "ecos", a["id"],
                    {"changes": dumps(merged), "affected_modules": dumps(modules),
                     "updated_at": _now()})
                a = self._eco(a["id"])
                self._invalidate_locked(a, "eco_merged")
                self.store.update(
                    "ecos", a["id"],
                    {"status": EcoStatus.DRAFT, "updated_at": _now()})
                self.store.update(
                    "ecos", b["id"],
                    {"status": EcoStatus.MERGED, "blocked_by": None,
                     "updated_at": _now()})
                self._emit("eco_merged", b["id"], into=a["id"])
                refreeze = self._release_queue_locked(b["id"])
            elif resolution == "partition":
                new_modules = sorted(params.get("modules", []))
                if not new_modules:
                    raise DomainError("bad_request", "partition 需提供 modules")
                overlap = set(new_modules) & set(loads(a["affected_modules"], []))
                if overlap:
                    raise DomainError(
                        "bad_request", f"拆分后仍与 {a['id']} 重叠:{sorted(overlap)}")
                changes = loads(b["changes"], {})
                changes["module_updates"] = {
                    k: v for k, v in changes.get("module_updates", {}).items()
                    if k in new_modules
                }
                changes["module_removals"] = [
                    m for m in changes.get("module_removals", []) if m in new_modules
                ]
                self.store.update(
                    "ecos", b["id"],
                    {"affected_modules": dumps(new_modules), "changes": dumps(changes),
                     "status": EcoStatus.DRAFT, "blocked_by": None,
                     "updated_at": _now()})
                self._emit("eco_partitioned", b["id"], modules=new_modules)
            elif resolution == "reject":
                self._invalidate_locked(b, "eco_rejected")
                self.store.update(
                    "ecos", b["id"],
                    {"status": EcoStatus.REJECTED, "blocked_by": None,
                     "updated_at": _now()})
                self._emit("eco_rejected", b["id"], by=by)
                refreeze = self._release_queue_locked(b["id"])
            else:
                raise DomainError("bad_request", "resolution 须为 queue|merge|partition|reject")
            self.store.update(
                "conflicts", conflict_id,
                {"status": "RESOLVED", "resolution": resolution,
                 "resolved_at": _now(), "resolved_by": by})
            self._emit("conflict_adjudicated", conflict_id,
                       resolution=resolution, by=by)
        for eco_id in refreeze:
            self.freeze_eco(eco_id)
        return self.store.get("conflicts", conflict_id)

    @staticmethod
    def _merge_changes(a: dict, b: dict) -> dict:
        """b 并入 a:同模块更新以 b 为准(裁决结果),其余取并集。"""
        out = {
            "module_updates": dict(a.get("module_updates", {})),
            "module_removals": list(a.get("module_removals", [])),
            "constraint_updates": dict(a.get("constraint_updates", {})),
        }
        out["module_updates"].update(b.get("module_updates", {}))
        for m in b.get("module_removals", []):
            if m not in out["module_removals"]:
                out["module_removals"].append(m)
        out["constraint_updates"].update(b.get("constraint_updates", {}))
        return out

    # ------------------------------------------------------------------
    # 失效处理
    # ------------------------------------------------------------------
    def _invalidate_locked(self, eco: dict, reason: str):
        runs = self.store.find(
            "check_runs", "eco_id=? AND status!=?", (eco["id"], RunStatus.INVALIDATED))
        for r in runs:
            self.store.update(
                "check_runs", r["id"],
                {"status": RunStatus.INVALIDATED, "invalidated_reason": reason})
        if runs:
            self._emit("checks_invalidated", eco["id"],
                       reason=reason, count=len(runs))
        self._revoke_signoffs_locked(eco, reason)

    def _revoke_signoffs_locked(self, eco: dict, reason: str):
        sos = self.store.find(
            "signoffs", "eco_id=? AND revoked=0", (eco["id"],))
        for s in sos:
            self.store.update(
                "signoffs", s["id"], {"revoked": 1, "revoke_reason": reason})
        if sos:
            self._emit("signoffs_revoked", eco["id"],
                       reason=reason, count=len(sos))

    # ------------------------------------------------------------------
    # 查询视图
    # ------------------------------------------------------------------
    def get_eco_view(self, eco_id: str) -> dict:
        eco = self._eco(eco_id)
        snap, base, epoch = self._binding(eco)
        runs = self.store.find("check_runs", "eco_id=?", (eco_id,), order="rowid")
        for r in runs:
            r["depends_on"] = loads(r["depends_on"], [])
            r["result"] = loads(r["result"])
            r["is_current_binding"] = (
                r["status"] != RunStatus.INVALIDATED
                and (r["snapshot_hash"], r["base_baseline_id"], r["constraint_epoch"])
                == (snap, base, epoch)
            )
        signoffs = self.store.find("signoffs", "eco_id=?", (eco_id,))
        conflicts = self.store.find(
            "conflicts", "eco_a=? OR eco_b=?", (eco_id, eco_id))
        for cf in conflicts:
            cf["modules"] = loads(cf["modules"], [])
        head = self._head()
        view = dict(eco)
        view.update(
            changes=loads(eco["changes"], {}),
            affected_modules=loads(eco["affected_modules"], []),
            matrix=loads(eco["matrix"], None),
            runs=runs,
            signoffs=signoffs,
            conflicts=conflicts,
            staleness={
                "head_baseline": head["id"],
                "base_is_head": eco["base_baseline_id"] == head["id"],
                "current_epoch": self._epoch(),
                "epoch_is_current": eco["constraint_epoch"] == self._epoch(),
            },
        )
        return view

    def readiness(self, eco_id: str) -> dict:
        eco = self._eco(eco_id)
        entries = self._entries(eco)
        runs = {r["name"]: r for r in self._current_runs(eco)}
        checks = [
            {"name": e["name"], "type": e["type"],
             "status": runs.get(e["name"], {}).get("status", "NOT_CREATED")}
            for e in entries
        ]
        active_roles = {
            s["role"] for s in self.store.find(
                "signoffs", "eco_id=? AND revoked=0", (eco_id,))
        }
        return {
            "eco_id": eco_id,
            "status": eco["status"],
            "checks": checks,
            "all_checks_green": bool(checks) and all(
                c["status"] == RunStatus.PASSED for c in checks),
            "signoffs_done": sorted(active_roles),
            "signoffs_pending": sorted(set(SIGNOFF_ROLES) - active_roles),
            "releasable": eco["status"] == EcoStatus.SIGNOFF_COMPLETE,
        }
