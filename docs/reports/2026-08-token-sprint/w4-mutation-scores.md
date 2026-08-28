# W4 突变测试得分报告（token sprint workstream W4）

- 工具路径：canonical manifest `cosmic_ray.status = "included"` → cosmic-ray 8.7.0（T-1 冻结），经 `scripts/token_sprint/mutation_harness.py` 调度；未发生 `uv add`，依赖未改动。
- 突变只在系统临时目录下的沙箱副本（`app/` + `scripts/` + `configs/` + `tests/` 树拷贝 + `mutation_boot.py` 引导插件）运行；worktree 产品树零改动（`git status --porcelain --untracked-files=all -- app/` 为空）。
- 导入证明：每次 pytest 子进程带 `-p mutation_boot`，插件断言 `app.__file__` 落在沙箱内并写证据文件；sentinel（`return 1`→`return 0`）端到端证明 pytest 导入的是副本（`tests/mutation/test_harness_sentinel.py`）。
- 引擎拒绝门：manifest `fallback` + `--engine cosmic-ray` → exit 2（测试锁定）。
- 计分：score = killed / mutants_total；timeout 按计划记 survived 并登记 W7。
- 得分 JSON 原始产物：`<sprint-root>/w4/mutation_scores/cosmic-ray/<module>.json`（共享根 `data/token_sprint/`；每个模块的 JSON 为该模块最新一次（after）运行）。
- 等效/不可杀突变逐条登记：`docs/reports/2026-08-token-sprint/w4-surviving.md`。
- 杀伤测试全部位于 `tests/mutation/test_mut_<module>.py`，且全部在**原始**模块下通过（未修任何产品代码）。

## 得分前后对比（before = 仅既有测试；after = 既有测试 + tests/mutation 杀伤测试）

| Module | mutants | before killed | before score | after killed | after score | 既有测试文件 |
|--------|---------|---------------|--------------|--------------|-------------|--------------|
| clip_merge | 181 | 110 | 0.608 | 139 | 0.768 | tests/test_clip_merge.py |
| export_ranking | 335 | 202 | 0.603 | 261 | 0.779 | tests/test_export_ranking.py |
| grid_select | 172 | 109 | 0.634 | 147 | 0.855 | tests/test_grid_select.py |
| timeline | 107 | 39 | 0.364 | 80 | 0.748 | tests/test_timeline.py |
| taste_map | 83 | 63 | 0.759 | 70 | 0.843 | tests/test_creative_curation.py |
| narrative_curation | 184 | 107 | 0.582 | 174 | 0.946 | tests/test_creative_curation.py |
| collections | 253 | 141 | 0.557 | 222 | 0.877 | tests/test_collections.py |
| desktop_export_sync | 703 | — | （首轮进行中） | — | — | tests/test_desktop_export_sync.py |
| metrics | 302 | 286 | 0.947 | 295 | 0.977 | tests/quality_lab/test_metrics.py |
| calibration | 290 | 236 | 0.814 | 274 | 0.945 | tests/quality_lab/test_calibration.py |
| promotion | 161 | 113 | 0.702 | 129 | 0.801 | tests/quality_lab/test_promotion.py |
| repository | 358 | 120 | 0.335 | 178 | 0.497 | tests/task_engine/test_repository.py |
| fingerprints | 59 | 12 | 0.203 | 34 | 0.576 | tests/test_adaptive_config.py |
| vector_math | 361 | 215 | 0.596 | 260 | 0.720 | （无既有测试，杀伤测试即基线） |

- 合计（13 个已完成 after 运行的模块，不含 desktop_export_sync）：mutants 2,845，before killed 1,872（0.658），after killed 2,362（0.830）。
- after 存活突变全部按模块登记于 w4-surviving.md：主要为类型注解 union 突变（`from __future__ import annotations` 下不可执行）、keyword-only 标记、不可达守卫分支、以及「区分性输入必先触发原实现既有缺陷」的实践不可杀项（均已转为 W7 线索）。

## 方法与证据链

1. Sentinel：`--dry-run-sentinel` 报告 `sentinel_killed=true`，`imported_from.json` 记录 `sentinel_mod`/`app`/`app.services.clip_merge` 的 `__file__` 全部位于临时沙箱目录；每次突变运行另有 `mutation_boot` 插件写 `imported_from_boot.txt`。
2. 每模块运行：`cosmic-ray init` → `cosmic-ray baseline`（未突变必须全绿，否则中止该模块）→ `cosmic-ray exec`（local distributor，cwd=沙箱）→ `cosmic-ray dump` 解析逐突变结果 → 原子写得分 JSON。
3. 杀伤测试验收：`uv run pytest -q tests/mutation/`（含全部原始模块测试）全绿，且 harness 复跑确认目标存活点转为 killed。
