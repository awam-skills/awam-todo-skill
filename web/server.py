#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
awam-todo —— Web 前端后端服务。

在本地启动一个 HTTP 服务，把 awam-todo 的待办以网页形式展示，并通过 REST API
复用 todo.py 的解析 / 校验 / 索引逻辑，支持对待办进行增删改查与状态流转。

用法：
  python web/server.py                 # 启动并自动打开浏览器（默认端口 8796）
  python web/server.py --port 9000     # 指定端口
  python web/server.py --no-browser    # 只启动，不打开浏览器

REST API：
  GET    /api/todos                       全部任务 + 索引摘要（可 ?state= 过滤）
  GET    /api/todos?state=进行中           按状态过滤（进行中/待开始/结束/维护/其他/urgent/all）
  GET    /api/todos?q=关键词               按内容/备注/工作空间关键词过滤
  GET    /api/todos/<id>                  单条任务
  POST   /api/todos                       新增任务（重复检测命中时返回 409 + duplicates）
  PUT    /api/todos/<id>                  更新任务字段（含状态，带依赖/子任务守卫）
  PATCH  /api/todos/<id>/status           仅改状态（冲突返回 409 + conflicts）
  DELETE /api/todos/<id>                  删除任务（同时清理其他任务对它的引用）

POST /api/todos 的冲突处理约定：
  默认开启重复检测：命中相同/近似任务时返回 409 {"duplicates": [...]}。
  前端可再携带 confirm 参数决定动作：
    {"force": true}                  -> 跳过重复检测，强制新建
    {"update_id": "T-...", ...}      -> 更新指定已有任务而非新建
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))
import todo  # noqa: E402  复用 todo.py 的解析/校验/索引逻辑

# 状态枚举（与 todo.py 一致）
STATUSES = ["进行中", "待开始", "结束", "维护", "其他"]
IMPORTANCES = ["重要", "不重要"]
URGENTS = ["紧急", "不紧急"]


def _norm(v):
    return (v or "").strip()


def _split_list(v):
    return [x.strip() for x in (v or "").split(";") if x.strip()]


def _guard_conflicts(task, target_status):
    """返回推进到 target_status 时的硬检查冲突清单（依赖 + 子任务），无冲突返回空 list。
    与 todo.py 的 _guard_by_deps / _guard_children_done 语义一致，但返回结构化结果。"""
    conflicts = []
    if target_status in ("进行中", "结束"):
        for did in (task.get("depends_on") or []):
            _, _, d = todo._find_task(did)
            if d is None or d.get("status") != "结束":
                conflicts.append({
                    "kind": "deps",
                    "id": did,
                    "status": d.get("status") if d else "(不存在)",
                    "date": todo._find_task(did)[0] if d else "",
                    "text": d.get("text") if d else "",
                })
    if target_status == "结束":
        for _, c in todo._children_of(task.get("id")):
            if c.get("status") != "结束":
                conflicts.append({
                    "kind": "children",
                    "id": c.get("id"),
                    "status": c.get("status"),
                    "date": todo._find_task(c.get("id"))[0],
                    "text": c.get("text"),
                })
    return conflicts


def _apply_fields(task, fields, now, is_new):
    """把 fields（dict）落到 task；is_new=True 时先填默认。返回错误字符串或 None。"""
    if is_new:
        task["status"] = "进行中"
        task["importance"] = "不重要"
        task["urgent"] = "不紧急"
        task["note"] = ""
        task["workspace"] = ""
        task["docs"] = []
        task["links"] = []
        task["depends_on"] = []
        task["due"] = None
        task["from"] = None
        task["parent"] = ""
        task["created"] = todo._fmt(now)
        task["updated"] = None

    if "text" in fields:
        task["text"] = _norm(fields["text"])
    if "importance" in fields:
        v = _norm(fields["importance"])
        task["importance"] = v if v in IMPORTANCES else "不重要"
    if "urgent" in fields:
        v = _norm(fields["urgent"])
        task["urgent"] = v if v in URGENTS else "不紧急"
    if "note" in fields:
        task["note"] = _norm(fields["note"])
    if "workspace" in fields:
        task["workspace"] = todo._norm_path(fields["workspace"])
    if "docs" in fields:
        task["docs"] = _split_list(fields["docs"])
    if "links" in fields:
        task["links"] = _split_list(fields["links"])
    if "deps" in fields:
        task["depends_on"] = _split_list(fields["deps"])
    if "due" in fields:
        raw = _norm(fields["due"])
        task["due"] = todo.parse_due(raw, now) if raw else None
    if "parent" in fields:
        task["parent"] = _norm(fields["parent"])
    if "status" in fields:
        v = _norm(fields["status"])
        task["status"] = v if v in STATUSES else task["status"]

    # 校验依赖：存在性 + 自依赖 + 环
    deps = task.get("depends_on") or []
    missing = todo._missing_deps(deps)
    if missing:
        return "以下依赖任务不存在：%s" % "; ".join(missing)
    tid = task.get("id")
    if tid in deps:
        return "任务不能依赖自身（%s）。" % tid
    if tid and todo._would_create_cycle(tid, deps):
        return "该依赖设置会形成循环依赖（%s 间接依赖自身）。" % tid

    # 校验父任务：存在性 + 自引用 + 环
    parent = _norm(task.get("parent"))
    if parent:
        if todo._find_task(parent)[2] is None:
            return "父任务 %s 不存在。" % parent
        if parent == tid:
            return "任务不能作为自身的父任务（%s）。" % tid
        if tid and todo._would_create_parent_cycle(tid, parent):
            return "该归属会形成循环（%s 成为 %s 的父任务会成环）。" % (parent, tid)
    return None

