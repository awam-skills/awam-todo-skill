# eval/ —— 意图路由评测资产（awam-skill-eval 生成）

本目录由 `awam-skill-eval` 评测流程生成，用于本技能的**触发边界**回归评测。

- `trigger-evals.json`：可重跑的查询集。正例=本技能 description 的触发场景；负例=跨技能混淆矩阵（相邻技能的场景，"看起来像但不该触发"）。
- `results/`：逐 query 的 yes/no 判定明细与 P/R/F1 汇总。

复用（只读、零副作用，不执行技能真实命令）：
```
python <skills-root>/awam-skill-eval/scripts/eval_trigger.py   --skill-dir <本目录的父目录> --queries eval/trigger-evals.json --local --votes 5
```
边界 case 建议 `--votes 5` 多数投票（temperature=0 仍有服务端波动）。

详见项目级报告：`技能仓库路由评测与优化报告.md`。
