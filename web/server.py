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
  GET    /api/todos                       全部任务 + 索引摘要（可 ?state= / ?q= / ?tag= 过滤，含 revision 指纹）
  GET    /api/todos?state=进行中           按状态过滤（进行中/待开始/结束/维护/其他/urgent/all）
  GET    /api/todos?state=overdue          仅逾期任务（带 overdue_days）
  GET    /api/todos?state=today            今日要处理（逾期 / 停滞 / 今天到期，带 pin_reason）
  GET    /api/todos?q=关键词               按内容/备注/工作空间/预案关键词过滤
  GET    /api/todos?tag=标签               按标签过滤
  GET    /api/todos/<id>                  单条任务
  POST   /api/todos                       新增任务（重复检测命中时返回 409 + duplicates）
  PUT    /api/todos/<id>                  更新任务字段（含状态，带依赖/子任务守卫）
  PATCH  /api/todos/<id>/status           仅改状态（冲突返回 409 + conflicts）
  DELETE /api/todos/<id>                  删除任务（同时清理其他任务对它的引用）
  POST   /api/todos/apply-patches         批量应用 patch 并一次性落盘（网页端延迟保存入口）

POST /api/todos/apply-patches 约定：
  body: {"revision": <GET /api/todos 返回的指纹>, "patches": [
    {"op": "create", "temp_id": "local-x", "task": {...字段, "date": "YYYY-MM-DD"}},
    {"op": "update", "id": "T-...", "fields": {...}},
    {"op": "status", "id": "T-...", "status": "结束"},
    {"op": "delete", "id": "T-..."},
  ]}
  保存前对比 revision 与当前文件指纹：不一致说明外部有并发修改，patch 会应用在
  最新文件内容上（合并语义），响应中 external_changed=true。
  落盘前会对每个日期文件做变更比较：将写入的内容与磁盘当前内容一致时跳过写文件/
  删除/索引重建（没有变更就不保存），响应中 changed=实际写盘文件数。
  返回 {"ok", "saved", "failed", "external_changed", "id_map": {temp_id: 正式ID},
        "changed", "revision"}。
  failed 条目 reason 可为 duplicate（含 duplicates）/ conflict（含 conflicts）/ not_found 等；
  前端可对失败条目携带 force 重新提交。

POST /api/todos 的冲突处理约定：
  默认开启重复检测：命中相同/近似任务时返回 409 {"duplicates": [...]}。
  前端可再携带 confirm 参数决定动作：
    {"force": true}                  -> 跳过重复检测，强制新建
    {"update_id": "T-...", ...}      -> 更新指定已有任务而非新建

GET /api/todos 返回体补充（前端一律以这些字段渲染，不得自行编造）：
  schema_version  存储格式版本；week 周口径（周一为起点）；defaults 可覆盖的默认值（表单预填）
  每条 item 含 blocker/counter（预案）、overdue_days/due_in_days/stall_days/projected_finish
  （算不出为 null）、pin/pin_reason（置顶与理由）。
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
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


