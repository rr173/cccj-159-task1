"""基线差异包:两个基线之间的模块/约束/验证矩阵差异 + 溯源信息。"""

import json
import os
import zipfile
from datetime import datetime, timezone

from .domain import flatten
from .engine import DomainError
from .store import loads


def _baseline(store, baseline_id: str) -> dict:
    row = store.get("baselines", baseline_id)
    if not row:
        raise DomainError("not_found", f"基线 {baseline_id} 不存在")
    return row


def diff_baselines(store, from_id: str, to_id: str) -> dict:
    a, b = _baseline(store, from_id), _baseline(store, to_id)
    mod_a, mod_b = loads(a["modules"], {}), loads(b["modules"], {})
    added = sorted(set(mod_b) - set(mod_a))
    removed = sorted(set(mod_a) - set(mod_b))
    changed = [
        {"module": m, "old_hash": mod_a[m]["netlist_hash"],
         "new_hash": mod_b[m]["netlist_hash"]}
        for m in sorted(set(mod_a) & set(mod_b))
        if mod_a[m]["netlist_hash"] != mod_b[m]["netlist_hash"]
    ]

    ca, cb = flatten(loads(a["constraints"], {})), flatten(loads(b["constraints"], {}))
    con_changed = [
        {"key": k, "old": ca[k], "new": cb[k]}
        for k in sorted(set(ca) & set(cb)) if ca[k] != cb[k]
    ]
    con_added = [{"key": k, "new": cb[k]} for k in sorted(set(cb) - set(ca))]
    con_removed = [{"key": k, "old": ca[k]} for k in sorted(set(ca) - set(cb))]

    mx_a = {json.dumps(e, sort_keys=True) for e in loads(a["matrix"], [])}
    mx_b = {json.dumps(e, sort_keys=True) for e in loads(b["matrix"], [])}
    matrix_diff = {
        "added": [json.loads(x) for x in sorted(mx_b - mx_a)],
        "removed": [json.loads(x) for x in sorted(mx_a - mx_b)],
    }

    # 溯源:两个基线之间发布的 ECO、签核与检查证据
    lo, hi = min(a["seq"], b["seq"]), max(a["seq"], b["seq"])
    provenance = []
    for bl in store.find(
        "baselines", "seq>? AND seq<=? AND created_from_eco IS NOT NULL",
        (lo, hi), order="seq",
    ):
        eco = store.get("ecos", bl["created_from_eco"])
        signoffs = store.find(
            "signoffs", "eco_id=? AND revoked=0", (bl["created_from_eco"],))
        evidence = store.find(
            "check_runs", "eco_id=? AND status='PASSED'", (bl["created_from_eco"],))
        provenance.append({
            "baseline": bl["id"], "seq": bl["seq"],
            "eco": {"id": eco["id"], "title": eco["title"],
                    "snapshot_hash": eco["snapshot_hash"]} if eco else None,
            "signoffs": [
                {"role": s["role"], "user": s["user"], "at": s["created_at"]}
                for s in signoffs
            ],
            "check_evidence": [
                {"check": r["name"], "run": r["id"],
                 "snapshot_hash": r["snapshot_hash"],
                 "base_baseline": r["base_baseline_id"],
                 "constraint_epoch": r["constraint_epoch"],
                 "completed_at": r["completed_at"]}
                for r in evidence
            ],
        })

    return {
        "from": {"id": a["id"], "seq": a["seq"], "kind": a["kind"]},
        "to": {"id": b["id"], "seq": b["seq"], "kind": b["kind"]},
        "modules": {"added": added, "removed": removed, "changed": changed},
        "constraints": {"changed": con_changed, "added": con_added,
                        "removed": con_removed},
        "matrix": matrix_diff,
        "provenance": provenance,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def export_diff_package(store, from_id: str, to_id: str, out_dir: str) -> str:
    """导出差异包目录 + zip,返回 zip 路径。"""
    diff = diff_baselines(store, from_id, to_id)
    pkg_dir = os.path.join(
        out_dir, f"baseline_diff_{diff['from']['seq']}_to_{diff['to']['seq']}")
    os.makedirs(pkg_dir, exist_ok=True)
    with open(os.path.join(pkg_dir, "diff_package.json"), "w", encoding="utf-8") as f:
        json.dump(diff, f, ensure_ascii=False, indent=2)
    manifest = {
        "package": os.path.basename(pkg_dir),
        "from": diff["from"], "to": diff["to"],
        "files": ["diff_package.json"],
        "generated_at": diff["generated_at"],
    }
    with open(os.path.join(pkg_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    zip_path = pkg_dir + ".zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in ("diff_package.json", "manifest.json"):
            zf.write(os.path.join(pkg_dir, name),
                     arcname=f"{os.path.basename(pkg_dir)}/{name}")
    return zip_path
