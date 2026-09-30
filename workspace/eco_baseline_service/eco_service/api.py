"""REST API 层(stdlib http.server,零依赖)。

Api.handle(method, path, body) 可直接单测;HTTPServer 只是薄适配。
"""

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import diff as diff_mod
from . import workbench as wb
from .engine import DomainError, Engine
from .store import loads


class Api:
    def __init__(self, engine: Engine):
        self.e = engine
        self.routes = [
            ("GET", r"/", self.info),
            ("POST", r"/baselines/seed", self.seed),
            ("GET", r"/baselines", self.list_baselines),
            ("GET", r"/baselines/(?P<id>[^/]+)", self.get_baseline),
            ("GET", r"/baselines/(?P<a>[^/]+)/diff/(?P<b>[^/]+)", self.diff),
            ("POST", r"/baselines/(?P<a>[^/]+)/diff/(?P<b>[^/]+)/package",
             self.diff_package),
            ("POST", r"/constraints/update", self.constraints_update),
            ("POST", r"/ecos", self.create_eco),
            ("GET", r"/ecos", self.list_ecos),
            ("GET", r"/ecos/(?P<id>[^/]+)", self.get_eco),
            ("POST", r"/ecos/(?P<id>[^/]+)/freeze", self.freeze),
            ("POST", r"/ecos/(?P<id>[^/]+)/amend", self.amend),
            ("POST", r"/ecos/(?P<id>[^/]+)/withdraw", self.withdraw),
            ("GET", r"/ecos/(?P<id>[^/]+)/readiness", self.readiness),
            ("POST", r"/ecos/(?P<id>[^/]+)/checks/(?P<run>[^/]+)/results",
             self.submit_result),
            ("POST", r"/ecos/(?P<id>[^/]+)/signoffs", self.signoff),
            ("POST", r"/ecos/(?P<id>[^/]+)/release", self.release),
            ("GET", r"/conflicts", self.list_conflicts),
            ("POST", r"/conflicts/(?P<id>[^/]+)/adjudicate", self.adjudicate),
            ("GET", r"/workbench/pending", self.workbench_pending),
            ("GET", r"/workbench/ecos/(?P<id>[^/]+)", self.workbench_eco),
            ("GET", r"/events", self.events),
            ("POST", r"/admin/recover", self.recover),
        ]

    # ---- 路由 ----
    def handle(self, method: str, path: str, body: dict = None, query: dict = None):
        try:
            for m, pattern, fn in self.routes:
                if m != method:
                    continue
                match = re.fullmatch(pattern, path)
                if match:
                    return 200, fn(body or {}, query or {}, **match.groupdict())
            return 404, {"error": "route_not_found", "path": path}
        except DomainError as e:
            status = 404 if e.code == "not_found" else 409
            return status, {"error": e.code, "message": str(e)}
        except (KeyError, TypeError, ValueError) as e:
            return 400, {"error": "bad_request", "message": str(e)}

    # ---- 处理器 ----
    def info(self, body, q):
        return {"service": "eco-baseline-service", "version": "1.0"}

    def seed(self, body, q):
        return self.e.seed_baseline(
            body["modules"], body["constraints"], body["matrix"],
            body.get("note", ""))

    def list_baselines(self, body, q):
        rows = self.e.store.find("baselines", order="seq")
        for r in rows:
            r["modules"] = loads(r["modules"], {})
            r["constraints"] = loads(r["constraints"], {})
            r["matrix"] = loads(r["matrix"], [])
        return {"baselines": rows}

    def get_baseline(self, body, q, id):
        row = self.e.store.get("baselines", id)
        if not row:
            raise DomainError("not_found", f"基线 {id} 不存在")
        row["modules"] = loads(row["modules"], {})
        row["constraints"] = loads(row["constraints"], {})
        row["matrix"] = loads(row["matrix"], [])
        return row

    def diff(self, body, q, a, b):
        return diff_mod.diff_baselines(self.e.store, a, b)

    def diff_package(self, body, q, a, b):
        out_dir = body.get("out_dir", "/tmp")
        path = diff_mod.export_diff_package(self.e.store, a, b, out_dir)
        return {"package": path}

    def constraints_update(self, body, q):
        return self.e.update_base_constraints(
            body.get("changes", {}), body.get("note", ""))

    def create_eco(self, body, q):
        return self.e.create_eco(
            title=body["title"], changes=body.get("changes", {}),
            affected_modules=body["affected_modules"],
            matrix=body.get("matrix"), created_by=body.get("created_by", ""),
            conflict_policy=body.get("conflict_policy", "queue"),
            base_baseline_id=body.get("base_baseline_id"))

    def list_ecos(self, body, q):
        status = (q.get("status") or [None])[0]
        rows = self.e.store.find(
            "ecos", "status=?" if status else "", (status,) if status else (),
            order="created_at")
        for r in rows:
            r["changes"] = loads(r["changes"], {})
            r["affected_modules"] = loads(r["affected_modules"], [])
            r["matrix"] = loads(r["matrix"], None)
        return {"ecos": rows}

    def get_eco(self, body, q, id):
        return self.e.get_eco_view(id)

    def freeze(self, body, q, id):
        return self.e.freeze_eco(id)

    def amend(self, body, q, id):
        return self.e.amend_eco(
            id, changes=body.get("changes"),
            affected_modules=body.get("affected_modules"),
            matrix=body.get("matrix"))

    def withdraw(self, body, q, id):
        return self.e.withdraw_eco(id)

    def readiness(self, body, q, id):
        return self.e.readiness(id)

    def submit_result(self, body, q, id, run):
        return self.e.submit_check_result(
            id, run, body["token"], bool(body["passed"]),
            body.get("details"))

    def signoff(self, body, q, id):
        return self.e.sign_off(id, body["role"], body["user"])

    def release(self, body, q, id):
        return self.e.release(id)

    def list_conflicts(self, body, q):
        status = (q.get("status") or [None])[0]
        rows = self.e.store.find(
            "conflicts", "status=?" if status else "",
            (status,) if status else (), order="created_at")
        for r in rows:
            r["modules"] = loads(r["modules"], [])
        return {"conflicts": rows}

    def adjudicate(self, body, q, id):
        return self.e.adjudicate(
            id, body["resolution"], body.get("by", ""), body.get("params"))

    def workbench_pending(self, body, q):
        return {"pending": wb.pending_signoffs(self.e)}

    def workbench_eco(self, body, q, id):
        return wb.eco_dashboard(self.e, id)

    def events(self, body, q):
        entity = (q.get("entity") or [None])[0]
        rows = self.e.store.find(
            "events", "entity_id=?" if entity else "",
            (entity,) if entity else (), order="id")
        for r in rows:
            r["payload"] = loads(r["payload"], {})
        return {"events": rows}

    def recover(self, body, q):
        return {"recovered": self.e.recover()}


def make_handler(api: Api):
    class Handler(BaseHTTPRequestHandler):
        def _respond(self, method):
            parsed = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            body = None
            if length:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            status, payload = api.handle(
                method, parsed.path.rstrip("/") or "/", body, parse_qs(parsed.query))
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._respond("GET")

        def do_POST(self):
            self._respond("POST")

        def log_message(self, *args):
            pass

    return Handler


def run_server(store_path: str, host: str = "127.0.0.1", port: int = 8080):
    from .store import Store

    engine = Engine(Store(store_path))
    engine.recover()  # 启动时恢复未完成的发布
    server = ThreadingHTTPServer((host, port), make_handler(Api(engine)))
    print(f"eco-baseline-service listening on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="ECO 签核基线服务")
    parser.add_argument("--db", default="eco_service.db")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    run_server(args.db, args.host, args.port)