def _apply_fields(task, fields, now, is_new, deps_resolver=None):
    """把 fields（dict）落到 task；is_new=True 时先填默认。返回错误字符串或 None。
    deps_resolver：可选的可调用对象，入参依赖 ID 列表，返回不存在的 ID 列表；
    不传时用 todo._missing_deps（基于磁盘），批量保存时传基于内存任务表的实现。"""
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
        task["blocker"] = ""
        task["counter"] = ""
        task["origin_date"] = ""
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
    if "blocker" in fields:
        task["blocker"] = _norm(fields["blocker"])
    if "counter" in fields:
        task["counter"] = _norm(fields["counter"])
    if "workspace" in fields:
        task["workspace"] = todo._norm_path(fields["workspace"])
    if "docs" in fields:
        task["docs"] = _split_list(fields["docs"])
    if "links" in fields:
        task["links"] = _split_list(fields["links"])
    if "tags" in fields:
        task["tags"] = _split_list(fields["tags"])
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
    missing = deps_resolver(deps) if deps_resolver else todo._missing_deps(deps)
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
    """把存储任务转成前端友好的展示对象（含派生指标；算不出的为 None，前端显示「—」）。"""
    pin, reason = todo._pin_info(t, now)
    return {
        "id": t.get("id"),
        "text": t.get("text"),
        "status": t.get("status"),
        "importance": t.get("importance"),
        "urgent": t.get("urgent"),
        "is_urgent": bool(todo._is_urgent(t, now)),
        "note": t.get("note") or "",
        # 预案（选填）
        "blocker": t.get("blocker") or "",
        "counter": t.get("counter") or "",
        "workspace": t.get("workspace") or "",
        "docs": t.get("docs") or [],
        "links": t.get("links") or [],
        "tags": t.get("tags") or [],
        "depends_on": t.get("depends_on") or [],
        "parent": t.get("parent") or "",
        "due": t.get("due"),
        "created": t.get("created"),
        "updated": t.get("updated"),
        "from": t.get("from"),
        "date": date,
        "archived": bool(todo.load_file(date)["archived"]),
        "blocked": bool(todo._has_unfinished_deps(t)),
        # 派生指标：None = 数据不足，前端须渲染「—」/「暂无推算」，不得编造
        "overdue_days": todo._overdue_days(t, now),
        "due_in_days": todo._due_in_days(t, now),
        "stall_days": todo._stall_days(t, now),
        "projected_finish": todo._projected_finish(t, now),
        "pin": pin,
        "pin_reason": reason or "",
    }


def _list_all():
    now = dt.datetime.now()
    items = []
    for date, data in todo._scan_files().items():
        for t in data["tasks"]:
            items.append(_task_view(date, t, now))
    return items


def _priority_rank(item):
    """紧急重要程度分档：紧急+重要 最高，依次为 紧急 或 重要，最末为 都不占。"""
    rank = 0
    if item.get("importance") == "重要":
        rank += 1
    if item.get("is_urgent"):
        rank += 1
    return rank


def _sort_items(items):
    """默认排序：
    0) 置顶组在前：逾期(0) > 停滞(1) > 今天到期(2)，无置顶理由的在后；
    1) 按截止日期升序（最先截止的在最前）；无截止日期的统一排在带截止日期的之后；
    2) 其次按紧急重要程度降序（紧急+重要 > 紧急/重要 > 都不占）；
    3) 最后按创建时间升序作为稳定次序。
    """
    items.sort(key=lambda it: (
        (0, it["pin"]) if it.get("pin") is not None else (1, 0),   # 置顶组在前
        0 if it.get("due") else 1,                      # 带截止日期的在前，无截止日期的在后
        todo._due_value(it.get("due")) or dt.datetime.min,  # 截止越早越靠前
        -_priority_rank(it),                            # 紧急重要程度降序
        str(it.get("created") or ""),
    ))
    return items


def _revision():
    """计算全部存储文件的内容指纹，用于检测外部是否有并发修改。"""
    h = hashlib.md5()
    for date in sorted(todo._scan_files().keys()):
        fp = todo._file_path(date)
        try:
            with open(fp, "rb") as f:
                h.update(date.encode("utf-8"))
                h.update(f.read())
        except OSError:
            continue
    return h.hexdigest()


def _render_file(date, archived, tasks):
    """生成将要写入的存储文件内容（与 todo.save_file 序列化一致，LF 归一），供变更比较。"""
    parts = ["# 待办 %s" % date, "归档: %s" % ("true" if archived else "false"), "---"]
    for t in tasks:
        parts.append("")
        parts.append(todo._write_block(t))
    return "\n".join(parts) + "\n"


def _file_content_matches(date, archived, tasks):
    """磁盘当前内容与"将要写入的内容"是否一致（逻辑比较，行尾归一）。
    一致则无需真正写盘/删除——即"没有变更就不保存"。"""
    fp = todo._file_path(date)
    new_content = _render_file(date, archived, tasks)
    if not os.path.exists(fp):
        return new_content == ""
    try:
        with open(fp, "r", encoding="utf-8") as f:
            return f.read() == new_content
    except OSError:
        return False


