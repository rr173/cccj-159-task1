"""签核工作台:聚合待办签核与单 ECO 仪表盘视图。"""

from .domain import SIGNOFF_ROLES, EcoStatus, RunStatus
from .store import loads


def _check_summary(engine, eco):
    runs = engine._current_runs(eco)
    by_name = {r["name"]: r for r in runs}
    out = []
    for entry in engine._entries(eco):
        r = by_name.get(entry["name"])
        out.append({
            "check": entry["name"],
            "status": r["status"] if r else "NOT_CREATED",
            "run_id": r["id"] if r else None,
            "token": r["token"] if r and r["status"] == RunStatus.DISPATCHED else None,
        })
    return out


def pending_signoffs(engine) -> list:
    """待办列表:等待签核 / 等待发布的 ECO。"""
    store = engine.store
    items = []
    for eco in store.find(
        "ecos",
        "status IN ('READY_FOR_SIGNOFF','SIGNOFF_PARTIAL','SIGNOFF_COMPLETE')",
        order="updated_at",
    ):
        active = store.find("signoffs", "eco_id=? AND revoked=0", (eco["id"],))
        roles = {s["role"] for s in active}
        items.append({
            "eco_id": eco["id"],
            "title": eco["title"],
            "status": eco["status"],
            "affected_modules": loads(eco["affected_modules"], []),
            "checks": _check_summary(engine, eco),
            "signoffs_done": [
                {"role": s["role"], "user": s["user"]} for s in active],
            "signoffs_pending": sorted(set(SIGNOFF_ROLES) - roles),
            "action": "RELEASE" if eco["status"] == EcoStatus.SIGNOFF_COMPLETE
                      else "SIGN",
        })
    return items


def eco_dashboard(engine, eco_id: str) -> dict:
    engine._eco(eco_id)  # 404 检查
    view = engine.get_eco_view(eco_id)
    readiness = engine.readiness(eco_id)
    events = engine.store.find(
        "events", "entity_id=?", (eco_id,), order="id DESC")[:20]
    invalidated = [r for r in view["runs"] if r["status"] == RunStatus.INVALIDATED]
    return {
        "eco": {k: view[k] for k in (
            "id", "title", "status", "base_baseline_id", "constraint_epoch",
            "snapshot_hash", "affected_modules", "blocked_by", "created_by")},
        "readiness": readiness,
        "checks": _check_summary(engine, engine._eco(eco_id)),
        "invalidated_run_count": len(invalidated),
        "invalidated_reasons": sorted(
            {r["invalidated_reason"] for r in invalidated} - {None}),
        "signoffs": view["signoffs"],
        "conflicts": view["conflicts"],
        "staleness": view["staleness"],
        "recent_events": [
            {"ts": e["ts"], "kind": e["kind"], "payload": loads(e["payload"], {})}
            for e in events
        ],
    }
