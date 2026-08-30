# 预存测试失败诊断报告（pre-existing test failures）

- 日期：2026-08-29
- 基线：master @ a8273bed（未改动）
- 执行者：test-diagnosis executor（只读诊断，未改任何 app/、scripts/、tests/ 代码）
- 结论速览：

| # | 测试 | 判定 | 一句话根因 |
|---|------|------|-----------|
| 1 | `tests/test_preference_preflight.py::test_preference_memory_status_reports_safe_defaults` | **测试过时**（断言与生产配置脱钩 + 隐性全局配置状态依赖） | 断言 `preference_memory_enabled is True`，但 7/18 起 models.yaml 已回退为 `enabled: false` |
| 2 | `tests/test_preference_preflight.py::test_status_handles_missing_manifest` | **测试过时**（同上） | 同上 |
| 3 | `tests/test_preference_preflight.py::test_status_handles_malformed_manifest` | **测试过时**（同上） | 同上 |
| 4 | `tests/test_ollama_loopback_config.py::test_ollama_defaults_use_ipv4_loopback` | **真实（轻微）bug** | 8/29 新增的富化脚本默认 `http://localhost:11434`，违反全库 IPv4 loopback 约定 |

**W8 假设被证伪**：「collect_status 环境依赖，疑与生产库正被富化有关」不成立。这 3 个 preflight 测试全程只触碰 `tmp_path`，从不打开生产库、也从不检查生产库 WAL——失败与生产库被富化进程写入完全无关。真正与富化进程时间线吻合的是 #4（见下）。另外说明：任务简报称这 4 个失败「非冲刺引入」，对 #1–#3 成立（7/18 起即红）；对 #4 需修正——它由 8/29 18:43 的 b382c48a（富化脚本入库）引入，早于 W8 gate 记录（8/29 19:59, 9dd86c83），因此对 W8 基线而言是「预存」，但引入者是富化脚本本身。

---

## 复现命令（当前 master，全部确定性复现）

```bash
# 4 个失败一次复现（ollama_loopback 的准确文件名是 tests/test_ollama_loopback_config.py）
uv run pytest -q tests/test_preference_preflight.py tests/test_ollama_loopback_config.py --no-header
# → 4 failed, 1 passed（唯一通过的是 WAL 检测用例 test_wal_file_exists_detected）

# 单独复现 #1–#3（隔离运行同样失败，证明非测试顺序问题）
uv run pytest -q tests/test_preference_preflight.py --no-header

# 单独复现 #4
uv run pytest -q tests/test_ollama_loopback_config.py --no-header
```

原始输出（摘要，2026-08-29）：

```
FAILED tests/test_preference_preflight.py::test_preference_memory_status_reports_safe_defaults
FAILED tests/test_preference_preflight.py::test_status_handles_missing_manifest
FAILED tests/test_preference_preflight.py::test_status_handles_malformed_manifest
FAILED tests/test_ollama_loopback_config.py::test_ollama_defaults_use_ipv4_loopback
4 failed, 1 passed, 1 warning in 2.08s
```

关键断言差异原文（pytest 输出）：

```
# test_preference_memory_status_reports_safe_defaults
E       assert False is True
tests\test_preference_preflight.py:21: AssertionError

# test_ollama_defaults_use_ipv4_loopback
E       AssertionError: assert ['scripts\\enrich_library_candidates.py'] == []
E         Left contains one more item: 'scripts\\enrich_library_candidates.py'
```

---

## #1–#3 test_preference_preflight ×3

### 根因链（历史考古，全部有 commit 佐证）

1. **f201578f**（6/28，"freeze baseline with preference memory preflight check"）：新增 `collect_status()` 与测试，断言 `preference_memory_enabled is False`。config 无 `preference_memory` 段 → 绿。
2. **0a14e222**（6/28）：`collect_status()` 改为从 config 读取 `preference_memory.enabled`；在 `configs/models.yaml` 增加 `preference_memory.enabled: false`；测试保持断言 False → 绿。
3. **a347edae**（7/16，"config: enable preference memory defaults"）：models.yaml 改为 `enabled: true`，**同时把 3 个测试断言翻转为 `is True`**。此时测试只在「进程内 `app.config._config` 已被某个先跑的测试加载过 models.yaml」时才成立——这是被引入的隐性顺序依赖。
4. **9e39ac1e**（7/18，"feat: add reliable staged task pipeline"）：把 models.yaml 的 `preference_memory.enabled` **改回 `false`**（`git diff a347edae 9e39ac1e -- configs/models.yaml` 可见 `enabled: true → enabled: false`），**但没有回退测试断言**。自此 3 个测试在任意运行顺序下都红。

### 机理（当前代码行为）

`scripts/preference_memory.py:25-64` 的 `collect_status()`：

```python
from app.config import get as config_get          # 函数体内导入，总是取当前 app.config
preference_enabled = bool(config_get("preference_memory.enabled", False))
```

- `app/config.py` 的 `_config` 是模块级全局 dict，只有显式调用 `load_config()` 才填充；为空时 `get()` 返回默认值 `False`。
- 隔离跑：`_config == {}` → `False` → 断言 True 失败。
- 全量跑：`tests/test_tasks_api.py` 等经 `app.routers.tasks`（`from app.config import load_config`，tasks.py:13）触发 `load_config()` 加载 configs/models.yaml；当前 `enabled: false` → 同样 `False` → 失败。
- 三态复现实验（uv run python -c，2026-08-29 实测）：