def _next_id_in_memory(files, date):
    """基于内存中的任务集合生成下一个任务 ID（避免批次内重复）。"""
    base = date.replace("-", "")
    seq = 0
    for t in files.get(date, {}).get("tasks", []):
        m = re.match(r"^T-%s-(\d+)$" % base, t.get("id") or "")
        if m:
            seq = max(seq, int(m.group(1)))
    return "T-%s-%03d" % (base, seq + 1)


def _guard_in_memory(task, target_status, by_id):
    """基于内存任务表执行依赖 + 子任务守卫，返回冲突清单。"""
    conflicts = []
    if target_status in ("进行中", "结束"):
        for did in (task.get("depends_on") or []):
            ent = by_id.get(did)
            if ent is None or ent[1].get("status") != "结束":
                conflicts.append({
                    "kind": "deps",
                    "id": did,
                    "status": ent[1].get("status") if ent else "(不存在)",
                    "text": ent[1].get("text") if ent else "",
                })
    if target_status == "结束":
        for ent in by_id.values():
            c = ent[1]
            if c.get("parent") == task.get("id") and c.get("status") != "结束":
                conflicts.append({
                    "kind": "children",
                    "id": c.get("id"),
                    "status": c.get("status"),
                    "text": c.get("text"),
                })
    return conflicts


def _resolve_workspace(path):
    """校验并返回本地绝对目录路径；非法或不存在返回 None。"""
    path = (path or "").strip()
    if not path:
        return None
    if not os.path.isabs(path):
        return None
    if not os.path.isdir(path):
        return None
    return path


def _open_dir(path):
    """在系统文件管理器中打开目录。返回 (ok, msg)。"""
    p = _resolve_workspace(path)
    if p is None:
        return False, "路径无效或目录不存在"
    if sys.platform.startswith("win"):
        os.startfile(p)  # noqa: S606  打开文件管理器
    elif sys.platform == "darwin":
        subprocess.Popen(["open", p])
    else:
        subprocess.Popen(["xdg-open", p])
    return True, "已打开目录"


def _find_cursor_cli():
    """定位 Cursor 命令行入口。返回命令列表或 None。"""
    cand = shutil.which("cursor")
    if cand:
        return [cand]
    # Windows 常见安装位置
    if sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA", "")
        for base in (os.path.join(local, "Programs", "cursor"),
                     os.path.join(local, "Programs", "Cursor")):
            if os.path.isdir(base):
                cli = os.path.join(base, "resources", "app", "bin", "cursor.cmd")
                if os.path.isfile(cli):
                    return [cli]
                cli = os.path.join(base, "cursor.exe")
                if os.path.isfile(cli):
                    return [cli]
    return None


