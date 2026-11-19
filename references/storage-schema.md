# Storage & schema (reference)

Extracted from `SKILL.md`. `SKILL.md` remains authoritative.

## Files

- `storage/YYYY-MM-DD.md` — one file per day, todos as `## T-YYYYMMDD-NNN` sections.
- `storage/archive/YYYY-MM.md` — monthly archives of fully completed months.
- `index.json` — auto-rebuilt after every mutation (todo / in_progress / urgent /
  done / archived; weekly stats start Monday).
- `env.json` — user config (`storage_dir`, `editor.path`, `ui.port`, `capture.*`, ...).

## Day-file header (schema 2)

```
# 待办 2026-11-19
归档: false
schema: 2
---

## T-20261119-001
状态: [ ] 待开始
重要: 重要
紧急: 紧急
内容: 任务原文
创建: 2026-11-19 10:00
```

- `migrate` previews old-schema → schema 2; `migrate --apply` writes.
- `archive-month` moves only months where every day file is fully complete;
  `--dry-run` previews.

## ID format

`T-YYYYMMDD-NNN` — globally allocated by a per-request counter, stable across
a multi-step workflow so `<ref>` markers map 1:1 to the final source list.
