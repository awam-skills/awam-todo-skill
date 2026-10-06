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

    # 先埋一条带标签的旧任务，供「历史标签复用」验证
    print("== 8. 标签推测：只给建议，不写入 ==")
    import json as _json
    rc, out, err = run(tmp, "add", "--text", "给甲方做方案PPT", "--tags", "客户对接", "--force")
    check("显式传 tags 时不推测", rc == 0 and "建议标签" not in out, out[:300])
    check("显式 tags 正常写入", "标签: 客户对接" in out, out[:300])

    rc, out, err = run(tmp, "suggest-tags", "--text", "给甲方做方案PPT 第二版")
    hist = {}
    try:
        hist = _json.loads(out)
    except Exception:
        pass
    check("suggest-tags 输出可读 JSON", rc == 0 and hist.get("written") is False, (out or err)[:300])
    check("命中历史标签", "客户对接" in (hist.get("suggestions") or []), str(hist.get("suggestions")))
    check("理由标明来自历史", hist.get("reason", {}).get("history") is True, str(hist.get("reason")))

    rc, out, err = run(tmp, "suggest-tags", "--text", "修复登录接口的 bug", "--note", "补单元测试")
    kw = _json.loads(out) if rc == 0 else {}
    check("命中关键词规则", "开发" in (kw.get("suggestions") or []), str(kw.get("suggestions")))

    rc, out, err = run(tmp, "suggest-tags", "--text", "整理项目笔记", "--workspace", "D:\\proj\\my-docs")
    wsh = _json.loads(out) if rc == 0 else {}
    check("工作空间领域词参与推测", "文档" in (wsh.get("suggestions") or []), str(wsh.get("suggestions")))
    check("建议给出依据", any("工作空间含领域词" in str(e) for e in (wsh.get("reason", {}).get("evidence") or [])),
          str(wsh.get("reason")))

    rc, out, err = run(tmp, "suggest-tags", "--text", "换个新的台灯")
    none_hit = _json.loads(out) if rc == 0 else {}
    check("无把握时返回空建议", rc == 0 and not none_hit.get("suggestions"), str(none_hit.get("suggestions")))

    rc, out, err = run(tmp, "add", "--text", "复习英语课程并背单词", "--force")
    check("新建未传 tags 时给出建议", rc == 0 and "建议标签:" in out, out[:400])
    check("建议标签未写入存储", "标签:" not in _task_block(tmp, "复习英语课程并背单词"),
          _task_block(tmp, "复习英语课程并背单词")[:200])

    print("== 9. 迁移（旧文件 -> v2）==")
    rc, out, err = run(tmp, "migrate")
    print(out or err)

    # ============ QA 独立验证：标签推测的「只建议、不写入」红线 ============
    print("== 10. 红线：建议标签只出现在 stdout，绝不落盘 ==")
    rc, out, err = run(tmp, "add", "--text", "修复登录接口的 bug 并补单测", "--force")
    check("有信号时 add 打印建议标签", rc == 0 and "建议标签:" in out, (out or err)[:400])
    sug_lines = [l for l in out.splitlines() if "建议标签:" in l]
    sug_body = sug_lines[0].split("建议标签:", 1)[1].split("（")[0].strip() if sug_lines else ""
    check("建议标签内容非空", bool(sug_body), str(sug_lines))
    check("依据一并打印", "依据:" in out, (out or err)[:400])
    blk = _task_block(tmp, "修复登录接口的 bug 并补单测")
    check("（防假阳性）存储里确实找到了这条任务", bool(blk), blk[:120])
    check("红线：落盘 md 块里没有 标签: 字段", bool(blk) and "标签:" not in blk, blk[:300])

    print("== 11. --update-id / --force / 显式 --tags 三条分支 ==")
    tid = _task_id_of(tmp, "修复登录接口的 bug 并补单测")
    check("能取到刚建任务的 ID", bool(tid), tid)
    rc, out, err = run(tmp, "add", "--update-id", tid, "--text", "背单词并复习英语课件")
    check("--update-id 分支不推测标签", rc == 0 and "建议标签" not in out, (out or err)[:400])
    rc, out2, _ = run(tmp, "suggest-tags", "--text", "背单词并复习英语课件")
    try:
        anti = _json.loads(out2)
    except Exception:
        anti = {}
    check("（反证）同一段文本单独推测是有建议的", "学习" in (anti.get("suggestions") or []),
          str(anti.get("suggestions")))

    rc, out, err = run(tmp, "add", "--text", "整理周报并汇报进度", "--force")
    check("--force 正常新建", rc == 0 and "已强制新建" in out, (out or err)[:300])
    check("--force 未传 tags 时同样给出建议", "建议标签:" in out, (out or err)[:300])

    # 刻意让显式传入的标签「盖不住」推测结果（学习能被盖住，英语盖不住），
    # 这样一旦推测分支被误触发，这条就会失败——否则这是条永远为真的空测试。
    rc, out, err = run(tmp, "add", "--text", "背单词打卡", "--tags", "学习;临时", "--force")
    check("显式 --tags 时不推测", rc == 0 and "建议标签" not in out, (out or err)[:300])
    check("显式 --tags 写入输出", "标签: 学习; 临时" in out, (out or err)[:300])
    blk2 = _task_block(tmp, "背单词打卡")
    check("显式 --tags 落盘到 md", bool(blk2) and "标签: 学习; 临时" in blk2, blk2[:300])
    rc, anti_out, _ = run(tmp, "suggest-tags", "--text", "背单词打卡")
    try:
        anti2 = _json.loads(anti_out)
    except Exception:
        anti2 = {}
    check("（反证）不传 tags 时这段文本确实有可推测的标签",
          "英语" in (anti2.get("suggestions") or []), str(anti2.get("suggestions")))

    print("== 12. 无信号时不硬凑 ==")
    rc, out, err = run(tmp, "add", "--text", "紫色窗帘", "--force")
    check("无信号时 add 不给建议", rc == 0 and "建议标签" not in out, (out or err)[:300])
    rc, out, err = run(tmp, "suggest-tags", "--text", "紫色窗帘")
    try:
        none_hit2 = _json.loads(out)
    except Exception:
        none_hit2 = {}
    check("无信号时 suggest-tags 返回空列表", rc == 0 and none_hit2.get("suggestions") == [],
          str(none_hit2.get("suggestions")))
    check("无信号时 written 仍为 false", none_hit2.get("written") is False)

    print("== 13. suggest-tags 的 JSON 契约 ==")
    rc, out, err = run(tmp, "suggest-tags", "--text", "整理项目笔记",
                       "--note", "复盘本周", "--workspace", "D:\\proj\\my-docs")
    try:
        js = _json.loads(out)
    except Exception as e:
        js = {}
        check("suggest-tags 输出可被 json.loads", False, str(e) + " | " + out[:200])
    check("suggest-tags 输出可被 json.loads", bool(js), out[:200])
    check("written 恒为 false", js.get("written") is False, str(js.get("written")))
    dets = js.get("details") or []
    check("details 非空", len(dets) > 0, str(js.get("suggestions")))
    check("details 每项都有 source 字段", bool(dets) and all("source" in d for d in dets), str(dets))
    check("details 每项都有 tag/score/evidence",
          bool(dets) and all(all(k in d for k in ("tag", "score", "evidence")) for d in dets), str(dets))
    check("工作空间来源进了 source", any("workspace" in str(d.get("source")) for d in dets), str(dets))
    check("reason 含 history/keyword/evidence",
          all(k in (js.get("reason") or {}) for k in ("history", "keyword", "evidence")), str(js.get("reason")))

    print("== 14. 英文词边界（debug 不能当 bug）==")
    for bad in ("debug the login flow", "debugging session", "bugfix 收尾"):
        rc, out, err = run(tmp, "suggest-tags", "--text", bad)
        try:
            j = _json.loads(out)
        except Exception:
            j = {}
        check("「%s」不误命中开发类规则" % bad, "开发" not in (j.get("suggestions") or []),
              str(j.get("suggestions")))
    rc, out, err = run(tmp, "suggest-tags", "--text", "fix a bug in login")
    try:
        j2 = _json.loads(out)
    except Exception:
        j2 = {}
    check("真正的 bug 仍能命中开发", "开发" in (j2.get("suggestions") or []), str(j2.get("suggestions")))

    # ============ QA 回归：原有命令行为不变 ============
    print("== 15. 回归：原有命令 ==")
    rc, out, err = run(tmp, "list")
    check("list 正常", rc == 0 and bool(out.strip()), (out or err)[:200])
    # check 发现重复时按「需确认」退出码返回（rc=3），属于既有约定，不能当成失败
    rc, out, err = run(tmp, "check", "--text", "背单词打卡")
    check("check 能识别重复", rc == 3 and "相同" in out, "rc=%s | %s" % (rc, (out or err)[:200]))

    tid2 = _task_id_of(tmp, "整理周报并汇报进度")
    rc, out, err = run(tmp, "start", tid2)
    check("start 正常", rc == 0, (out or err)[:200])
    rc, out, err = run(tmp, "done", tid2)
    check("done 正常", rc == 0, (out or err)[:200])
    rc, out, err = run(tmp, "reopen", tid2)
    check("reopen 正常", rc == 0, (out or err)[:200])

    tid3 = _task_id_of(tmp, "紫色窗帘")
    rc, out, err = run(tmp, "dep", tid3, tid2)
    check("dep 设置依赖正常", rc == 0, (out or err)[:200])
    rc, out, err = run(tmp, "children", tid2)
    check("children 正常", rc == 0, (out or err)[:200])
    rc, out, err = run(tmp, "deparent", tid3)
    check("deparent 正常", rc == 0, (out or err)[:200])

    rc, out, err = run(tmp, "archive-month", "--dry-run")
    check("archive-month 预览正常", rc == 0, (out or err)[:200])
    rc, out, err = run(tmp, "migrate", "--apply")
    check("migrate --apply 正常", rc == 0, (out or err)[:300])
    check("迁移后日文件全部为 schema 2", _all_schema2(tmp), _schema_report(tmp))

    with open(os.path.join(storage, "2026-08-08.md"), "w", encoding="utf-8") as f:
        f.write("# 待办 2026-08-08\n归档: false\nschema: 2\n---\n\n"
                "## T-20260808-001\n状态: [x] 结束\n重要: 不重要\n紧急: 不紧急\n内容: 八月归档测试\n"
                "创建: 2026-08-08 10:00\n")
    # 注：整日文件全部完成后会被折进月度归档（既有行为，非本次改动引入），
    # 所以这里认「就地标记 归档: true」或「内容已并入 storage/archive/YYYY-MM.md」两种落点。
    rc, out, err = run(tmp, "archive", "2026-08-08")
    check("archive 强制归档正常", rc == 0, (out or err)[:200])
    day_file = _read_file(os.path.join(storage, "2026-08-08.md"))
    month_file = _read_file(os.path.join(storage, "archive", "2026-08.md"))
    check("归档后当日文件被标记或折入月度归档",
          "归档: true" in day_file or "八月归档测试" in month_file,
          "day=%r month=%r" % (day_file[:120], month_file[:120]))

    rc, out, err = run(tmp, "index", "--rebuild")
    check("index --rebuild 正常", rc == 0 and '"schema_version"' in out, (out or err)[:200])
    rc, out, err = run(tmp, "env")
    check("env 查看正常", rc == 0, (out or err)[:200])
    rc, out, err = run(tmp, "show", tid2)
    check("show 任务正常", rc == 0 and bool(out.strip()), (out or err)[:200])

    shutil.rmtree(tmp, ignore_errors=True)
    print("\n结果：" + ("全部通过" if ok else "存在失败项"))
    return 0 if ok else 1


