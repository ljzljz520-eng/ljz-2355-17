"""标准库 HTTP 服务：参数树 API + 单页面前端。

所有读取接口都按 spec_version_id 取不可变快照 —— 页面表格、样例高亮、
导出三者天然同源。分享令牌（?share=）可对单个版本授予只读访问。
"""
from __future__ import annotations

import csv
import io
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import db as dbmod
from . import examples as ex_mod
from . import shares, versions
from .resolver import Resolver
from .tree import (DEFAULT_MAX_DEPTH, NodeNotFound, build_full,
                   compute_children, iter_nodes)


def make_handler(db_path, static_dir):

    class Handler(BaseHTTPRequestHandler):
        server_version = 'ParamTree/1.0'
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def _conn(self):
            return dbmod.connect(db_path)

        def _user(self):
            return self.headers.get('X-User-Id', 'anonymous')

        def _send(self, code, obj, content_type='application/json; charset=utf-8',
                  headers=None):
            if isinstance(obj, str) and content_type.startswith('application/json'):
                body = obj.encode()
            elif isinstance(obj, (dict, list)):
                body = json.dumps(obj, ensure_ascii=False).encode()
            else:
                body = obj.encode() if isinstance(obj, str) else obj
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _error(self, code, message):
            self._send(code, {'error': message})

        def _body(self):
            length = int(self.headers.get('Content-Length', 0))
            return json.loads(self.rfile.read(length) or b'{}')

        def _version(self, conn, vid):
            return conn.execute(
                'SELECT * FROM spec_versions WHERE id=?', (vid,)).fetchone()

        def _can_read(self, conn, ver, query):
            """ACL 或有效分享令牌（令牌只授权其指向的版本）。"""
            if shares.check_access(conn, self._user(), ver['spec_name'], 'read'):
                return True
            token = query.get('share', [None])[0]
            if token:
                try:
                    payload = shares.verify_token(dbmod.get_secret(conn), token)
                    return payload.get('vid') == ver['id']
                except shares.ShareError:
                    return False
            return False

        # ---------------- GET ----------------
        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            conn = self._conn()
            try:
                if path in ('/', '/index.html'):
                    f = open(static_dir + '/index.html', encoding='utf-8')
                    try:
                        html = f.read()
                    finally:
                        f.close()
                    self._send(200, html, 'text/html; charset=utf-8')
                    return
                if path == '/api/specs':
                    self._list_specs(conn)
                    return
                m = re.fullmatch(r'/api/versions/(\d+)', path)
                if m:
                    self._version_meta(conn, int(m.group(1)), query)
                    return
                m = re.fullmatch(r'/api/versions/(\d+)/(roots|tree|children|examples|diff|export)', path)
                if m:
                    self._version_read(conn, int(m.group(1)), m.group(2), query)
                    return
                m = re.fullmatch(r'/api/share/([A-Za-z0-9_\-\.]+)', path)
                if m:
                    self._read_share(conn, m.group(1))
                    return
                self._error(404, 'not found')
            except BrokenPipeError:
                pass
            finally:
                conn.close()

        # ---------------- POST ----------------
        def do_POST(self):
            parsed = urlparse(self.path)
            path = parsed.path
            conn = self._conn()
            try:
                try:
                    body = self._body()
                except json.JSONDecodeError:
                    self._error(400, '请求体不是合法 JSON')
                    return
                m = re.fullmatch(r'/api/specs/([\w\-\.]+)/versions', path)
                if m:
                    self._create_version(conn, m.group(1), body)
                    return
                m = re.fullmatch(r'/api/versions/(\d+)/(examples|revalidate)', path)
                if m:
                    self._version_write(conn, int(m.group(1)), m.group(2), body)
                    return
                if path == '/api/share':
                    self._create_share(conn, body)
                    return
                self._error(404, 'not found')
            finally:
                conn.close()

        def _list_specs(self, conn):
            user = self._user()
            rows = conn.execute(
                'SELECT id, spec_name, version, created_at, created_by '
                'FROM spec_versions ORDER BY spec_name, id').fetchall()
            specs = {}
            for r in rows:
                entry = specs.setdefault(r['spec_name'], {
                    'name': r['spec_name'],
                    'access': shares.check_access(conn, user, r['spec_name'], 'read'),
                    'versions': [],
                })
                entry['versions'].append({
                    'id': r['id'], 'version': r['version'],
                    'created_at': r['created_at'], 'created_by': r['created_by'],
                })
            self._send(200, {'user': user, 'specs': list(specs.values())})

        def _version_meta(self, conn, vid, query):
            ver = self._version(conn, vid)
            if not ver:
                self._error(404, '版本不存在')
                return
            if not self._can_read(conn, ver, query):
                self._error(403, '无访问权限')
                return
            self._send(200, {k: ver[k] for k in
                            ('id', 'spec_name', 'version', 'sha256', 'created_at')})

        def _version_read(self, conn, vid, what, query):
            ver = self._version(conn, vid)
            if not ver:
                self._error(404, '版本不存在')
                return
            if not self._can_read(conn, ver, query):
                self._error(403, '无访问权限')
                return
            spec = json.loads(ver['raw'])

            if what == 'roots':
                self._send(200, {'roots': Resolver(spec).schema_names()})
                return

            if what == 'diff':
                self._send(200, json.loads(ver['diff_from_prev'] or '{}'))
                return

            if what in ('tree', 'children'):
                root = query.get('root', [''])[0]
                depth = int(query.get('depth', [DEFAULT_MAX_DEPTH])[0])
                try:
                    if what == 'tree':
                        # 构建期全量展开：返回挂载了 children 的完整树
                        node = build_full(spec, root, depth)
                        self._send(200, {'node': node,
                                         'children': node.get('children', [])})
                        return
                    node_id = query.get('id', [''])[0]
                    node, children = self._cached_children(
                        conn, vid, spec, root, node_id, depth)
                except (NodeNotFound, KeyError) as exc:
                    self._error(404, '节点不存在（可能已在新版本中变更）: ' + str(exc))
                    return
                self._send(200, {'node': node, 'children': children})
                return

            if what == 'examples':
                root = query.get('root', [None])[0]
                path = query.get('path', [None])[0]
                self._send(200, {
                    'examples': ex_mod.examples_for_path(conn, vid, root, path)
                })
                return

            if what == 'export':
                self._export(conn, ver, spec, query)
                return

            self._error(404, 'not found')

        def _cached_children(self, conn, vid, spec, root, node_id, depth):
            key = f'{root}|{node_id}|{depth}'
            row = conn.execute(
                'SELECT payload FROM node_cache WHERE spec_version_id=? AND cache_key=?',
                (vid, key)).fetchone()
            if row:
                cached = json.loads(row['payload'])
                return cached['node'], cached['children']
            node, children = compute_children(spec, root, node_id, depth)
            conn.execute(
                'INSERT OR REPLACE INTO node_cache (spec_version_id, cache_key, payload) '
                'VALUES (?,?,?)',
                (vid, key, json.dumps(
                    {'node': node, 'children': children}, ensure_ascii=False)))
            conn.commit()
            return node, children

        def _export(self, conn, ver, spec, query):
            fmt = query.get('fmt', ['json'])[0]
            root_filter = query.get('root', [None])[0]
            roots = [root_filter] if root_filter else Resolver(spec).schema_names()
            trees = {r: build_full(spec, r) for r in roots}
            examples = ex_mod.examples_for_path(conn, ver['id'], None, None)

            if fmt == 'csv':
                buf = io.StringIO()
                writer = csv.writer(buf)
                writer.writerow(['root', 'path', 'name', 'required', 'conditions',
                                 'type', 'default', 'nullable', 'deprecated', 'ref'])
                for r, tree in trees.items():
                    for n in iter_nodes(tree):
                        if n['kind'] not in ('field', 'items', 'reference') or not n['path']:
                            continue
                        writer.writerow([
                            r, n['path'], n['name'], n['required'],
                            '; '.join(c['text'] for c in n['conditions']),
                            n['type'],
                            json.dumps(n['default'].get('value'), ensure_ascii=False)
                                if n['default']['present'] else '',
                            n['nullable'], n['deprecated'], n.get('ref', ''),
                        ])
                self._send(
                    200, buf.getvalue(), 'text/csv; charset=utf-8',
                    {'Content-Disposition':
                     'attachment; filename="' + ver['spec_name'] + '-'
                     + ver['version'] + '.csv"'})
                return

            self._send(200, {
                'spec_name': ver['spec_name'],
                'version': ver['version'],
                'version_id': ver['id'],
                'sha256': ver['sha256'],
                'trees': trees,
                'examples': examples,
            })

        def _read_share(self, conn, token):
            try:
                payload = shares.verify_token(dbmod.get_secret(conn), token)
            except shares.ShareError as exc:
                self._error(403, str(exc))
                return
            self._send(200, payload)

        # ---------------- 写接口 ----------------
        def _create_version(self, conn, spec_name, body):
            user = self._user()
            exists = conn.execute(
                'SELECT 1 FROM spec_versions WHERE spec_name=? LIMIT 1',
                (spec_name,)).fetchone()
            if exists and not shares.check_access(conn, user, spec_name, 'write'):
                self._error(403, '需要 editor 及以上权限')
                return
            if not isinstance(body.get('spec'), dict) or not body.get('version'):
                self._error(400, '需要 version 与 spec')
                return
            if not exists:
                # 首个版本的创建者成为该规范 admin（须在 create_version 提交前授权，
                # 因为它内部会 commit）；之后新版本再按 write 权限管控。
                shares.grant(conn, user, spec_name, 'admin')
            try:
                vid, diff, affected = versions.create_version(
                    conn, spec_name, body['version'], body['spec'], user)
            except Exception as exc:
                self._error(409, str(exc))
                return
            self._send(201, {'id': vid, 'diff': diff,
                             'affected_examples': affected})

        def _version_write(self, conn, vid, what, body):
            ver = self._version(conn, vid)
            if not ver:
                self._error(404, '版本不存在')
                return
            if not shares.check_access(conn, self._user(), ver['spec_name'], 'write'):
                self._error(403, '需要 editor 及以上权限')
                return
            if what == 'examples':
                try:
                    eid, errors = ex_mod.add_example(
                        conn, vid, body['root'], body.get('title', ''),
                        body['payload'], self._user())
                except KeyError as exc:
                    self._error(400, str(exc))
                    return
                self._send(201, {'id': eid, 'errors': errors,
                                 'valid': not errors})
                return
            if what == 'revalidate':
                counts = versions.revalidate(conn, vid)
                self._send(200, counts)
                return
            self._error(404, 'not found')

        def _create_share(self, conn, body):
            vid = body.get('vid')
            ver = self._version(conn, vid)
            if not ver:
                self._error(404, '版本不存在')
                return
            if not shares.check_access(conn, self._user(), ver['spec_name'], 'read'):
                self._error(403, '无访问权限')
                return
            token = shares.make_token(
                dbmod.get_secret(conn),
                {'vid': vid, 'expanded': body.get('expanded', []),
                 'by': self._user()},
                ttl=int(body.get('ttl', shares.DEFAULT_TTL)),
            )
            self._send(201, {'token': token, 'url': '/?share=' + token})

    return Handler


def serve(db_path, static_dir, host='127.0.0.1', port=8000):
    conn = dbmod.connect(db_path)
    dbmod.init_db(conn)
    conn.close()
    httpd = ThreadingHTTPServer((host, port), make_handler(db_path, static_dir))
    print(f'参数树服务已启动: http://{host}:{port}')
    httpd.serve_forever()


if __name__ == '__main__':
    import os
    import sys
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db_path = os.path.join(base, 'data', 'app.db')
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    serve(db_path, os.path.join(base, 'static'), port=port)
