# W4 突变测试得分报告（token sprint workstream W4）

- 工具路径：canonical manifest `cosmic_ray.status = "included"` → cosmic-ray 8.7.0（T-1 冻结），经 `scripts/token_sprint/mutation_harness.py` 调度。
- 突变只在系统临时目录下的沙箱副本（`app/` + `tests/` 树拷贝 + `mutation_boot.py` 引导插件）运行；worktree 产品树零改动。
- 导入证明：每次 pytest 子进程带 `-p mutation_boot`，插件断言 `app.__file__` 落在沙箱内并写证据文件；sentinel（`return 1`→`return 0`）验证 pytest 导入的是副本（`tests/mutation/test_harness_sentinel.py`）。
- 引擎拒绝门：manifest `fallback` + `--engine cosmic-ray` → exit 2（测试锁定）。
- 计分：score = killed / mutants_total；timeout 按 plan 记 survived 并登记 W7。
- 得分 JSON 原始产物：`<sprint-root>/w4/mutation_scores/cosmic-ray/<module>.json`（共享根 `data/token_sprint/`）。
- 等效/不可杀突变逐条登记：`docs/reports/2026-08-token-sprint/w4-surviving.md`。

## 得分前后对比

| Module | Run | mutants | killed | survived | score | JSON |
|--------|-----|---------|--------|----------|-------|------|
| clip_merge | before（仅既有 tests/test_clip_merge.py） | 181 | 110 | 71 | 0.608 | w4/mutation_scores/cosmic-ray/clip_merge.json |
| clip_merge | after（+ tests/mutation/test_mut_clip_merge.py，18 个杀伤测试） | 181 | 139 | 42 | 0.768 | 同上（覆盖） |

- clip_merge 存活 42 条全部为等效/实践不可杀突变（见 w4-surviving.md），可杀突变已 100% 杀死。

（其余模块随 Task 4+ 增量追加。）