def _task_view(date, t, now):
    """把存储任务转成前端友好的展示对象。"""
    return {
        "id": t.get("id"),
        "text": t.get("text"),
        "status": t.get("status"),
        "importance": t.get("importance"),
        "urgent": t.get("urgent"),
        "is_urgent": bool(todo._is_urgent(t, now)),
        "note": t.get("note") or "",
        "workspace": t.get("workspace") or "",
        "docs": t.get("docs") or [],
        "links": t.get("links") or [],
        "depends_on": t.get("depends_on") or [],
        "parent": t.get("parent") or "",
        "due": t.get("due"),
        "created": t.get("created"),
        "updated": t.get("updated"),
        "from": t.get("from"),
        "date": date,
        "archived": bool(todo.load_file(date)["archived"]),
        "blocked": bool(todo._has_unfinished_deps(t)),
    }


def _list_all():
    now = dt.datetime.now()
    items = []
    for date, data in todo._scan_files().items():
        for t in data["tasks"]:
            items.append(_task_view(date, t, now))
    return items


class Handler(BaseHTTPRequestHandler):
    server_version = "awam-todo/0.1"

    # ---- HTTP 基础 ----
    def log_message(self, fmt, *args):
        sys.stderr.write("[web] %s\n" % (fmt % args))

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8")) or {}
        except Exception:
            return {}

    # ---- 静态文件 ----
    def _serve_static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        if ".." in path or "\\" in path:
            self._json(400, {"error": "bad path"})
            return
        fp = os.path.join(WEB_DIR, path.lstrip("/"))
        if not os.path.isfile(fp):
            self._json(404, {"error": "not found"})
            return
        ctype = "text/html; charset=utf-8"
        if path.endswith(".css"):
            ctype = "text/css; charset=utf-8"
        elif path.endswith(".js"):
            ctype = "application/javascript; charset=utf-8"
        elif path.endswith(".svg"):
            ctype = "image/svg+xml"
        with open(fp, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---- 路由 ----
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        if path == "/api/todos":
            self._api_list(qs)
        elif path.startswith("/api/todos/"):
            self._api_get(path[len("/api/todos/"):])
        else:
            self._serve_static(path)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/todos":
            self._api_create(self._read_body())
        else:
            self._json(404, {"error": "not found"})

    def do_PUT(self):
        path = urlparse(self.path).path
        if path.startswith("/api/todos/"):
            self._api_update(path[len("/api/todos/"):], self._read_body())
        else:
            self._json(404, {"error": "not found"})

    def do_PATCH(self):
        path = urlparse(self.path).path
        m = re.match(r"^/api/todos/([^/]+)/status$", path)
        if m:
            self._api_status(m.group(1), self._read_body())
        else:
            self._json(404, {"error": "not found"})

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path.startswith("/api/todos/"):
            self._api_delete(path[len("/api/todos/"):])
        else:
            self._json(404, {"error": "not found"})
    # ---- 具体 API ----
    def _api_list(self, qs):
        state = (qs.get("state") or [""])[0]
        keyword = (qs.get("q") or [""])[0].strip().lower()
        items = _list_all()
        if state and state != "all":
            if state == "urgent":
                items = [i for i in items if i["is_urgent"] and i["status"] != "结束"]
            else:
                items = [i for i in items if i["status"] == state]
        if keyword:
            items = [i for i in items if keyword in (i["text"] or "").lower()
                     or keyword in (i["note"] or "").lower()
                     or keyword in (i["workspace"] or "").lower()]
        index = todo.build_index()
        self._json(200, {
            "summary": index["summary"],
            "items": items,
        })

    def _api_get(self, tid):
        date, _, t = todo._find_task(tid)
        if t is None:
            self._json(404, {"error": "任务不存在: %s" % tid})
            return
        self._json(200, _task_view(date, t, dt.datetime.now()))

    def _api_create(self, body):
        text = _norm(body.get("text"))
        if not text:
            self._json(400, {"error": "text 不能为空"})
            return
        now = dt.datetime.now()
        force = bool(body.get("force"))
        update_id = _norm(body.get("update_id"))

        # 显式指定更新目标
        if update_id:
            code, result = _do_update(update_id, body, now, force)
            self._json(code, result)
            return

        # 重复检测
        if not force:
            hits = todo.find_duplicates(text)
            if hits:
                self._json(409, {"duplicates": [_dup_view(h) for h in hits]})
                return

        date = _norm(body.get("date")) or now.strftime("%Y-%m-%d")
        data = todo.load_file(date)
        if data["archived"]:
            data["archived"] = False
        tid = todo._next_id(date)
        task = {
            "id": tid, "status": "进行中", "importance": "不重要", "urgent": "不紧急",
            "text": text, "note": "", "workspace": "", "docs": [], "links": [],
            "depends_on": [], "due": None, "created": None, "updated": None,
            "from": None, "parent": "",
        }
        err = _apply_fields(task, {k: v for k, v in body.items() if k not in ("force", "update_id", "date")}, now, is_new=False)
        if err:
            self._json(400, {"error": err})
            return
        conflicts = _guard_conflicts(task, task["status"])
        if conflicts and not force:
            self._json(409, {"conflicts": conflicts})
            return
        data["tasks"].append(task)
        todo.save_file(date, data["archived"], data["tasks"])
        todo.build_index(now)
        self._json(201, _task_view(date, task, now))

    def _api_update(self, tid, body):
        code, result = _do_update(tid, body, dt.datetime.now(), bool(body.get("force")))
        self._json(code, result)

    def _api_status(self, tid, body):
        status = _norm(body.get("status"))
        if status not in STATUSES:
            self._json(400, {"error": "status 非法: %s" % status})
            return
        force = bool(body.get("force"))
        date, data, t = todo._find_task(tid)
        if t is None:
            self._json(404, {"error": "任务不存在: %s" % tid})
            return
        if status == t.get("status"):
            self._json(200, _task_view(date, t, dt.datetime.now()))
            return
        conflicts = _guard_conflicts(t, status)
        if conflicts and not force:
            self._json(409, {"conflicts": conflicts})
            return
        now = dt.datetime.now()
        t["status"] = status
        t["updated"] = todo._fmt(now)
        all_done = all(x["status"] == "结束" for x in data["tasks"])
        data["archived"] = all_done
        todo.save_file(date, data["archived"], data["tasks"])
        todo.build_index(now)
        self._json(200, _task_view(date, t, now))

    def _api_delete(self, tid):
        date, data, t = todo._find_task(tid)
        if t is None:
            self._json(404, {"error": "任务不存在: %s" % tid})
            return
        now = dt.datetime.now()
        data["tasks"] = [x for x in data["tasks"] if x.get("id") != tid]
        if data["tasks"]:
            all_done = all(x["status"] == "结束" for x in data["tasks"])
            data["archived"] = all_done
            todo.save_file(date, data["archived"], data["tasks"])
        else:
            # 清空后不产生归档空文件：尝试删除文件；删除不可用（如沙箱覆盖层）则写 归档:false 空文件
            try:
                os.remove(todo._file_path(date))
            except OSError:
                data["archived"] = False
                todo.save_file(date, False, data["tasks"])
        for d2, d2data in todo._scan_files().items():
            changed = False
            for x in d2data["tasks"]:
                if tid in (x.get("depends_on") or []):
                    x["depends_on"] = [d for d in x.get("depends_on") if d != tid]
                    x["updated"] = todo._fmt(now)
                    changed = True
                if (x.get("parent") or "") == tid:
                    x["parent"] = ""
                    x["updated"] = todo._fmt(now)
                    changed = True
            if changed:
                d2_all = all(y["status"] == "结束" for y in d2data["tasks"])
                d2data["archived"] = d2_all
                todo.save_file(d2, d2data["archived"], d2data["tasks"])
        todo.build_index(now)
        self._json(200, {"deleted": tid})


def _dup_view(h):
    t = h["task"]
    return {
        "kind": h["kind"],
        "score": round(h["score"] * 100),
        "id": t.get("id"),
        "date": h["date"],
        "status": t.get("status"),
        "text": t.get("text"),
        "workspace": t.get("workspace"),
    }


def _do_update(tid, body, now, force):
    """更新任务字段。返回 (code, result)。"""
    date, data, t = todo._find_task(tid)
    if t is None:
        return 404, {"error": "任务不存在: %s" % tid}
    fields = {k: v for k, v in body.items() if k not in ("force", "update_id")}
    err = _apply_fields(t, fields, now, is_new=False)
    if err:
        return 400, {"error": err}
    if "status" in fields:
        target = t["status"]
        conflicts = _guard_conflicts(t, target)
        if conflicts and not force:
            return 409, {"conflicts": conflicts}
    t["updated"] = todo._fmt(now)
    all_done = all(x["status"] == "结束" for x in data["tasks"])
    data["archived"] = all_done
    todo.save_file(date, data["archived"], data["tasks"])
    todo.build_index(now)
    return 200, _task_view(date, t, now)


def main():
    ap = argparse.ArgumentParser(prog="awam-todo-web", description="awam-todo 网页前端")
    ap.add_argument("--port", type=int, default=8796, help="监听端口（默认 8796）")
    ap.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    args = ap.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = "http://%s:%d/" % (args.host, args.port)
    print("awam-todo Web 已启动: %s" % url)
    print("按 Ctrl+C 停止。")

    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        httpd.shutdown()


if __name__ == "__main__":
    main()