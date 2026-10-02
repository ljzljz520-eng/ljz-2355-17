"""标准库 HTTP 服务：把 Service 暴露为 JSON API，并托管 web/ 静态页。"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import model as M
from .resolver import SnapshotMismatch
from .service import Service
from .store import Store

_WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


class ApiHandler(BaseHTTPRequestHandler):
    svc: Service = None  # 由 main 注入

    # ---------- helpers ----------
    def _send(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code: str, message: str, status=400, detail=None):
        self._send({"error": {"code": code, "message": message, "detail": detail}},
                   status)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode())

    def _principal(self, body=None, qs=None):
        body = body or {}
        qs = qs or {}
        return body.get("principal") or qs.get("principal", ["anonymous"])[0]

    def log_message(self, fmt, *args):  # 安静
        return

    # ---------- routing ----------
    def do_GET(self):
        u = urlparse(self.path)
        p, qs = u.path, parse_qs(u.query)
        try:
            if p.startswith("/api/"):
                return self._route_get(p, qs)
            return self._static(p)
        except Exception as e:  # noqa: BLE001
            self._error("internal", str(e), 500)

    def do_POST(self):
        u = urlparse(self.path)
        p, qs = u.path, parse_qs(u.query)
        try:
            body = self._body()
            if p.startswith("/api/"):
                return self._route_post(p, qs, body)
            self._error("not-found", p, 404)
        except SnapshotMismatch as e:
            self._error("snapshot-mismatch", str(e), 409)
        except PermissionError as e:
            self._error("forbidden-node", f"无访问权限: {e}", 403)
        except KeyError as e:
            self._error("not-found", f"不存在: {e}", 404)
        except LookupError as e:
            self._error("not-found", str(e), 404)
        except json.JSONDecodeError as e:
            self._error("bad-json", str(e), 400)
        except Exception as e:  # noqa: BLE001
            self._error("internal", str(e), 500)

    def _route_get(self, p, qs):
        parts = [x for x in p.split("/") if x]
        # /api/admin/apis/:api/versions
        if len(parts) == 5 and parts[1] == "admin" and parts[2] == "apis" \
                and parts[4] == "versions":
            return self._send({"versions": self.svc.store.versions(parts[3])})
        # /api/sessions/:id/tree?nodeId=
        if len(parts) == 4 and parts[1] == "sessions" and parts[3] == "tree":
            sess = self.svc.get_session(parts[2])
            nid = qs.get("nodeId", [None])[0]
            return self._send(sess.tree(nid) if nid else sess.root())
        # /api/admin/queue?api=&snapshot=
        if parts[-1] == "queue" and parts[1] == "admin":
            snap = qs.get("snapshot", [None])[0]
            return self._send({"queue": self.svc.store.queue(snap)})
        self._error("not-found", p, 404)

    def _route_post(self, p, qs, body):
        parts = [x for x in p.split("/") if x]
        # 发布
        if len(parts) == 6 and parts[1] == "admin" and parts[2] == "apis" \
                and parts[5] == "publish":
            api, version = parts[3], body["version"]
            res = self.svc.publish(api, version, body["schema"],
                                   body.get("by"))
            return self._send(res, 201)
        # 权限策略
        if parts[-1] == "policies" and parts[1] == "admin":
            self.svc.store.set_policy(body["api"], body["pattern"],
                                      body["principal"], body["effect"])
            return self._send({"ok": True}, 201)
        # 示例
        if parts[-1] == "examples" and parts[1] == "admin":
            api = body["api"]
            sid = self.svc.store.latest_snapshot(api)
            res = self.svc.add_example(api, body["name"], body["payload"], sid)
            return self._send(res, 201)
        if len(parts) == 5 and parts[1] == "admin" and parts[2] == "examples" \
                and parts[4] == "revalidate":
            sid = body["snapshot_id"]
            return self._send(self.svc.revalidate(parts[3], sid))
        # 会话
        if parts == ["api", "sessions"]:
            sess = self.svc.create_session(body["api"], body.get("principal", "anon"),
                                           body.get("version"))
            return self._send({"session_id": sess.id,
                               "snapshot_id": sess.snapshot_id}, 201)
        if parts == ["api", "sessions", "from-share"]:
            return self._send(self.svc.from_share(body["share_id"],
                                                  body.get("principal", "anon")), 201)
        if len(parts) == 4 and parts[1] == "sessions":
            sess = self.svc.get_session(parts[2])
            action = parts[3]
            if action == "expand":
                return self._send(sess.expand(body["node_id"]))
            if action == "collapse":
                sess.collapse(body["node_id"])
                return self._send({"ok": True})
            if action == "select":
                sess.select(body["path"])
                return self._send({"ok": True})
            if action == "highlight":
                return self._send(sess.highlight(body["example_id"]))
            if action == "switch-version":
                return self._send(sess.switch_version(body.get("version"),
                                                      body.get("snapshot_id")))
            if action == "share":
                sid = self.svc.share(sess, body.get("scope_hint", ""))
                return self._send({"share_id": sid})
            if action == "export":
                return self._send(sess.export())
        self._error("not-found", p, 404)

    def _static(self, p):
        rel = "index.html" if p in ("/", "") else p.lstrip("/")
        fp = os.path.normpath(os.path.join(_WEB, rel))
        if not fp.startswith(_WEB) or not os.path.isfile(fp):
            self._error("not-found", p, 404)
            return
        ctype = {"html": "text/html; charset=utf-8", "js": "application/javascript",
                 "css": "text/css"}.get(fp.rsplit(".", 1)[-1], "text/plain")
        with open(fp, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def make_server(db_path: str = ":memory:", host: str = "127.0.0.1",
                port: int = 8077) -> ThreadingHTTPServer:
    svc = Service(Store(db_path))
    ApiHandler.svc = svc

    class _Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    return _Server((host, port), ApiHandler), svc


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data.db"))
    ap.add_argument("--port", type=int, default=8077)
    args = ap.parse_args()
    srv, _ = make_server(args.db, port=args.port)
    print(f"参数树服务已启动: http://127.0.0.1:{args.port}/")
    srv.serve_forever()


if __name__ == "__main__":
    main()
