# awam-todo

Personal todo management skill. Parses natural-language todos from the session,
stores them in date-partitioned Markdown files, and auto-maintains an index.
Pure Python standard library (no third-party dependencies). Works with WorkBuddy,
Cursor, Doubao and other agents via the `SKILL.md` instructions.

See [SKILL.md](SKILL.md) for the full behavioral specification (parsing rules,
duplicate detection, dependency/subtask guards, contingency plans, overdue exits,
schema migration, monthly archiving, language policy, capture service).

## When to use

Anything resembling a todo: "记一下/加个待办/提醒/安排/完成/删除/改期/标记状态",
or a sentence that looks like a task. The developer routes it to the pure-rule
triage entry; the agent never guesses the classification or rewrites the raw text.

## Quick start

```powershell
# Command form (Windows; substitute the real install directory)
python "<skill-directory>\scripts\todo.py" add --text "背完 2000 个单词" --due "下周一"
python "<skill-directory>\scripts\todo.py" list
python "<skill-directory>\scripts\todo.py" index
```

The canonical invocation is `python <skill-dir>/scripts/todo.py <command> <args>`.
Run `python ...\todo.py --help` for the full command list, or
`python ...\todo.py <command> --help` for a specific command.

### Common commands

| Command | Purpose |
|---------|---------|
| `init` / `env` | Detect & save environment / view or change config |
| `add` | Add a todo (identical → update by default; `--force` skips checks) |
| `check` / `suggest-tags` | Read-only duplicate check / tag suggestion (JSON) |
| `list` / `show` / `index` | List todos / view an item / view the aggregate index |
| `done` / `start` / `reopen` / `work` | State transitions (dependency guards apply) |
| `postpone` | Reschedule or clear a deadline (overdue exit) |
| `dep` / `children` / `deparent` | Dependencies and subtasks |
| `archive` / `archive-month` / `migrate` | Day archive / monthly archive / schema migration |
| `triage` | One sentence → add/update/status/delete (read-only, JSON; exit 3 = needs confirmation) |
| `delete` | Delete a todo (cleans dependency/parent references) |

## Agent prompts

Drop-in prompt files are in `docs/`:

- `agent-prompts.md` — file index + short dispatch prompt
- `prompt-cursor.md` — full prompt for Cursor
- `prompt-doubao.md` — full prompt for Doubao
- `OPTIMIZATION.md` / `优化建议.md` — recorded (unimplemented) improvement ideas

## Optional web UI + capture service

`web/server.py` serves an i18n web UI and hosts the capture service inside the
same process. The capture service uses a global hotkey (default `Ctrl+Alt+T`) to
read the clipboard as raw task text and feed the triage entry; configure it in the
UI Settings or via `env --set capture.*` (stored in `env.json`). `capture.py` is
imported by the server — it is **not** a standalone CLI.

## Self-test / verification

```powershell
python "<skill-directory>\scripts\selftest.py"           # full scenario suite (~25s, temp dir only)
python "<skill-directory>\scripts\selftest.py" --help     # usage
```

The self-test runs entirely in a temporary directory; it never touches real
`storage/`. By default a passing run cleans up and a failing run keeps the temp
dir for inspection; override with `AWAM_TODO_KEEP_TMP=1|0`.

## Static quality check

This skill ships a standard skill layout (README.md, `references/`, `assets/`,
`expected_outputs/`, `scripts/`) so it can be validated by the `skill-tester`
meta-skill's `skill_validator.py` / `script_tester.py` / `quality_scorer.py` /
`security_scorer.py`.

## Layout

```
SKILL.md            Full behavioral spec (the file agents load)
SKILL.zh-CN.md      Chinese variant
README.md           This file
scripts/todo.py     Main CLI (add/list/done/triage/... )
scripts/capture.py  Capture service module (imported by web/server.py)
scripts/selftest.py Scenario self-test
web/                Optional local web UI (server.py, index.html)
docs/               Agent prompts + recorded optimization ideas
references/         Reference notes (see references/README.md)
assets/             Sample data for tests (see assets/README.md)
expected_outputs/   Golden outputs for regression (see expected_outputs/README.md)
storage/            Date-partitioned todo data (schema-versioned)
env.json / index.json  Config / auto-maintained index
```
