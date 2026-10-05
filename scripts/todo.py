#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
awam-todo —— 输入与管理 To-Do 的实现脚本。

职责：
  1. 把一条待办追加到按日期划分的存储文件 storage/YYYY-MM-DD.md
  2. 维护索引 index.json（待办 / 进行中 / 紧急 / 结束 / 归档文件）
  3. 支持完成 / 重新打开 / 标记进行中；某文件全部任务完成则自动标记归档
  4. 支持从自然语言解析时间（今天 / 明天 / 星期X / X天 / X小时 / 具体时间）作为兜底，
     主要语义解析由调用它的 Agent 在会话中完成。

用法示例：
  python todo.py add --text "撰写季度报告" --importance 重要 --due "2026-10-05 10:00"
  python todo.py add --text "预约体检" --urgent 紧急 --note "带身份证" --tags "健康;生活"
  python todo.py add --update-id T-20261002-001 --text "撰写季度报告"   # 确认后更新已有
  python todo.py add --force --text "撰写季度报告"                     # 跳过重复检测强制新建
  python todo.py check --text "撰写季度报告"                           # 只读查重
  python todo.py list
  python todo.py list --state urgent
  python todo.py done T-20261002-001
  python todo.py start T-20261002-001
  python todo.py reopen T-20261002-001
  python todo.py dep T-20261002-001 --add T-20261002-003   # 设置依赖链
  python todo.py dep T-20261002-001                        # 查看依赖
  python todo.py add --text "子任务" --parent T-20261002-001   # 新增子任务（归属于主任务）
  python todo.py children T-20261002-001                   # 列出某主任务下的子任务
  python todo.py deparent T-20261002-003                   # 解除子任务归属
  python todo.py archive 2026-10-01
  python todo.py show T-20261002-001
  python todo.py index --rebuild

依赖链说明：
  - 通过 `dep`（或 `add --deps`）为任务标记前置依赖（任务 ID，用 ; 分隔）。
  - 依赖硬检查是死的：目标状态推进到「进行中/结束」时，若前置依赖未全部结束，
    脚本拒绝（exit 3）并列出冲突，提示 AI 向用户确认；用户确认后由 AI 以 --force 强制推进。
  - 设置依赖时会做存在性校验与循环依赖检测，成环/引用不存在任务直接报错。

子任务说明：
  - 通过 `add --parent <主任务ID>` 新增子任务；子任务拥有与其他任务相同的全部属性。
  - 父任务完成硬检查：把存在未完成子任务的任务标记为「结束」时，脚本拒绝（exit 3），
    交由 AI 向用户确认后决定是否 --force 强制结束；子任务全部完成后父任务才能正常结束。
  - 父子归属可嵌套但不得成环；`children <id>` 查看某主任务的子任务，`deparent <id>` 解除归属。
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
from difflib import SequenceMatcher

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORAGE_DIR = os.path.join(SKILL_DIR, "storage")
INDEX_PATH = os.path.join(SKILL_DIR, "index.json")
ENV_PATH = os.path.join(SKILL_DIR, "env.json")

STATUS_MARKS = {"维护": "[ ]", "进行中": "[~]", "结束": "[x]", "待开始": "[ ]", "其他": "[ ]"}
STATUS_SET = set(STATUS_MARKS.keys())
DEFAULT_STATUS = "进行中"
IMPORTANCE_SET = {"重要", "不重要"}
DEFAULT_IMPORTANCE = "不重要"
URGENT_SET = {"紧急", "不紧急"}
DEFAULT_URGENT = "不紧急"

# 重复检测：相似度阈值（含边界）；exit 3 = 发现近似重复，需确认
SIMILAR_THRESHOLD = 0.72
EXIT_NEEDS_CONFIRM = 3

# 旧值 -> 新枚举 的迁移映射（兼容历史存储文件）
_OLD_STATUS = {"open": "待开始", "in_progress": "进行中", "done": "结束"}
_OLD_IMPORTANCE = {"高": "重要", "中": "不重要", "低": "不重要"}


def _norm_status(v):
    v = _norm(v)
    if v in STATUS_SET:
        return v
    return _OLD_STATUS.get(v, "其他")


def _norm_importance(v):
    v = _norm(v)
    if v in IMPORTANCE_SET:
        return v
    return _OLD_IMPORTANCE.get(v, DEFAULT_IMPORTANCE)


def _norm_urgent(v):
    v = _norm(v).lower()
    if v in ("紧急", "true", "1", "是"):
        return "紧急"
    return DEFAULT_URGENT

# ---- 时间解析（兜底） -----------------------------------------------------
_WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6, "七": 6}


def _norm(s):
    return (s or "").strip()


def _parse_weekday_target(name, base):
    """解析 星期X/周X，返回落在 base 当天/之后的最近一天（datetime.date）。"""
    w = _WEEKDAY.get(name)
    if w is None:
        return None
    delta = (w - base.weekday()) % 7
    if delta == 0:
        delta = 7  # 今天不说“下周一”等，默认取未来最近一次
    return base + dt.timedelta(days=delta)


def parse_due(text, now=None):
    """把自然语言时间解析为 ISO 字符串（YYYY-MM-DD 或 YYYY-MM-DD HH:MM）。
    支持 今天/明天/后天、星期X/周X/下周X、M月D日、YYYY-MM-DD、X天后、
    HH:MM、X点(半)，以及“相对日期 + 时间”的组合（如 明天 10:00、周五 09:00）。
    解析不了返回 None。now 用于测试注入，默认取当前本地时间。"""
    s = _norm(text)
    if not s:
        return None
    now = now or dt.datetime.now()
    base = now.date()

    # 提取尾部时间部分：HH:MM 或 X点/点半
    time_part = None
    date_s = s
    m = re.search(r"(\d{1,2}):(\d{2})\s*$", s)
    if m:
        time_part = (int(m.group(1)), int(m.group(2)))
        date_s = s[:m.start()].strip()
    else:
        m = re.search(r"(\d{1,2})点(半)?\s*$", s)
        if m:
            time_part = (int(m.group(1)), 30 if m.group(2) else 0)
            date_s = s[:m.start()].strip()

    def fmt(d, t):
        if t is not None:
            return "%04d-%02d-%02d %02d:%02d" % (d.year, d.month, d.day, t[0], t[1])
        return "%04d-%02d-%02d" % (d.year, d.month, d.day)

    def date_of(ss):
        ss = _norm(ss)
        if not ss:
            return None
        m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", ss)
        if m:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = re.match(r"^(\d{1,2})月(\d{1,2})日$", ss)
        if m:
            return dt.date(base.year, int(m.group(1)), int(m.group(2)))
        if ss in ("今天", "今日"):
            return base
        if ss in ("明天", "明日"):
            return base + dt.timedelta(days=1)
        if ss == "后天":
            return base + dt.timedelta(days=2)
        if ss in ("昨天", "昨日"):
            return base - dt.timedelta(days=1)
        m = re.match(r"^下?(?:星期|周)([一二三四五六日天七])$", ss)
        if m:
            return _parse_weekday_target(m.group(1), base)
        m = re.match(r"^(\d+)\s*天(?:后)?$", ss)
        if m:
            return base + dt.timedelta(days=int(m.group(1)))
        return None

    # 纯时间（无日期词）→ 今天该时间
    if not date_s and time_part:
        return fmt(base, time_part)

    d = date_of(date_s)
    if d is None:
        return None
    return fmt(d, time_part)


