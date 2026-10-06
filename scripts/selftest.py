#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""awam-todo 改造后的场景自测：在临时目录里跑，不碰真实 storage。"""
import os
import shutil
import subprocess
import sys
import tempfile

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
PY = sys.executable


def run(tmp, *args):
    r = subprocess.run([PY, os.path.join(tmp, "scripts", "todo.py")] + list(args),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def main():
    tmp = tempfile.mkdtemp(prefix="awamtodo-")
    shutil.copytree(SRC, os.path.join(tmp, "scripts"))
    os.makedirs(os.path.join(tmp, "storage"), exist_ok=True)
    ok = True

    def check(name, cond, extra=""):
        nonlocal ok
        print(("  PASS  " if cond else "  FAIL  ") + name + (("  | " + extra) if extra and not cond else ""))
        if not cond:
            ok = False

    print("== 1. 新建带预案 + 过期截止 ==")
    rc, out, err = run(tmp, "add", "--text", "背完 2000 个单词", "--blocker", "晚上刷手机",
                       "--counter", "拿起手机前先背 20 个", "--due", "2026-09-20")
    print(out or err)
    check("add 成功", rc == 0)
    check("输出含障碍", "障碍: 晚上刷手机" in out)
    check("输出含对策", "对策: 拿起手机前先背 20 个" in out)
    check("输出含置顶理由(逾期)", "置顶: 逾期" in out)
    check("输出含两个出口", "立即推进" in out and "调整计划" in out)
    check("预计完成日不编造", "暂无推算" in out)

    print("== 2. 无截止任务：不得编造逾期/推算 ==")
    rc, out, err = run(tmp, "add", "--text", "整理归档策略", "--force")
    check("无截止不产出逾期天数", "逾期" not in out)

    print("== 3. 时间解析失败要报错，不静默丢弃 ==")
    rc, out, err = run(tmp, "postpone", "T-", "乱七八糟的时间")
    rc2, out2, err2 = run(tmp, "add", "--text", "带坏时间的任务", "--due", "下个世纪的某天", "--force")
    check("坏时间有告警", "未识别时间" in (err + err2), (err + err2)[:200])

    print("== 4. 调整计划 postpone ==")
    rc, out, err = run(tmp, "postpone", "T-20260920-001" if False else _first_id(tmp), "下周一")
    print(out or err)
    check("postpone 成功", rc == 0 and "已调整计划" in out)

    print("== 5. 月度归档：上月全部完成的文件 ==")
    storage = os.path.join(tmp, "storage")
    with open(os.path.join(storage, "2026-09-01.md"), "w", encoding="utf-8") as f:
        f.write("# 待办 2026-09-01\n归档: false\nschema: 1\n---\n\n"
                "## T-20260901-001\n状态: [x] 结束\n重要: 不重要\n紧急: 不紧急\n内容: 九月的旧任务\n"
                "创建: 2026-09-01 10:00\n")
    with open(os.path.join(storage, "2026-09-02.md"), "w", encoding="utf-8") as f:
        f.write("# 待办 2026-09-02\n归档: false\nschema: 1\n---\n\n"
                "## T-20260902-001\n状态: [~] 进行中\n重要: 不重要\n紧急: 不紧急\n内容: 九月未完成的\n"
                "创建: 2026-09-02 10:00\n")
    rc, out, err = run(tmp, "archive-month", "--dry-run")
    print(out or err)
    check("只归档已全部完成的月份文件", "2026-09-01" in out and "2026-09-02" not in out)
    rc, out, err = run(tmp, "archive-month")
    print(out or err)
    check("归档执行成功", rc == 0)
    check("原日文件已移走", not os.path.exists(os.path.join(storage, "2026-09-01.md")))
    check("未完成的文件保留", os.path.exists(os.path.join(storage, "2026-09-02.md")))
    arch = os.path.join(storage, "archive", "2026-09.md")
    check("归档文件已生成", os.path.exists(arch))
    if os.path.exists(arch):
        with open(arch, encoding="utf-8") as f:
            body = f.read()
        check("归档文件带 schema 头", "schema: 2" in body)
        check("归档文件记录原日期", "原日期: 2026-09-01" in body)
        check("归档文件记录来源", "来源文件: 2026-09-01" in body)
    rc, out, err = run(tmp, "show", "2026-09")
    check("show 月份可查看归档", rc == 0 and "九月的旧任务" in out)

    print("== 6. 索引：周口径 + 今日/逾期 ==")
    import json
    rc, out, err = run(tmp, "index", "--rebuild")
    idx = json.loads(out)
    check("index 带 schema_version", idx.get("schema_version") == 2)
    check("week 从周一起", idx["week"]["start"] == _monday())
    check("summary 含 today/overdue/monthly_archives",
          all(k in idx["summary"] for k in ("today", "overdue", "monthly_archives", "archived_tasks")))
    check("月度归档计数", idx["summary"]["monthly_archives"] == 1)

    print("== 7. 默认值可覆盖 ==")
    rc, out, err = run(tmp, "env", "--set", "defaults.status=待开始")
    check("env --set defaults.status", rc == 0 and "待开始" in out)
    rc, out, err = run(tmp, "add", "--text", "默认状态测试", "--force")
    check("新任务用覆盖后的默认值", "状态: 待开始" in out, out[:300])

    print("== 8. 迁移（旧文件 -> v2）==")
    rc, out, err = run(tmp, "migrate")
    print(out or err)

    shutil.rmtree(tmp, ignore_errors=True)
    print("\n结果：" + ("全部通过" if ok else "存在失败项"))
    return 0 if ok else 1


def _first_id(tmp):
    import re
    p = os.path.join(tmp, "storage")
    for fn in sorted(os.listdir(p)):
        if not fn.endswith(".md"):
            continue
        with open(os.path.join(p, fn), encoding="utf-8") as f:
            m = re.search(r"^## (T-\d{8}-\d{3})", f.read(), re.M)
        if m:
            return m.group(1)
    return "T-00000000-000"


def _monday():
    import datetime as dt
    d = dt.date.today()
    return (d - dt.timedelta(days=d.isoweekday() - 1)).strftime("%Y-%m-%d")


if __name__ == "__main__":
    sys.exit(main())