def _storage_text(tmp):
    """把临时目录里的日文件拼成一整块文本（不含 archive/ 子目录）。"""
    parts = []
    p = os.path.join(tmp, "storage")
    for fn in sorted(os.listdir(p)):
        fp = os.path.join(p, fn)
        if fn.endswith(".md") and os.path.isfile(fp):
            with open(fp, encoding="utf-8") as f:
                parts.append(f.read())
    return "\n".join(parts)


def _task_block(tmp, text):
    """取出某条任务在存储文件里的原始块，用于验证「没有被偷偷写入字段」。"""
    for blk in _storage_text(tmp).split("\n## "):
        if ("内容: " + text) in blk:
            return blk
    return ""


def _task_id_of(tmp, text):
    """按任务内容定位其 ID（用于 --update-id / done / dep 等后续操作）。"""
    import re
    p = os.path.join(tmp, "storage")
    for fn in sorted(os.listdir(p)):
        if not fn.endswith(".md"):
            continue
        with open(os.path.join(p, fn), encoding="utf-8") as f:
            for blk in f.read().split("\n## "):
                if ("内容: " + text) in blk:
                    m = re.search(r"^(T-\d{8}-\d{3})", blk.strip())
                    if m:
                        return m.group(1)
    return ""


def _read_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _all_schema2(tmp):
    """日文件（不含 archive/）必须都是 schema 2，验证 migrate 没被改坏。"""
    p = os.path.join(tmp, "storage")
    for fn in sorted(os.listdir(p)):
        if fn.endswith(".md") and os.path.isfile(os.path.join(p, fn)):
            if "schema: 2" not in _read_file(os.path.join(p, fn)):
                return False
    return True


def _schema_report(tmp):
    p = os.path.join(tmp, "storage")
    return "; ".join(fn + "=" + ("v2" if "schema: 2" in _read_file(os.path.join(p, fn)) else "非v2")
                     for fn in sorted(os.listdir(p)) if fn.endswith(".md"))


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
