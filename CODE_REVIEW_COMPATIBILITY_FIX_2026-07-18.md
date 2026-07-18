# Candidate Review 兼容层恢复结果

日期：2026-07-18
范围：`app.ui.candidate_review` 历史公开接口兼容性
状态：已完成，完整测试通过

## 1. 修改结果

本次修改保留新的 Workbench 作为默认界面，同时完整恢复旧版
`app.ui.candidate_review` 模块的公开函数接口。

恢复内容包括：

- Review：候选加载、选择、翻页、评分、收藏、撤销和自动前进。
- Profile：状态查询、构建、发布、刷新和向量回填。
- Config：配置字段组件、读取、保存和 LLM 连接测试。
- Control：旧版 `start_batch()`、`stop_batch()`、`get_batch_status()` 调用接口。
- Gradio：旧版 Review、Control、Profile 三页构建入口。
- 历史辅助函数，包括路径允许列表、进程命令识别和显示格式化函数。

## 2. 实现方式

- `app/ui/candidate_review.py` 保持为少于 300 行的轻量入口。
- 新增 `app/ui/legacy_candidate_review.py`，集中承载历史接口适配。
- 模块级 `__getattr__` 将未在轻量入口直接声明的历史名称转发到兼容模块。
- 旧 Control 函数在默认模式下转换为 Task API 操作，不重新引入 PID 队列管理。
- `rate_and_advance()` 支持依赖注入，使旧模块 Monkeypatch 和测试行为继续有效。
- 新增 `build_legacy_candidate_review()`，旧调用方仍可构建 Review、Control、Profile 三页布局。

## 3. 测试驱动过程

新增契约测试：

```text
tests/test_candidate_review_legacy_api.py
```

修复前，测试确认 27 个历史接口缺失并失败。修复后结果：

```text
兼容层及 UI 定向测试：42 passed
Python compileall：PASS
git diff --check：PASS
完整测试：787 passed, 2 skipped, 4 warnings
```

之前完整测试中的 14 个 Candidate Review 失败已全部消除。

## 4. 修改文件

- `app/ui/candidate_review.py`
- `app/ui/legacy_candidate_review.py`
- `app/ui/tabs/review.py`
- `tests/test_candidate_review_legacy_api.py`

## 5. 尚未解决的上一轮审查问题

本次范围只处理完整兼容层。以下生产阻断问题仍需另行修改：

1. Launcher 在主线程创建 SQLite 连接后交给 Worker 线程，Worker 会立即退出。
2. 每一个 Task Stage 仍执行完整视频流水线。
3. 成功或失败的视频聚合后，Job 可能永久停留在 `running`。
4. 90 秒 Lease 没有在长时间 Stage 执行期间自动续租。
5. Task 配置快照结构和 Quality Lab 的配置、单视频隔离仍未正确传递。

因此，兼容层和测试门禁已经恢复，但在以上任务引擎问题修复前，仍不建议构建并发布新的生产 EXE。