def _open_cursor(path):
    """用 Cursor 打开目录。返回 (ok, msg)。"""
    p = _resolve_workspace(path)
    if p is None:
        return False, "路径无效或目录不存在"
    cli = _find_cursor_cli()
    if cli is None:
        return False, "未找到 Cursor 命令行，请在系统 PATH 中配置 cursor 命令"
    try:
        if sys.platform.startswith("win"):
            subprocess.Popen(cli + [p], shell=(cli[0].lower().endswith(".cmd")),
                             creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | 0x08000000)
        else:
            subprocess.Popen(cli + [p])
    except Exception as e:  # noqa: BLE001
        return False, "启动 Cursor 失败：%s" % e
    return True, "已用 Cursor 打开"


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
        elif path == "/api/todos/apply-patches":
            self._api_apply_patches(self._read_body())
        elif path == "/api/workspace/open":
            self._api_workspace_open(self._read_body())
        elif path == "/api/workspace/cursor":
            self._api_workspace_cursor(self._read_body())
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
        tag = (qs.get("tag") or [""])[0].strip()
        items = _list_all()
        if state and state != "all":
            if state == "urgent":
                items = [i for i in items if i["is_urgent"] and i["status"] != "结束"]
            elif state == "overdue":
                items = [i for i in items if i.get("overdue_days") is not None]
            elif state == "today":
                items = [i for i in items if i.get("pin") is not None and i["status"] != "结束"]
            else:
                items = [i for i in items if i["status"] == state]
        if keyword:
            items = [i for i in items if keyword in (i["text"] or "").lower()
                     or keyword in (i["note"] or "").lower()
                     or keyword in (i["workspace"] or "").lower()
                     or keyword in (i.get("blocker") or "").lower()
                     or keyword in (i.get("counter") or "").lower()]
        if tag:
            items = [i for i in items if tag in (i.get("tags") or [])]
        _sort_items(items)
        index = todo.build_index()
        self._json(200, {
            "schema_version": index.get("schema_version"),
            "summary": index["summary"],
            "week": index.get("week"),            # 周口径：周一为起点
            "defaults": todo._defaults_view(),    # 可覆盖的默认值（仅用于表单预填）
            "items": items,
            "revision": _revision(),
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
            "from": None, "parent": "", "tags": [],
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

    def _api_apply_patches(self, body):
        """批量应用前端暂存的 patch 并一次性落盘。
        body: {revision, patches: [{op, ...}]}
        op 支持 create / update / status / delete。
        保存前对比 revision：不一致说明外部（命令行等）有并发修改，
        此时把 patch 应用到最新文件内容上（合并语义），并返回 external_changed。
        failed 中可能带 reason: duplicate（含 duplicates）或 conflict（含 conflicts），
        由前端决定强制重试或放弃该条。"""
        patches = body.get("patches")
        if not isinstance(patches, list):
            self._json(400, {"error": "patches 必须是数组"})
            return
        if not patches:
            self._json(200, {"ok": True, "saved": 0, "failed": [], "external_changed": False, "id_map": {}})
            return
        now = dt.datetime.now()
        base_rev = _norm(body.get("revision"))
        cur_rev = _revision()
        external_changed = bool(base_rev) and base_rev != cur_rev

        files = todo._scan_files()
        by_id = {}
        for date, data in files.items():
            for t in data["tasks"]:
                by_id[t["id"]] = (date, t)
        id_map = {}
        failed = []
        saved = 0
        changed_dates = set()

        for p in patches:
            if not isinstance(p, dict):
                failed.append({"op": None, "reason": "invalid_patch"})
                continue
            op = p.get("op")
            force = bool(p.get("force"))
            if op == "create":
                temp_id = p.get("temp_id") or ""
                task_fields = p.get("task") or {}
                text = _norm(task_fields.get("text"))
                if not text:
                    failed.append({"op": "create", "temp_id": temp_id, "reason": "text 不能为空"})
                    continue
                if not force:
                    hits = todo.find_duplicates(text)
                    if hits:
                        failed.append({"op": "create", "temp_id": temp_id,
                                       "reason": "duplicate",
                                       "duplicates": [_dup_view(h) for h in hits]})
                        continue
                date = _norm(task_fields.get("date")) or now.strftime("%Y-%m-%d")
                if date not in files:
                    files[date] = {"archived": False, "tasks": []}
                data = files[date]
                if data["archived"]:
                    data["archived"] = False
                tid = _next_id_in_memory(files, date)
                task = {
                    "id": tid, "status": "进行中", "importance": "不重要", "urgent": "不紧急",
                    "text": text, "note": "", "workspace": "", "docs": [], "links": [],
                    "depends_on": [], "due": None, "created": None, "updated": None,
                    "from": None, "parent": "", "tags": [],
                }
                err = _apply_fields(
                    task,
                    {k: v for k, v in task_fields.items() if k != "date"},
                    now, is_new=False,
                    deps_resolver=lambda ds: [d for d in ds if d not in by_id and d != task.get("id")],
                )
                if err:
                    failed.append({"op": "create", "temp_id": temp_id, "reason": err})
                    continue
                conflicts = _guard_in_memory(task, task["status"], by_id)
                if conflicts and not force:
                    failed.append({"op": "create", "temp_id": temp_id,
                                   "reason": "conflict", "conflicts": conflicts})
                    continue
                data["tasks"].append(task)
                by_id[tid] = (date, task)
                if temp_id:
                    id_map[temp_id] = tid
                changed_dates.add(date)
                saved += 1
            elif op in ("update", "status"):
                tid = p.get("id")
                ent = by_id.get(tid)
                if ent is None:
                    failed.append({"op": op, "id": tid, "reason": "not_found"})
                    continue
                date, t = ent
                if op == "status":
                    status = _norm(p.get("status"))
                    if status not in STATUSES:
                        failed.append({"op": "status", "id": tid, "reason": "status 非法: %s" % status})
                        continue
                    if status == t.get("status"):
                        continue
                    conflicts = _guard_in_memory(t, status, by_id)
                    if conflicts and not force:
                        failed.append({"op": "status", "id": tid,
                                       "reason": "conflict", "conflicts": conflicts})
                        continue
                    t["status"] = status
                else:
                    fields = p.get("fields") or {}
                    before_block = todo._write_block(t)
                    err = _apply_fields(
                        t, fields, now, is_new=False,
                        deps_resolver=lambda ds: [d for d in ds if d not in by_id and d != t.get("id")],
                    )
                    if err:
                        failed.append({"op": "update", "id": tid, "reason": err})
                        continue
                    after_block = todo._write_block(t)
                    if after_block == before_block:
                        # 字段无实际变化（提交内容与原值相同）：不刷新 updated，不算变更
                        continue
                    if "status" in fields:
                        conflicts = _guard_in_memory(t, t["status"], by_id)
                        if conflicts and not force:
                            failed.append({"op": "update", "id": tid,
                                           "reason": "conflict", "conflicts": conflicts})
                            continue
                t["updated"] = todo._fmt(now)
                changed_dates.add(date)
                saved += 1
            elif op == "delete":
                tid = p.get("id")
                ent = by_id.get(tid)
                if ent is None:
                    # 任务已不存在（可能已被外部删除）：视为已删除
                    continue
                date, t = ent
                data = files[date]
                data["tasks"] = [x for x in data["tasks"] if x.get("id") != tid]
                by_id.pop(tid, None)
                for d2, d2data in files.items():
                    for x in d2data["tasks"]:
                        if tid in (x.get("depends_on") or []):
                            x["depends_on"] = [d for d in x.get("depends_on") if d != tid]
                            x["updated"] = todo._fmt(now)
                            changed_dates.add(d2)
                        if (x.get("parent") or "") == tid:
                            x["parent"] = ""
                            x["updated"] = todo._fmt(now)
                            changed_dates.add(d2)
                changed_dates.add(date)
                saved += 1
            else:
                failed.append({"op": op, "reason": "unknown_op"})
        # 落盘：先做变更比较——只有内容与磁盘不一致的日期文件才真正写入/删除
        wrote_dates = []
        for date in sorted(changed_dates):
            data = files[date]
            if data["tasks"]:
                data["archived"] = all(x["status"] == "结束" for x in data["tasks"])
                if not _file_content_matches(date, data["archived"], data["tasks"]):
                    todo.save_file(date, data["archived"], data["tasks"])
                    wrote_dates.append(date)
            else:
                fp = todo._file_path(date)
                if os.path.exists(fp):
                    os.remove(fp)
                    wrote_dates.append(date)
                # 文件本就不存在：无变更，不创建空文件
        if wrote_dates:
            todo.build_index(now)
        self._json(200, {
            "ok": True,
            "saved": saved,
            "failed": failed,
            "external_changed": external_changed,
            "id_map": id_map,
            "changed": len(wrote_dates),
            "revision": _revision(),
        })

    def _api_workspace_open(self, body):
        ok, msg = _open_dir(_norm(body.get("path")))
        self._json(200 if ok else 400, {"ok": ok, "message": msg})

    def _api_workspace_cursor(self, body):
        ok, msg = _open_cursor(_norm(body.get("path")))
        self._json(200 if ok else 400, {"ok": ok, "message": msg})


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