def _fmt(d):
    return d.strftime("%Y-%m-%d %H:%M") if isinstance(d, dt.datetime) else d.strftime("%Y-%m-%d")


# ---- 工作环境识别与路径统一 ------------------------------------------------
# env.json 保存运行时环境与路径风格偏好；首次由 `init` 生成，`env` 命令查看/修改。
# 路径风格：
#   windows -> 工作空间统一为反斜杠 `G:\Projects\...`
#   posix   -> 工作空间统一为正斜杠 `G:/Projects/...`
#   mixed   -> 保持录入原样，不做转换
_PATH_STYLES = ("windows", "posix", "mixed")


def _detect_env():
    """探测当前运行环境，返回环境信息 dict（不落盘）。"""
    import platform
    import socket
    is_windows = os.name == "nt"
    default_style = "windows" if is_windows else "posix"
    return {
        "generated": _fmt(dt.datetime.now()),
        "path_style": default_style,
        "platform": {
            "os": platform.system() or ("Windows" if is_windows else "Unknown"),
            "os_name": os.name,
            "is_windows": is_windows,
            "path_sep": os.sep,
            "python_version": platform.python_version(),
            "hostname": socket.gethostname() if hasattr(socket, "gethostname") else "",
            "cwd": os.getcwd(),
        },
        "dirs": {"skill_dir": SKILL_DIR, "storage_dir": STORAGE_DIR},
    }


def _load_env():
    """读取已保存的环境配置；不存在时按当前 OS 推断（不落盘）。"""
    if os.path.exists(ENV_PATH):
        try:
            with open(ENV_PATH, "r", encoding="utf-8") as f:
                env = json.load(f)
            if isinstance(env, dict) and env.get("path_style") in _PATH_STYLES:
                return env
        except Exception:
            pass
    env = _detect_env()
    env["_inferred"] = True
    return env


def _save_env(env):
    os.makedirs(SKILL_DIR, exist_ok=True)
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        json.dump(env, f, ensure_ascii=False, indent=2)


def _get_path_style():
    return _load_env().get("path_style") or ("windows" if os.name == "nt" else "posix")


def _norm_path(p):
    """按当前 path_style 统一本地路径分隔符（仅用于工作空间等本地路径，不用于 URL）。"""
    p = _norm(p)
    if not p:
        return p
    style = _get_path_style()
    if style == "windows":
        return p.replace("/", "\\")
    if style == "posix":
        return p.replace("\\", "/")
    return p


# ---- 存储文件读写 ---------------------------------------------------------
def _file_path(date):
    """date 为 'YYYY-MM-DD'。返回存储文件绝对路径。"""
    return os.path.join(STORAGE_DIR, date + ".md")


def _parse_from(v):
    """把 '来源' 行（`type|url`）解析为 {type,url} 对象；空则返回 None。"""
    v = _norm(v)
    if not v:
        return None
    if "|" in v:
        typ, _, url = v.partition("|")
        return {"type": _norm(typ) or None, "url": _norm(url) or None}
    return {"type": "link", "url": v}


def _parse_block(block_lines):
    """解析一个任务的 key: value 行列表为 dict。"""
    task = {"id": None, "status": DEFAULT_STATUS, "importance": DEFAULT_IMPORTANCE, "text": "",
            "note": "", "workspace": "", "docs": [], "links": [], "depends_on": [], "due": None,
            "created": None, "updated": None, "urgent": DEFAULT_URGENT, "from": None,
            "parent": "", "tags": []}
    for line in block_lines:
        line = line.strip()
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k, v = k.strip(), _norm(v)
        if k == "状态":
            task["status"] = _norm_status(v.split()[-1])
        elif k == "重要":
            task["importance"] = _norm_importance(v)
        elif k == "内容":
            task["text"] = v
        elif k == "父任务":
            task["parent"] = v
        elif k == "备注":
            task["note"] = v
        elif k == "工作空间":
            task["workspace"] = _norm_path(v)
        elif k == "文档":
            task["docs"] = [x.strip() for x in v.split(";") if x.strip()]
        elif k == "链接":
            task["links"] = [x.strip() for x in v.split(";") if x.strip()]
        elif k == "依赖":
            task["depends_on"] = [x.strip() for x in v.split(";") if x.strip()]
        elif k == "标签":
            task["tags"] = [x.strip() for x in v.split(";") if x.strip()]
        elif k == "来源":
            task["from"] = _parse_from(v)
        elif k == "截止":
            task["due"] = v or None
        elif k == "创建":
            task["created"] = v or None
        elif k == "更新":
            task["updated"] = v or None
        elif k == "紧急":
            task["urgent"] = _norm_urgent(v)
    return task


