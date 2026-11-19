# CLI contract (reference)

Extracted from `SKILL.md` for quick lookup. `SKILL.md` remains authoritative.

## Invocation

```
python <skill-dir>/scripts/todo.py <command> [args]
```

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 2 | Invalid usage / unsupported action / no target |
| 3 | Guard hit — unfinished prerequisites or open subtasks, or duplicate needs confirmation. The agent must ask the user; `--force` overrides. |

## Output language

CLI/API output is fixed **English** regardless of the user's language; the AI
conversation follows the user's language; the web UI is i18n (English default).

## `triage` (pure-rule entry, read-only)

- Input: `triage --text "..."`
- Output: JSON with the decided action (`add` / `update` / `status` / `delete`
  / `unknown`) plus parsed fields (`text`, `importance`, `urgency`, `due`,
  `target_id`, `need_confirm`, ...).
- Exit `3` when confirmation is required (unsure / destructive delete).
- No LLM involved — classification is deterministic rules in `todo.py`.

## Language / no-invention

Derived metrics (e.g. projected finish date) render `N/A` when data is
insufficient — the tool never fabricates numbers.