```
isolated (_config empty): False None
config loaded (enabled=false today): False
config loaded (enabled=true): True        # 仅当回到 7/16–7/18 两天的配置态才绿
```

### 判定

- **不是 app bug**：`collect_status()` 忠实反映配置开关，行为正确。
- **不是生产库/富化环境依赖**：测试传 `tmp_path`，`collect_status` 在该路径下只做 `Path.exists()` 与 manifest JSON 解析，与生产库、WAL、富化进程零交集。W8 的猜测应予撤销。
- **是测试过时**：断言把「产品配置开关的值」当成不变量来测；7/18 产品有意关闭该开关后测试未同步。此外测试通过模块级可变全局（`app.config._config`）耦合运行顺序，即使断言侥幸正确也是脆弱设计。

### 修复提案（仅改 tests/test_preference_preflight.py，不动 app/、不动生产配置）

用 monkeypatch 显式钉住配置，使测试确定性且同时覆盖两个分支：

```python
import pytest

@pytest.fixture
def pref_enabled(monkeypatch):
    monkeypatch.setattr("app.config._config", {"preference_memory": {"enabled": True}})

@pytest.fixture
def pref_disabled(monkeypatch):
    monkeypatch.setattr("app.config._config", {"preference_memory": {"enabled": False}})
```

- 3 个现有用例改挂 `pref_enabled`（保留原断言 `is True`，语义变为「开关为 true 时 status 如实上报」）；
- 追加一个 `pref_disabled` 下的对照用例断言 `is False`，把 9e39ac1e 的产品决策也锁进测试。

备选（不推荐）：把断言改回 `is False`——只是让现状变绿，仍然耦合全局配置与运行顺序；或把 models.yaml 重新打开——那是产品决策（8 月的富化/搜索基线均运行在 disabled 之上），不应由 testfix 顺手做。

**无需立即修复的后果**：该 3 项失败不掩盖任何回归（被测逻辑本身正确），可按上述提案在测试侧一次性修复。

---

## #4 test_ollama_loopback_config::test_ollama_defaults_use_ipv4_loopback

### 根因链

1. **650029f1**（7/9，"improve review publishing workflow"）引入守卫测试：扫描 `configs/models.yaml`、`app/`、`scripts/` 全部 `.py/.yaml`，禁止出现 `localhost:11434`。动机：`localhost` 在 IPv6-first 解析的系统上会先试 `::1`，而 Ollama 默认只绑 `127.0.0.1`，会导致连接失败/超时——全库必须统一 IPv4 loopback。
2. 当时起全库 17 处 Ollama base_url 全部是 `http://127.0.0.1:11434`（app/pipeline/vlm_runtime.py、app/services/ollama_runtime.py、app/services/vision.py、scripts/rag_100_batch.py 等，实测 grep 确认），测试一直绿。
3. **b382c48a**（8/29 18:43，"Add batch VLM enrichment for folder-import candidates with empty text"）新增 `scripts/enrich_library_candidates.py`，第 44 行：

```python
DEFAULT_BASE_URL = os.environ.get("GIFAGENT_OLLAMA_BASE", "http://localhost:11434")
```

守卫测试自此红， offender 唯一：`scripts/enrich_library_candidates.py`（pytest -v 确认列表里只有这一项）。

### 判定：真实（轻微）bug

- 不是测试过时：守卫测试代表的是全库一致约定与真实故障模式（IPv6 解析优先时连不上 Ollama）。
- 不是环境依赖：静态文本扫描，任何环境都红。
- 影响：富化脚本（即 W8 期间后台富化进程所用脚本）在未设置 `GIFAGENT_OLLAMA_BASE` 的机器上默认走 `localhost`，在这类系统上 VLM 富化请求可能失败。当前这台机器显然解析正常（富化已完成），故实际损害有限，但属于约定破坏 + 潜在可移植性坑。

### 最小修复提案（1 行，scripts/ 文件，非 app/；按红线本次未改，仅提案）

```diff
--- a/scripts/enrich_library_candidates.py
+++ b/scripts/enrich_library_candidates.py
@@ -44 +44 @@
-DEFAULT_BASE_URL = os.environ.get("GIFAGENT_OLLAMA_BASE", "http://localhost:11434")
+DEFAULT_BASE_URL = os.environ.get("GIFAGENT_OLLAMA_BASE", "http://127.0.0.1:11434")
```

`GIFAGENT_OLLAMA_BASE` 环境变量覆盖行为不变；修复后守卫测试恢复绿。

---

## 建议汇总

1. #4：采纳 1 行 diff（scripts/enrich_library_candidates.py:44），立即可做，风险为零。
2. #1–#3：按「monkeypatch 钉住配置 + 增加 disabled 对照用例」修复 tests/test_preference_preflight.py；不改 app/、不改 models.yaml。
3. 撤销 W8 报告中「collect_status 环境依赖，疑与生产库正被富化有关」的记录，代之本报告结论。
4. 顺手防回归（可选）：该类「config 全局态泄漏」问题已第二次出现（本组 4 个失败里 3 个源于此），可考虑在 tests 顶层 conftest 加一个 autouse fixture，把 `app.config._config` 每用例重置为空 dict，强制测试显式声明配置。

## 复现命令（汇总）

```bash
uv run pytest -q tests/test_preference_preflight.py tests/test_ollama_loopback_config.py --no-header
```

## 变更清单

- 本报告：`docs/reports/2026-08-token-sprint/preexisting-test-failures.md`（分支 `sprint/testfix`）
- 代码变更：无（全部为提案，见上文）