def load_file(date):
    """读取存储文件，返回 {'archived': bool, 'tasks': [dict,...]}；不存在则返回空。"""
    fp = _file_path(date)
    result = {"archived": False, "tasks": []}
    if not os.path.exists(fp):
        return result
    with open(fp, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    i = 0
    # 头：归档标记
    while i < len(lines):
        line = lines[i].strip()
        if line == "---":
            i += 1
            break
        if line.startswith("归档:"):
            result["archived"] = _norm(line[3:]).lower() in ("true", "1", "是")
        i += 1
    # 任务块（跳过空行，避免把空行误判成空任务块）
    blocks = []
    cur = []
    for line in lines[i:]:
        if not line.strip():
            continue
        if line.startswith("## "):
            if cur:
                blocks.append(cur)
            cur = [line]
        else:
            cur.append(line)
    if cur:
        blocks.append(cur)
    for blk in blocks:
        if not blk:
            continue
        body = blk[0][3:].strip()  # id
        tid = body.split(" ")[0] if body else None
        t = _parse_block(blk[1:])
        t["id"] = tid or t["id"]
        if t["id"]:
            result["tasks"].append(t)
    return result


def _write_block(t):
    lines = []
    lines.append("## %s" % t["id"])
    mark = STATUS_MARKS.get(t["status"], "[ ]")
    lines.append("状态: %s %s" % (mark, t["status"]))
    lines.append("重要: %s" % t["importance"])
    lines.append("紧急: %s" % t["urgent"])
    lines.append("内容: %s" % t["text"])
    if t.get("parent"):
        lines.append("父任务: %s" % t["parent"])
    if t.get("note"):
        lines.append("备注: %s" % t["note"])
    if t.get("workspace"):
        lines.append("工作空间: %s" % t["workspace"])
    if t.get("docs"):
        lines.append("文档: %s" % "; ".join(t["docs"]))
    if t.get("links"):
        lines.append("链接: %s" % "; ".join(t["links"]))
    if t.get("depends_on"):
        lines.append("依赖: %s" % "; ".join(t["depends_on"]))
    if t.get("tags"):
        lines.append("标签: %s" % "; ".join(t["tags"]))
    if t.get("from") and (t["from"].get("type") or t["from"].get("url")):
        lines.append("来源: %s|%s" % (t["from"].get("type") or "",
                                      t["from"].get("url") or ""))
    if t.get("due"):
        lines.append("截止: %s" % t["due"])
    if t.get("created"):
        lines.append("创建: %s" % t["created"])
    if t.get("updated"):
        lines.append("更新: %s" % t["updated"])
    return "\n".join(lines)


def save_file(date, archived, tasks):
    """写回存储文件，保持 header + 任务块。"""
    fp = _file_path(date)
    os.makedirs(STORAGE_DIR, exist_ok=True)
    parts = ["# 待办 %s" % date, "归档: %s" % ("true" if archived else "false"), "---"]
    for t in tasks:
        parts.append("")
        parts.append(_write_block(t))
    with open(fp, "w", encoding="utf-8") as f:
        f.write("\n".join(parts) + "\n")


def _next_id(date):
    data = load_file(date)
    base = date.replace("-", "")
    seq = 0
    for t in data["tasks"]:
        m = re.match(r"^T-%s-(\d+)$" % base, t["id"] or "")
        if m:
            seq = max(seq, int(m.group(1)))
    return "T-%s-%03d" % (base, seq + 1)


# ---- 索引 ---------------------------------------------------------------
def _due_value(due):
    """把 due 转成可比较的时间戳；解析失败返回 None。"""
    if not due:
        return None
    try:
        return dt.datetime.strptime(due, "%Y-%m-%d %H:%M")
    except ValueError:
        try:
            return dt.datetime.strptime(due, "%Y-%m-%d")
        except ValueError:
            return None


def _is_urgent(t, now):
    """显式紧急，或截止在 24 小时内/已过期，视为紧急。"""
    if t.get("urgent") == "紧急":
        return True
    dv = _due_value(t.get("due"))
    if dv is None:
        return False
    return dv <= now + dt.timedelta(hours=24)


def _scan_files():
    """返回 {date: {'archived':bool,'tasks':[...]}}，按日期升序。"""
    if not os.path.isdir(STORAGE_DIR):
        return {}
    out = {}
    for fn in sorted(os.listdir(STORAGE_DIR)):
        if not fn.endswith(".md"):
            continue
        date = fn[:-3]
        try:
            dt.datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            continue
        out[date] = load_file(date)
    return out


def build_index(now=None):
    """重建 index.json。返回 dict。"""
    now = now or dt.datetime.now()
    files = _scan_files()
    urgent, in_progress, todo_, done = [], [], [], []
    archived_files = []
    total = done_total = 0
    imp_rank = {"重要": 0, "不重要": 1}

    def item_key(it):
        # 紧急优先，然后重要性（高>中>低），再按创建时间倒序
        return (0 if it["urgent"] else 1,
                imp_rank.get(it.get("importance"), 1),
                _due_value(it.get("due")) is None,
                -(now.timestamp() if it.get("created") else 0))

    for date, data in files.items():
        if data["archived"]:
            archived_files.append(date)
        for t in data["tasks"]:
            total += 1
            item = {
                "id": t["id"], "text": t["text"], "importance": t["importance"],
                "status": t["status"], "due": t["due"], "file": date,
                "created": t.get("created"), "urgent": bool(_is_urgent(t, now)),
                "workspace": t.get("workspace"), "links": t.get("links"),
                "depends_on": t.get("depends_on"), "blocked": _has_unfinished_deps(t),
                "parent": t.get("parent") or "",
                "from": t.get("from"),
                "tags": t.get("tags") or [],
            }
            if t["status"] == "结束":
                done.append(item)
                done_total += 1
            elif t["status"] == "进行中":
                in_progress.append(item)
            else:
                todo_.append(item)
            if _is_urgent(t, now) and t["status"] != "结束":
                urgent.append(item)
    urgent.sort(key=item_key)
    in_progress.sort(key=item_key)
    todo_.sort(key=item_key)
    done.sort(key=item_key)
    index = {
        "generated": _fmt(now),
        "summary": {
            "todo": len(todo_), "in_progress": len(in_progress), "urgent": len(urgent),
            "done": len(done), "total": total, "archived_files": len(archived_files),
            "files": len(files),
        },
        "urgent": urgent,
        "in_progress": in_progress,
        "todo": todo_,
        "done": done,
        "archived_files": archived_files,
    }
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    return index


# ---- 重复检测 -----------------------------------------------------------
_PUNCT_RE = re.compile(
    r"[\s\u3000，。！？、；：\u201c\u201d\u2018\u2019（）()\[\]【】《》<>·…—\-_/"
    r"\\|,.;:!?\'\"`~@#$%^&*+=]+"
)


def _normalize_text(text):
    """归一化任务内容：去空白/标点、英文小写，便于相同与近似比对。"""
    s = _norm(text).lower()
    s = _PUNCT_RE.sub("", s)
    return s


def _text_similarity(a, b):
    """返回 0~1 相似度；一方包含另一方且较短方足够长时抬高为近似。"""
    na, nb = _normalize_text(a), _normalize_text(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ratio = SequenceMatcher(None, na, nb).ratio()
    shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(shorter) >= 4 and shorter in longer:
        contain = len(shorter) / max(len(longer), 1)
        ratio = max(ratio, 0.70 + 0.30 * contain)
    return ratio


def find_duplicates(text, exclude_id=None):
    """扫描全部任务，返回 [{'kind','score','date','task'}, ...]。
    kind: exact（归一化相同） / similar（相似度达阈值）。
    优先列出未结束任务；exact 排在 similar 前，同分按创建倒序。"""
    text = _norm(text)
    if not text:
        return []
    exact, similar = [], []
    for date, data in _scan_files().items():
        for t in data["tasks"]:
            if exclude_id and t.get("id") == exclude_id:
                continue
            score = _text_similarity(text, t.get("text") or "")
            if score >= 1.0 or _normalize_text(text) == _normalize_text(t.get("text") or ""):
                exact.append({"kind": "exact", "score": 1.0, "date": date, "task": t})
            elif score >= SIMILAR_THRESHOLD:
                similar.append({"kind": "similar", "score": score, "date": date, "task": t})

    def rank(item):
        t = item["task"]
        done = 1 if t.get("status") == "结束" else 0
        created = t.get("created") or ""
        return (done, -item["score"], created)

    exact.sort(key=rank)
    similar.sort(key=rank)
    return exact + similar


def _print_dup_hits(hits, title="发现重复或近似任务"):
    print(title)
    for h in hits:
        t = h["task"]
        kind_label = "相同" if h["kind"] == "exact" else "近似(%.0f%%)" % (h["score"] * 100)
        print("  [%s] %s | %s | %s | %s" % (
            kind_label, t.get("id"), h["date"], t.get("status"), t.get("text")))
        if t.get("workspace"):
            print("         工作空间: %s" % t["workspace"])


def _split_list_field(raw):
    return [x.strip() for x in (raw or "").split(";") if x.strip()]


def _apply_task_fields(task, args, now, *, is_new):
    """把 add 参数落到 task。新建时填默认值；更新时仅覆盖用户显式传入的字段。"""
    if args.status is not None:
        task["status"] = args.status if args.status in STATUS_SET else DEFAULT_STATUS
    elif is_new:
        task["status"] = DEFAULT_STATUS

    if args.importance is not None:
        task["importance"] = args.importance if args.importance in IMPORTANCE_SET else DEFAULT_IMPORTANCE
    elif is_new:
        task["importance"] = DEFAULT_IMPORTANCE

    if args.urgent is not None:
        task["urgent"] = args.urgent if args.urgent in URGENT_SET else DEFAULT_URGENT
    elif is_new:
        task["urgent"] = DEFAULT_URGENT

    if args.note is not None:
        task["note"] = _norm(args.note)
    elif is_new:
        task["note"] = ""

    if args.workspace is not None:
        task["workspace"] = _norm_path(args.workspace)
    elif is_new:
        task["workspace"] = ""

    if args.docs is not None:
        task["docs"] = _split_list_field(args.docs)
    elif is_new:
        task["docs"] = []

    if args.links is not None:
        task["links"] = _split_list_field(args.links)
    elif is_new:
        task["links"] = []

    if args.deps is not None:
        task["depends_on"] = _split_list_field(args.deps)
    elif is_new:
        task["depends_on"] = []

    if getattr(args, "tags", None) is not None:
        task["tags"] = _split_list_field(args.tags)
    elif is_new:
        task["tags"] = []

    if args.parent is not None:
        task["parent"] = _norm(args.parent)
    elif is_new:
        task["parent"] = ""

    if args.due is not None:
        task["due"] = parse_due(args.due, now) if args.due else None
    elif is_new:
        task["due"] = None

    if args.from_url is not None:
        url = _norm(args.from_url)
        task["from"] = ({"type": _norm(args.from_type) or "agent", "url": url} if url else None)
    elif is_new:
        task["from"] = None

    if is_new:
        task["text"] = _norm(args.text)
        task["created"] = _fmt(now)
        task["updated"] = None
    else:
        # 更新时若传入 text，同步刷新内容（用于近似确认后纠正措辞）
        if args.text is not None and _norm(args.text):
            task["text"] = _norm(args.text)
        task["updated"] = _fmt(now)


def _print_task_summary(prefix, tid, date, task, idx):
    print("%s %s：%s" % (prefix, tid, task["text"]))
    print("  日期: %s | 重要: %s | 状态: %s | 紧急: %s" % (
        date, task["importance"], task["status"], task["urgent"]))
    if task.get("due"):
        print("  截止: %s" % task["due"])
    if task.get("note"):
        print("  备注: %s" % task["note"])
    if task.get("workspace"):
        print("  工作空间: %s" % task["workspace"])
    if task.get("docs"):
        print("  文档: %s" % "; ".join(task["docs"]))
    if task.get("links"):
        print("  链接: %s" % "; ".join(task["links"]))
    if task.get("depends_on"):
        print("  依赖: %s" % "; ".join(task["depends_on"]))
    if task.get("tags"):
        print("  标签: %s" % "; ".join(task["tags"]))
    if task.get("parent"):
        print("  父任务: %s" % task["parent"])
    if task.get("from") and task["from"].get("url"):
        print("  来源: %s | %s" % (task["from"].get("type"), task["from"].get("url")))
    print("  索引已更新：todo=%d in_progress=%d urgent=%d" % (
        idx["summary"]["todo"], idx["summary"]["in_progress"], idx["summary"]["urgent"]))


def _update_existing_task(tid, args, now):
    """按 add 参数更新已有任务；维护归档不变量。"""
    date, data, t = _find_task(tid)
    if t is None:
        print("未找到任务 %s" % tid, file=sys.stderr)
        return 2
    old_status = t.get("status")
    _apply_task_fields(t, args, now, is_new=False)
    # 依赖校验：存在性 + 自依赖 + 环
    deps = t.get("depends_on") or []
    missing = _missing_deps(deps)
    if missing:
        print("错误：以下依赖任务不存在：%s" % "; ".join(missing), file=sys.stderr)
        return 2
    if tid in deps:
        print("错误：任务不能依赖自身（%s）。" % tid, file=sys.stderr)
        return 2
    if _would_create_cycle(tid, deps):
        print("错误：该依赖设置会形成循环依赖（%s 间接依赖自身）。" % tid, file=sys.stderr)
        return 2
    rc = _guard_by_deps(deps, t["status"], args.force)
    if rc != 0:
        return rc
    # 父任务校验：存在性 + 自引用 + 环
    parent = _norm(t.get("parent"))
    if parent:
        if _find_task(parent)[2] is None:
            print("错误：父任务 %s 不存在。" % parent, file=sys.stderr)
            return 2
        if parent == tid:
            print("错误：任务不能作为自身的父任务（%s）。" % tid, file=sys.stderr)
            return 2
        if _would_create_parent_cycle(tid, parent):
            print("错误：该归属会形成循环（%s 成为 %s 的父任务会成环）。" % (parent, tid), file=sys.stderr)
            return 2
    rc = _guard_children_done(tid, t["status"], args.force)
    if rc != 0:
        return rc
    all_done = all(x["status"] == "结束" for x in data["tasks"])
    data["archived"] = all_done
    save_file(date, data["archived"], data["tasks"])
    idx = build_index(now)
    _print_task_summary("已更新（未新建）", tid, date, t, idx)
    if old_status != t["status"]:
        print("  状态变更: %s -> %s" % (old_status, t["status"]))
    if all_done:
        print("  该文件全部完成，已标记归档")
    elif old_status == "结束" and t["status"] != "结束":
        print("  已取消归档标记")
    return 0


# ---- 命令实现 -----------------------------------------------------------
def cmd_add(args, now=None):
    now = now or dt.datetime.now()
    date = args.date or now.strftime("%Y-%m-%d")
    text = _norm(args.text)
    if not text:
        print("错误：必须提供 --text 任务内容。", file=sys.stderr)
        return 2

    # 显式指定更新目标：跳过重复检测，直接更新
    if args.update_id:
        # 确认更新时未给状态 → 默认拉回「进行中」（避免只复述内容却不推进状态）
        if args.status is None:
            args.status = DEFAULT_STATUS
        return _update_existing_task(args.update_id, args, now)

    hits = [] if args.force else find_duplicates(text)
    exact_hits = [h for h in hits if h["kind"] == "exact"]
    similar_hits = [h for h in hits if h["kind"] == "similar"]

    # 相同或近似：先不落盘，交由会话确认（相同默认建议更新状态）
    if hits and not args.force:
        if exact_hits:
            _print_dup_hits(exact_hits, title="检测到相同任务（默认应更新状态，勿新建）：")
            if similar_hits:
                _print_dup_hits(similar_hits, title="同时发现近似任务：")
            print("确认更新请加：--update-id <id>（默认推荐）；确认仍要新建请加：--force")
        else:
            _print_dup_hits(similar_hits, title="检测到近似任务，请确认后再操作：")
            print("确认更新请加：--update-id <id>；确认仍要新建请加：--force")
        return EXIT_NEEDS_CONFIRM

    data = load_file(date)
    if data["archived"]:
        # 归档文件不追加新任务：取消归档标记（视为重新打开该日文件）
        data["archived"] = False
    tid = _next_id(date)
    task = {
        "id": tid, "status": DEFAULT_STATUS, "importance": DEFAULT_IMPORTANCE,
        "text": text, "note": "", "workspace": "", "docs": [], "links": [],
        "depends_on": [], "due": None, "created": None, "updated": None,
        "urgent": DEFAULT_URGENT, "from": None, "parent": "", "tags": [],
    }
    _apply_task_fields(task, args, now, is_new=True)
    # 新任务的依赖校验：存在性 + 硬检查（新建即以 进行中/结束 起步时同样受依赖约束）
    missing = _missing_deps(task.get("depends_on") or [])
    if missing:
        print("错误：以下依赖任务不存在：%s" % "; ".join(missing), file=sys.stderr)
        return 2
    rc = _guard_by_deps(task.get("depends_on"), task["status"], args.force)
    if rc != 0:
        return rc
    # 新任务的父任务校验：存在性（新建任务尚无父子环）
    parent = _norm(task.get("parent"))
    if parent and _find_task(parent)[2] is None:
        print("错误：父任务 %s 不存在。" % parent, file=sys.stderr)
        return 2
    data["tasks"].append(task)
    save_file(date, data["archived"], data["tasks"])
    idx = build_index(now)
    prefix = "已强制新建" if args.force else "已添加"
    _print_task_summary(prefix, tid, date, task, idx)
    return 0


def cmd_check(args):
    """检查文本是否与已有任务相同/近似（只读，不落盘）。"""
    text = _norm(args.text)
    if not text:
        print("错误：必须提供 --text。", file=sys.stderr)
        return 2
    hits = find_duplicates(text, exclude_id=args.exclude_id or None)
    if not hits:
        print("无重复或近似任务")
        return 0
    _print_dup_hits(hits)
    exact_n = sum(1 for h in hits if h["kind"] == "exact")
    print("合计：相同 %d，近似 %d" % (exact_n, len(hits) - exact_n))
    return EXIT_NEEDS_CONFIRM if hits else 0


def _find_task(tid):
    for date, data in _scan_files().items():
        for t in data["tasks"]:
            if t["id"] == tid:
                return date, data, t
    return None, None, None


# ---- 依赖链 ---------------------------------------------------------------
def _all_tasks_map():
    """id -> (date, data, task) 全表，用于批量存在性/环检测。"""
    m = {}
    for date, data in _scan_files().items():
        for t in data["tasks"]:
            m[t["id"]] = (date, data, t)
    return m


def _missing_deps(dep_ids):
    """返回依赖列表中不存在的任务 ID。"""
    allmap = _all_tasks_map()
    return [d for d in dep_ids if d not in allmap]


def _would_create_cycle(root_id, new_dep_ids):
    """沿「依赖」正向传播：若任一新依赖能（间接）到达 root，则新增会成环。"""
    allmap = _all_tasks_map()

    def forward(start):
        t = allmap.get(start)
        if not t:
            return ()
        return t[2].get("depends_on") or ()

    for nd in new_dep_ids:
        seen = set()
        stack = list(forward(nd))
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            if cur == root_id:
                return True
            stack.extend(forward(cur))
    return False


def _has_unfinished_deps(t):
    """任务 t 是否存在未结束（或不存在）的直接依赖。"""
    for did in t.get("depends_on") or []:
        _, _, dtask = _find_task(did)
        if dtask is None or dtask["status"] != "结束":
            return True
    return False


def _guard_by_deps(dep_ids, target_status, force):
    """依赖硬检查（死的规则）：target_status 为「进行中/结束」且存在未结束的直接依赖时，
    拒绝推进并返回 EXIT_NEEDS_CONFIRM（exit 3），由会话中的 AI 向用户确认后决定是否 --force。
    force=True 或目标状态非推进态（待开始/维护/其他）时不拦截。返回 0=通过。"""
    if force or target_status not in ("进行中", "结束"):
        return 0
    conflicts = []
    for did in dep_ids or []:
        ddate, _, dtask = _find_task(did)
        if dtask is None:
            conflicts.append({"id": did, "status": "(不存在)", "text": "", "date": ""})
        elif dtask["status"] != "结束":
            conflicts.append({"id": did, "status": dtask["status"], "text": dtask["text"], "date": ddate})
    if conflicts:
        print("依赖检查未通过（前置依赖尚未结束，不能推进到「%s」）：" % target_status)
        for c in conflicts:
            print("  - %s [%s] %s %s" % (c["id"], c["status"], c["date"], c["text"]))
        print("提示：如需强行推进请加 --force（由会话中的 AI 在用户确认后调用）；"
              "或先处理未完成的依赖任务。")
        return EXIT_NEEDS_CONFIRM
    return 0


# ---- 子任务 ---------------------------------------------------------------
def _children_of(pid):
    """返回父任务 pid 的全部子任务 [(date, task), ...]。"""
    return [(date, t) for date, _, t in _all_tasks_map().values()
            if (t.get("parent") or "") == pid]


def _would_create_parent_cycle(tid, parent_id):
    """把 parent_id 设为 tid 的父任务时，沿父链上溯是否成环（或已存在环）。"""
    allmap = _all_tasks_map()
    cur, seen = parent_id, set()
    while cur:
        if cur == tid:
            return True
        if cur in seen:
            return True
        seen.add(cur)
        t = allmap.get(cur)
        cur = (t[2].get("parent") or "") if t else None
    return False


def _guard_children_done(tid, target_status, force):
    """父任务完成硬检查：target_status 为「结束」且存在未完成的子任务时，拒绝并返回
    EXIT_NEEDS_CONFIRM（exit 3），由 AI 向用户确认后决定是否 --force。返回 0=通过。"""
    if force or target_status != "结束":
        return 0
    unfinished = [(date, t) for date, t in _children_of(tid) if t["status"] != "结束"]
    if unfinished:
        print("子任务检查未通过（存在未完成的子任务，不能将父任务标记为「结束」）：")
        for date, t in unfinished:
            print("  - %s [%s] %s %s" % (t["id"], t["status"], date, t["text"]))
        print("提示：如需强行结束请加 --force（由会话中的 AI 在用户确认后调用）；"
              "或先完成子任务。")
        return EXIT_NEEDS_CONFIRM
    return 0


def cmd_children(args):
    """列出某主任务下的全部子任务。"""
    pid = args.id
    _, _, pt = _find_task(pid)
    if pt is None:
        print("未找到父任务 %s" % pid, file=sys.stderr)
        return 2
    children = _children_of(pid)
    children.sort(key=lambda c: (c[1].get("created") or ""))
    print("父任务 %s：%s（%d 个子任务）" % (pid, pt["text"], len(children)))
    for date, t in children:
        flag = STATUS_MARKS.get(t["status"], "[ ]")
        urgent_mark = "!" if _is_urgent(t, dt.datetime.now()) else " "
        print("  %s %s [%s%s] %s · %s · %s" % (
            t["id"], flag, urgent_mark, t["importance"], t["text"],
            t["due"] or "无截止", t["status"]))
        if t.get("depends_on"):
            print("     依赖: %s" % "; ".join(t["depends_on"]))
    if not children:
        print("  （无子任务）")
    return 0


def cmd_deparent(args):
    """解除子任务与父任务的归属关系。"""
    date, data, t = _find_task(args.id)
    if t is None:
        print("未找到任务 %s" % args.id, file=sys.stderr)
        return 2
    if not t.get("parent"):
        print("%s 当前没有父任务。" % args.id)
        return 0
    parent_id = t["parent"]
    t["parent"] = ""
    t["updated"] = _fmt(dt.datetime.now())
    data["archived"] = all(x["status"] == "结束" for x in data["tasks"])
    save_file(date, data["archived"], data["tasks"])
    build_index()
    print("%s 已从父任务 %s 下解除归属。" % (args.id, parent_id))
    return 0


def cmd_dep(args):
    """设置 / 查看任务依赖。模式：
      dep <id>               查看
      dep <id> A B ...       设置（替换）依赖
      dep <id> --add A B     追加依赖
      dep <id> --remove A B  移除依赖
      dep <id> --clear       清空依赖
    任何改动都会做存在性校验与循环依赖检测。"""
    date, data, t = _find_task(args.id)
    if t is None:
        print("未找到任务 %s" % args.id, file=sys.stderr)
        return 2

    deps = list(t.get("depends_on") or [])
    if args.deps:
        deps = list(args.deps)
    elif args.add:
        deps.extend(args.add)
    if args.remove:
        rm = set(args.remove)
        deps = [d for d in deps if d not in rm]
    if args.clear:
        deps = []
    # 去重保序
    uniq, seen = [], set()
    for d in deps:
        if d not in seen:
            seen.add(d)
            uniq.append(d)
    deps = uniq

    missing = _missing_deps(deps)
    if missing:
        print("错误：以下依赖任务不存在：%s" % "; ".join(missing), file=sys.stderr)
        return 2
    if args.id in deps:
        print("错误：任务不能依赖自身（%s）。" % args.id, file=sys.stderr)
        return 2
    if _would_create_cycle(args.id, deps):
        print("错误：该依赖设置会形成循环依赖（%s 间接依赖自身）。" % args.id, file=sys.stderr)
        return 2

    changed = deps != (t.get("depends_on") or [])
    if changed:
        t["depends_on"] = deps
        t["updated"] = _fmt(dt.datetime.now())
        data["archived"] = all(x["status"] == "结束" for x in data["tasks"])
        save_file(date, data["archived"], data["tasks"])
        build_index()

    print("%s 依赖（%d 条）：" % (args.id, len(deps)))
    for did in deps:
        ddate, _, dtask = _find_task(did)
        print("  - %s [%s] %s %s" % (did, dtask["status"] if dtask else "(不存在)",
                                     ddate, dtask["text"] if dtask else ""))
    if not deps:
        print("  （无依赖）")
    return 0


def _set_status(tid, status, force=False):
    date, data, t = _find_task(tid)
    if t is None:
        print("未找到任务 %s" % tid, file=sys.stderr)
        return 2
    rc = _guard_by_deps(t.get("depends_on"), status, force)
    if rc != 0:
        return rc
    rc = _guard_children_done(tid, status, force)
    if rc != 0:
        return rc
    t["status"] = status
    t["updated"] = _fmt(dt.datetime.now())
    # 归档不变量：归档 ⇔ 该文件所有任务均为 结束
    all_done = all(x["status"] == "结束" for x in data["tasks"])
    data["archived"] = all_done
    save_file(date, data["archived"], data["tasks"])
    msg = "%s -> %s（%s）" % (tid, status, t["text"])
    if status == "结束" and all_done:
        msg += "；该文件全部完成，已标记归档"
    elif status != "结束" and not all_done:
        msg += "；已取消归档标记"
    build_index()
    print(msg)
    return 0


def _find_cursor():
    """定位 Cursor 可执行文件，返回命令列表（可加路径参数）。"""
    import shutil
    cli = shutil.which("cursor")
    if cli:
        return [cli]
    cands = [
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\cursor\Cursor.exe"),
        r"C:\Program Files\cursor\Cursor.exe",
        r"C:\Program Files\cursor\resources\app\bin\cursor.cmd",
    ]
    for c in cands:
        if os.path.exists(c):
            return [c]
    return []


def cmd_work(args):
    """继续一个待办：标记进行中，若有工作区目录则用 Cursor 打开。"""
    date, data, t = _find_task(args.id)
    if t is None:
        print("未找到任务 %s" % args.id, file=sys.stderr)
        return 2
    rc = _guard_by_deps(t.get("depends_on"), "进行中", args.force)
    if rc != 0:
        return rc
    t["status"] = "进行中"
    t["updated"] = _fmt(dt.datetime.now())
    all_done = all(x["status"] == "结束" for x in data["tasks"])
    data["archived"] = all_done
    save_file(date, data["archived"], data["tasks"])
    build_index()
    print("已标记进行中：%s %s" % (args.id, t["text"]))
    ws = _norm(t.get("workspace"))
    if ws:
        cmd = _find_cursor()
        if cmd:
            import subprocess
            try:
                subprocess.Popen(cmd + [ws])
                print("已用 Cursor 打开工作区：%s" % ws)
            except Exception as e:
                print("用 Cursor 打开工作区失败：%s" % e, file=sys.stderr)
                print("工作区目录：%s" % ws)
        else:
            print("未找到 Cursor，工作区目录：%s" % ws)
    else:
        print("该待办未设置工作区目录，未打开 Cursor。")
    return 0


def cmd_done(args):
    return _set_status(args.id, "结束", args.force)


def cmd_start(args):
    return _set_status(args.id, "进行中", args.force)


def cmd_reopen(args):
    return _set_status(args.id, "待开始")


def cmd_archive(args):
    date = args.date
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        print("错误：date 需为 YYYY-MM-DD", file=sys.stderr)
        return 2
    if not os.path.exists(_file_path(date)):
        print("文件不存在：%s" % _file_path(date), file=sys.stderr)
        return 2
    data = load_file(date)
    if data["tasks"] and not all(t["status"] == "结束" for t in data["tasks"]):
        print("仍有未完成任务，拒绝强制归档（先完成或 reopen）。可手动编辑文件。", file=sys.stderr)
        return 2
    data["archived"] = True
    save_file(date, True, data["tasks"])
    build_index()
    print("已归档 %s" % date)
    return 0


def cmd_list(args, now=None):
    now = now or dt.datetime.now()
    if args.date:
        data = load_file(args.date)
        files = {args.date: data}
    else:
        files = _scan_files()
    state = args.state or "all"
    lines = []
    for date in sorted(files):
        data = files[date]
        for t in data["tasks"]:
            if state == "urgent" and not _is_urgent(t, now):
                continue
            if state not in ("all", "urgent") and t["status"] != state:
                continue
            if state == "urgent":
                pass
            flag = STATUS_MARKS.get(t["status"], "[ ]")
            urgent_mark = "!" if _is_urgent(t, now) else " "
            lines.append("%s %s %s [%s%s] %s · %s · %s"
                         % (t["id"], date, flag, urgent_mark, t["importance"], t["text"],
                            t["due"] or "无截止", t["status"]))
            if t.get("note"):
                lines.append("   备注: %s" % t["note"])
            if t.get("parent"):
                lines.append("   父任务: %s" % t["parent"])
            if t.get("depends_on"):
                blocked = "（被依赖阻塞）" if _has_unfinished_deps(t) else ""
                lines.append("   依赖: %s%s" % ("; ".join(t["depends_on"]), blocked))
            if t.get("tags"):
                lines.append("   标签: %s" % "; ".join(t["tags"]))
            if t.get("workspace"):
                lines.append("   工作空间: %s" % t["workspace"])
            if t.get("links"):
                lines.append("   链接: %s" % "; ".join(t["links"]))
    if not lines:
        print("（无 %s 状态的任务）" % state)
        return 0
    print("\n".join(lines))
    return 0


def cmd_show(args):
    tid = args.id
    if re.match(r"^\d{4}-\d{2}-\d{2}$", tid):
        date = tid
        data = load_file(date)
        print("# 待办 %s（归档: %s）" % (date, data["archived"]))
        for t in data["tasks"]:
            print(_write_block(t))
            print("")
        return 0
    date, data, t = _find_task(tid)
    if t is None:
        print("未找到任务 %s" % tid, file=sys.stderr)
        return 2
    print("文件: %s（归档: %s）" % (date, data["archived"]))
    print(_write_block(t))
    return 0


def cmd_index(args):
    if args.rebuild:
        idx = build_index()
    else:
        if not os.path.exists(INDEX_PATH):
            idx = build_index()
        else:
            with open(INDEX_PATH, "r", encoding="utf-8") as f:
                idx = json.load(f)
    print(json.dumps(idx, ensure_ascii=False, indent=2))
    return 0


def _reformat_all_workspaces(style):
    """按指定路径风格重写全部存储文件中的工作空间，返回改动文件数。

    注意：这里直接操作存储文件原文（不经 load_file 的读取期规范化），
    以确保落盘内容真正统一为指定风格，而不仅是读取时归一化。"""
    if style not in ("windows", "posix"):
        return 0
    if not os.path.isdir(STORAGE_DIR):
        return 0
    changed = 0
    for fn in sorted(os.listdir(STORAGE_DIR)):
        if not fn.endswith(".md"):
            continue
        date = fn[:-3]
        try:
            dt.datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            continue
        fp = os.path.join(STORAGE_DIR, fn)
        with open(fp, "r", encoding="utf-8") as f:
            raw = f.read()
        new_raw = _reformat_workspace_lines(raw, style)
        if new_raw != raw:
            with open(fp, "w", encoding="utf-8") as f:
                f.write(new_raw)
            changed += 1
    if changed:
        build_index()
    return changed


def _reformat_workspace_lines(raw, style):
    """把存储文件文本中所有 `工作空间: <path>` 行的分隔符统一为指定风格。"""
    if style == "windows":
        return re.sub(r"^(工作空间:\s*)(.+)$",
                      lambda m: m.group(1) + m.group(2).replace("/", "\\"),
                      raw, flags=re.M)
    return re.sub(r"^(工作空间:\s*)(.+)$",
                  lambda m: m.group(1) + m.group(2).replace("\\", "/"),
                  raw, flags=re.M)


def cmd_init(args):
    """识别当前工作环境并保存到 env.json，同时把已有工作空间统一为当前路径风格。"""
    env = _detect_env()
    _save_env(env)
    n = _reformat_all_workspaces(env["path_style"])
    print("已识别并保存工作环境 -> %s" % ENV_PATH)
    print(json.dumps(env, ensure_ascii=False, indent=2))
    if n:
        print("已将 %d 个存储文件中的工作空间统一为 %s 格式" % (n, env["path_style"]))
    else:
        print("工作空间已统一为 %s 格式" % env["path_style"])
    return 0


def cmd_env(args):
    """查看 / 修改已保存的环境配置。--set KEY=VALUE 可多次；--reset 重新探测。"""
    env = _load_env()
    if args.reset:
        env = _detect_env()
    for kv in args.set or []:
        if "=" not in kv:
            print("错误：--set 需为 KEY=VALUE 形式，例如 path_style=windows", file=sys.stderr)
            return 2
        k, _, v = kv.partition("=")
        k, v = k.strip(), v.strip()
        if k == "path_style":
            if v not in _PATH_STYLES:
                print("错误：path_style 仅支持 %s" % " / ".join(_PATH_STYLES), file=sys.stderr)
                return 2
            env["path_style"] = v
        else:
            print("错误：不支持的配置键 %s（当前仅支持 path_style）" % k, file=sys.stderr)
            return 2
    if args.reset or args.set:
        env["updated"] = _fmt(dt.datetime.now())
        _save_env(env)
    reformat_n = 0
    if args.reformat_workspaces:
        reformat_n = _reformat_all_workspaces(env.get("path_style"))
    out = {k: v for k, v in env.items() if not k.startswith("_")}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if args.reformat_workspaces:
        if reformat_n:
            print("已将 %d 个存储文件中的工作空间统一为 %s 格式" % (reformat_n, env["path_style"]))
        else:
            print("工作空间已统一为 %s 格式" % env["path_style"])
    elif env.get("_inferred"):
        print("（提示：环境配置尚未保存，可运行 `init` 命令识别并保存。）")
    return 0


def main():
    p = argparse.ArgumentParser(prog="awam-todo", description="输入与管理 To-Do")
    sub = p.add_subparsers(dest="cmd")

    pa = sub.add_parser("add", help="添加一条待办（相同则默认更新；近似则需确认）")
    pa.add_argument("--text", required=True)
    # 下列字段默认 None：更新已有任务时仅覆盖用户显式传入的项
    pa.add_argument("--importance", choices=["重要", "不重要"], default=None)
    pa.add_argument("--urgent", choices=["紧急", "不紧急"], default=None, help="紧急程度")
    pa.add_argument("--status", choices=["维护", "进行中", "结束", "待开始", "其他"], default=None)
    pa.add_argument("--note", default=None)
    pa.add_argument("--workspace", default=None)
    pa.add_argument("--docs", default=None, help="相关文档，用 ; 分隔")
    pa.add_argument("--links", default=None, help="链接，用 ; 分隔")
    pa.add_argument("--deps", default=None, help="依赖任务ID，用 ; 分隔（如 T-A;T-B）")
    pa.add_argument("--tags", default=None, help="标签，用 ; 分隔（如 工作;紧急）")
    pa.add_argument("--parent", default=None, help="父任务ID（作为其子任务）")
    pa.add_argument("--due", default=None, help="自然语言时间，如 明天 / 星期五 / 2026-10-05 10:00")
    pa.add_argument("--date", default="", help="存储到指定日期文件 YYYY-MM-DD，默认今天")
    pa.add_argument("--from-type", default="agent", help="来源类型，如 agent/link/web（默认 agent）")
    pa.add_argument("--from-url", default=None, help="来源 URL（如 AI 对话链接），留空则不记录")
    pa.add_argument("--force", action="store_true", help="跳过重复检测，强制新建")
    pa.add_argument("--update-id", default="", help="确认后更新指定已有任务，不新建")

    pc = sub.add_parser("check", help="检查文本是否与已有任务相同/近似（只读）")
    pc.add_argument("--text", required=True)
    pc.add_argument("--exclude-id", default="", help="比对时排除的任务 ID")

    pd = sub.add_parser("done", help="完成任务（全部完成则自动归档该文件；前置依赖未结束需确认）")
    pd.add_argument("id")
    pd.add_argument("--force", action="store_true", help="忽略前置依赖未完成的硬检查，强制完成")
    ps = sub.add_parser("start", help="标记进行中（前置依赖未结束需确认）")
    ps.add_argument("id")
    ps.add_argument("--force", action="store_true", help="忽略前置依赖未完成的硬检查，强制进行")
    pw = sub.add_parser("work", aliases=["continue"], help="继续待办：标记进行中并用 Cursor 打开工作区（前置依赖未结束需确认）")
    pw.add_argument("id")
    pw.add_argument("--force", action="store_true", help="忽略前置依赖未完成的硬检查，强制进行")
    pr = sub.add_parser("reopen", help="重新打开")
    pr.add_argument("id")
    pdep = sub.add_parser("dep", help="设置 / 查看任务依赖（含存在性与环检测）")
    pdep.add_argument("id")
    pdep.add_argument("deps", nargs="*", help="设置（替换）依赖任务ID列表")
    pdep.add_argument("--add", nargs="+", default=[], help="追加依赖任务ID")
    pdep.add_argument("--remove", nargs="+", default=[], help="移除依赖任务ID")
    pdep.add_argument("--clear", action="store_true", help="清空依赖")
    pch = sub.add_parser("children", aliases=["sub"], help="列出某主任务下的子任务")
    pch.add_argument("id")
    pdp = sub.add_parser("deparent", help="解除子任务与父任务的归属关系")
    pdp.add_argument("id")
    par = sub.add_parser("archive", help="强制归档某个日期文件（须全部完成）")
    par.add_argument("date")
    pl = sub.add_parser("list", help="列出任务")
    pl.add_argument("--date", default="")
    pl.add_argument("--state", choices=["维护", "进行中", "结束", "待开始", "其他", "urgent", "all"], default="all")
    psh = sub.add_parser("show", help="查看任务或某天文件")
    psh.add_argument("id")
    pi = sub.add_parser("index", help="查看/重建索引")
    pi.add_argument("--rebuild", action="store_true")

    pinit = sub.add_parser("init", help="识别工作环境并保存到 env.json（含路径风格，统一工作空间）")
    penv = sub.add_parser("env", help="查看 / 修改已保存的环境配置")
    penv.add_argument("--set", action="append", default=[], help="KEY=VALUE，可多次；当前支持 path_style=windows|posix|mixed")
    penv.add_argument("--reset", action="store_true", help="重新探测环境并覆盖保存")
    penv.add_argument("--reformat-workspaces", action="store_true",
                      help="将全部存储文件中的工作空间统一为当前 path_style")

    args = p.parse_args()
    if not args.cmd:
        p.print_help()
        return 0
    handlers = {
        "add": lambda a: cmd_add(a),
        "check": lambda a: cmd_check(a),
        "done": lambda a: cmd_done(a),
        "start": lambda a: cmd_start(a),
        "work": lambda a: cmd_work(a),
        "reopen": lambda a: cmd_reopen(a),
        "dep": lambda a: cmd_dep(a),
        "children": lambda a: cmd_children(a),
        "sub": lambda a: cmd_children(a),
        "deparent": lambda a: cmd_deparent(a),
        "archive": lambda a: cmd_archive(a),
        "list": lambda a: cmd_list(a),
        "show": lambda a: cmd_show(a),
        "index": lambda a: cmd_index(a),
        "init": lambda a: cmd_init(a),
        "env": lambda a: cmd_env(a),
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
