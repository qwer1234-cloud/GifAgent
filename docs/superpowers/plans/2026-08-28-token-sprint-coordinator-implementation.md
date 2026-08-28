# Token Sprint Coordinator + T-1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Each task ends with a focused test run. Commit only the files listed for that task, and only when the user asks to commit (or when they explicitly say to execute this plan including commits).
>
> **This is the only currently executable Workstream.** Design v4.1: W1–W8 execution is frozen until every T-1 gate below is green. Sibling implementation plans may already exist on disk; do not run them.

**Goal:** 在 `sprint/integration` 上形成唯一 `sprint_base_sha`：coordinator 自举协议（共享根、四方哈希链、单写者 manifest）可测且跨独立命令成立；生产库绝对路径与 W3 输入副本钉死；依赖（`hypothesis`、`mypy`，以及 Cosmic Ray spike 通过时的 `cosmic-ray`）冻结；W1–W8 从此 SHA 开分支。

**Architecture:** 先落地 `scripts/token_sprint/coordinator/` 三个工具和 `tests/token_sprint/test_coordinator_protocol.py`（全部用 `tmp_path`，零 git checkpoint 命令），再验 `master == refactor_base_sha`（或 fallback 冻结 `00559804`），再从该 SHA 建干净 worktree `sprint/integration`，在 worktree 内做 Cosmic Ray 限时 spike、提交依赖 / `.gitignore` / 计划文件，得到 `sprint_base_sha`。共享根永远是主检出
`C:\Users\sunhao\Desktop\code\GifAgent\data\token_sprint\`（Git Bash: `/c/Users/sunhao/Desktop/code/GifAgent/data/token_sprint`）。

**Tech Stack:** Python 3.11+、stdlib（`argparse` / `hashlib` / `json` / `pathlib` / `os.replace` / `uuid` / `tempfile` / `unittest.mock`）、现有 `pytest`。T-1 结束前由本会话独占 `uv add --dev`。不引入直连 GLM 的运行时。

**Companion:** `docs/superpowers/plans/2026-08-28-glm-token-sprint-design.md`（v4.1）。格式对齐 `2026-08-28-pipeline-module-split-implementation.md`。

**Sibling plans（本会话撰写，T-1 结束时一并提交，不得执行）：**

| Plan | Branch after T-1 |
|------|------------------|
| `2026-08-28-token-sprint-w1-enrichment-implementation.md` | `sprint/w1-enrichment` |
| `2026-08-28-token-sprint-w2-eval-harness-implementation.md` | `sprint/w2-eval-harness` |
| `2026-08-28-token-sprint-w3-data-audit-implementation.md` | `sprint/w3-data-audit` |
| `2026-08-28-token-sprint-w4-mutation-implementation.md` | `sprint/w4-mutation` |
| `2026-08-28-token-sprint-w5-property-tests-implementation.md` | `sprint/w5-property-tests` |
| `2026-08-28-token-sprint-w6-mypy-implementation.md` | `sprint/w6-mypy`（默认不启动） |
| `2026-08-28-token-sprint-w7-code-audit-implementation.md` | `sprint/w7-code-audit` |
| `2026-08-28-token-sprint-w8-shadow-fts-implementation.md` | `sprint/w8-shadow-fts` |

**Shell:** ZCode Windows Auto 优先 Git Bash，其次 cmd。本计划所有命令按 Git Bash 书写。禁止 PowerShell 专有语法（`Get-Item`、`Select-Object`、here-string 当唯一路径）。禁止依赖会话环境变量跨调用持久性：每次命令都传 `--sprint-root`。

---

## Constraints

### 全局约束（从设计文档原样复制）

1. **所有模型消耗只经 ZCode harness。** 不新增任何调用 GLM/智谱端点的运行时代码或脚本；
   不把活动额度 API Key 写进任何配置、代码或文档。
2. **T-1 验收前 W1–W8 禁止执行**（实施方案文本可撰写）。所有 W 分支必须从
   `sprint_base_sha` 创建，只合入 `sprint/integration`；未经用户确认不合回 `master`。
3. 不修改「与进行中重构的关系」列出的文件；管线符号只经 facade import。
4. **共享根传递协议：** `scripts/token_sprint/*` 所有脚本必须接受 `--sprint-root` 参数；
   未传参时才读环境变量 `GIFAGENT_SPRINT_ROOT` 作为显式 fallback；两者皆缺或路径不存在时
   立即报错退出。禁止依赖 PowerShell 专有语法；脚本须可在 Git Bash 与 cmd 下运行。
5. 不写根层 `data/*.db*`、`data/exports/`、`data/labels/`、checkpoint、`dist/`、真实桌面同步目录。
   产物只进共享根。消费跨 Workstream 产物前必须按 canonical manifest 校验 SHA-256。
6. **manifest 单写者 + review 哈希绑定协议：** executor 只原子发布产物与 `.ready`；
   reviewer 校验 `.ready.artifact_sha256` 与实际文件一致后，原子写 `.done` 与唯一 receipt。
   receipt 文件名使用 Windows 安全时间戳 `YYYYMMDDTHHMMSSfffZ` + UUID，内容至少包含
   `receipt_id`、`artifact_path`、`artifact_sha256`、`ready_path`、`ready_sha256`、`done_path`、
   `done_sha256`、producer、reviewer、workstream、ts。canonical `sprint_manifest.json` 只由
   协调会话在验证实际文件、`.ready`、`.done`、receipt 四者一致后原子重写；重复 receipt
   按 `receipt_id` 幂等处理。原子写的临时文件必须与目标文件同目录。
   此四方链只约束跨 Workstream 数据/工程产物；`sprint_manifest.json`、`inputs_manifest.json`、
   `approved_enrichment_manifest.json`、`usage-log.md` 与 receipt 归档状态属于 coordinator 控制面，
   由协调会话单写者原子维护，不为自身生成 receipt。canonical manifest 应记录其他控制面文件
   的 SHA 与 schema/revision，但不得递归记录自己的 SHA。
7. 不改 `configs/models.yaml` / `configs/models.adult_candidate.yaml`。
8. **依赖与 lockfile 由协调会话独占**：所有依赖决策必须在 T-1、`sprint_base_sha` 形成前
   完成；任何 Workstream 不得运行 `uv add` 或改 `pyproject.toml` / `uv.lock`
   （白名单：`hypothesis`、`mypy`、`cosmic-ray`）。基线形成后不得追加白名单依赖。
9. 测试只用 `tmp_path` / 内存 SQLite / 共享根内快照副本。新测试只进三个新目录：
   `tests/property/`、`tests/mutation/`、`tests/token_sprint/`；不改已有测试文件。
10. 数据分片包只处理**文本**。不让任何 agent 读取 GIF、JPG 或视频内容。
11. 分片纪律：每片 50–200 条；一片一个会话；输出独立
    `shard-XX.{pass}.{prompt_version}.{attempt_id}.jsonl`；`attempt_id` 使用 UUID，禁止覆盖旧产物；
    拒绝行记 `{"candidate_id": ..., "refused": true}`；合法 JSON 但成人语义被消毒的行
    视同失败（保真检查识别）。
12. 工作包状态：executor 只产绑定 artifact SHA 的 `.ready`；`.done` 只由 reviewer 对同一
    SHA 实跑测试/QC 后原子生成；reviewer 再发布同 SHA receipt。`.ready` 后禁止原地修改产物，
    红线与台账只统计 `.done`。
13. 用量记录：每类工作包完成后记录 ZCode 使用统计；每 2 小时抄录控制台余量。
    两者写入 `docs/reports/2026-08-token-sprint/usage-log.md`（协调会话独占）。
14. 用户可见文案中文，代码标识与测试名英文。commit 风格对齐仓库现状。
15. 每个 Workstream 在独立 git worktree + 分支（`sprint/w{N}-{slug}`）上工作，
    并作为独立 ZCode 工作区打开。

### Coordinator 额外禁区

- 禁止 `git add -A`。禁止把脏工作区打成基线。不明产物（`-I`、`nomic-embed-text`、数据结果）不入库，登记为待清理项。
- 禁止从游离 commit 开工：非 fallback 时 `git rev-parse master` 必须等于 `refactor_base_sha`。
- 不存在「先从旧 HEAD 开工、基线出现后 rebase」路径。开工后禁止给已开工 worktree 换基线。
- 重构 File Map 内文件只读。不提交重构 agent 范围外的已跟踪改动。
- 控制面文件（manifest / inputs_manifest / usage-log / approved enrichment manifest）不为自身发 receipt。
- Tasks 1–5 的计划正文不得包含任何 `git worktree` / `git commit` / `git rev-parse master` checkpoint 命令（那些命令从 Task 6 才允许出现）。

---

## File Map

全部落在协调会话所有权矩阵内。

**Create**

- `scripts/token_sprint/__init__.py` — 空包标记
- `scripts/token_sprint/coordinator/__init__.py` — 再导出 `resolve_sprint_root`、`atomic_write_text`、`sha256_file`
- `scripts/token_sprint/coordinator/common.py` — `--sprint-root` 解析、SHA-256、同目录原子写、Windows 安全时间戳、receipt 文件名
- `scripts/token_sprint/coordinator/preflight.py` — `--write-nonce` / `--verify-nonce`
- `scripts/token_sprint/coordinator/prepare_inputs.py` — `Path.resolve(strict=True)` 钉死源库，复制 W3 输入，写 `inputs_manifest.json`
- `scripts/token_sprint/coordinator/reconcile_receipts.py` — 四方哈希链、按 `receipt_id` 幂等重写 `sprint_manifest.json`、归档 receipt
- `scripts/token_sprint/coordinator/protocol.py` — `.ready` / `.done` / receipt 的 schema 校验与哈希绑定（给 W1–W8 import；本会话独占写入）
- `tests/token_sprint/__init__.py` — 空包标记
- `tests/token_sprint/test_coordinator_protocol.py`
- `docs/reports/2026-08-token-sprint/usage-log.md` — 空模板（Task 11）

**Modify（仅 Task 8，且只在 `sprint/integration` worktree 内）**

- `pyproject.toml` / `uv.lock` — `hypothesis`、`mypy`；（spike 通过时）`cosmic-ray`
- `.gitignore` — 追加 `data/token_sprint/`

**Create at shared root（不入库，运行时）**

```
data/token_sprint/
├── sprint_manifest.json
├── inputs_manifest.json
├── receipts/
│   └── processed/
├── inputs/
├── shards/
└── evalsets/
```

**Do not create / do not modify**

- 重构 File Map 内任何文件
- `configs/models.yaml`、`configs/models.adult_candidate.yaml`
- 根层 `data/*.db*`
- W1–W8 脚本（本计划只写 coordinator 包）

---

## Shared interfaces (lock these names)

`scripts/token_sprint/coordinator/common.py` 必须提供：

```python
SPRINT_ROOT_ENV = "GIFAGENT_SPRINT_ROOT"

def add_sprint_root_argument(parser: argparse.ArgumentParser) -> None: ...
def resolve_sprint_root(cli_value: str | None) -> Path: ...
def sha256_file(path: Path) -> str: ...
def sha256_bytes(data: bytes) -> str: ...
def atomic_write_bytes(path: Path, data: bytes) -> None: ...
def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None: ...
def windows_safe_ts(dt: datetime | None = None) -> str: ...  # YYYYMMDDTHHMMSSfffZ
def receipt_filename(workstream: str, *, dt: datetime | None = None, receipt_id: str | None = None) -> str: ...
```

规则：

- `resolve_sprint_root(None)` 才读 `GIFAGENT_SPRINT_ROOT`。两者皆缺、或 `Path(raw).expanduser().resolve()` 后不存在或不是目录 → `SystemExit`（stderr 英文 `error:` 前缀，exit 2）。
- `atomic_write_*` 的临时文件必须 `path.with_name("." + path.name + ".tmp." + pid + "." + uuid)`，与目标同目录，写完 `os.replace`。禁止 `tempfile.mkstemp` 到 `%TEMP%`。
- `windows_safe_ts` 禁止冒号。`receipt_filename` 形状：`{workstream}_{YYYYMMDDTHHMMSSfffZ}_{uuid}.json`，`workstream` 只允许 `[a-z0-9-]+`。

`protocol.py` 必须提供：

```python
READY_SUFFIX = ".ready"
DONE_SUFFIX = ".done"

def write_ready(artifact: Path, *, producer: str, qc_summary: dict) -> Path: ...
def write_done(artifact: Path, ready: Path, *, reviewer: str, qc_evidence: dict, verdict: str = "pass") -> Path: ...
def write_receipt(*, sprint_root: Path, workstream: str, producer: str, reviewer: str,
                  artifact: Path, ready: Path, done: Path) -> Path: ...
def verify_hash_chain(artifact: Path, ready: Path, done: Path, receipt: Path) -> None:
    """Raise HashChainError if any of the four disagrees."""
```

`.ready` JSON 字段：`artifact_path`（POSIX 风格字符串）、`artifact_sha256`、`qc_summary`、`producer`、`ts`。
`.done` JSON 字段：`artifact_path`、`artifact_sha256`、`ready_path`、`ready_sha256`、`qc_evidence`、`review_verdict`、`reviewer`、`ts`。
receipt JSON 字段：设计全局约束 6 列出的全部键。`artifact_path` 等路径写入时用 `Path.resolve()` 后的绝对路径，分隔符统一 `/`。

`sprint_manifest.json` schema（不得含自身 sha256 字段）：

```json
{
  "schema": "gifagent.token_sprint.manifest.v1",
  "revision": 1,
  "as_of": "2026-08-28T00:00:00.000Z",
  "refactor_base_sha": "",
  "sprint_base_sha": "",
  "fallback_frozen": false,
  "fallback_reason": null,
  "cosmic_ray": {"status": "undecided", "reason": ""},
  "vector_math_in_baseline": false,
  "cleanup_pending": [],
  "control_plane": {
    "inputs_manifest.json": {"sha256": null, "schema": "gifagent.token_sprint.inputs.v1"},
    "approved_enrichment_manifest.json": {"sha256": null, "schema": "gifagent.token_sprint.approved_enrichment.v1"},
    "usage-log.md": {"sha256": null}
  },
  "artifacts": {}
}
```

`inputs_manifest.json` schema：

```json
{
  "schema": "gifagent.token_sprint.inputs.v1",
  "as_of": "",
  "source_db_abs": "",
  "source_db_sha256": "",
  "files": [
    {
      "key": "adaptive_test_result",
      "source_abs": "",
      "copy_rel": "inputs/adaptive_test_result.json",
      "sha256": "",
      "required": true,
      "status": "copied"
    }
  ]
}
```

`status` 枚举：`copied` | `absent`。`required: true` 且源缺失 → 脚本 exit 2，不得写残缺清单。`required: false` 且缺失 → 记录 `status: "absent"`、`sha256: null`，不得静默跳过。

---

## Phase 0 — Coordinator bootstrap (no git checkpoint commands)

本 Phase 只新增 coordinator 文件并用 `tmp_path` 测通。不要提交。不要创建 worktree。不要改 `pyproject.toml`。

### Task 1: `common.py` sprint-root + atomic write

**Files:**

- Create: `scripts/token_sprint/__init__.py`, `scripts/token_sprint/coordinator/__init__.py`, `scripts/token_sprint/coordinator/common.py`, `tests/token_sprint/__init__.py`, `tests/token_sprint/test_coordinator_protocol.py`

**Interfaces:** 上一节 `common.py` 名单。

- [ ] **Step 1: 实现 `common.py`**

`resolve_sprint_root` 必须先看 CLI 参数，只有 `cli_value in (None, "")` 时才读 env。路径用 `Path(raw).expanduser()` 再 `resolve()`（此处不要 `strict=True`，目录必须已经存在所以随后 `.exists()` 即可）。Git Bash 传入 `/c/Users/...` 与 cmd 传入 `C:\Users\...` 都必须成功。

- [ ] **Step 2: 写测试（全部 `tmp_path`）**

在 `test_coordinator_protocol.py` 至少包含：

```python
def test_missing_sprint_root_exits(monkeypatch):
    monkeypatch.delenv("GIFAGENT_SPRINT_ROOT", raising=False)
    with pytest.raises(SystemExit):
        resolve_sprint_root(None)

def test_nonexistent_sprint_root_exits(tmp_path):
    with pytest.raises(SystemExit):
        resolve_sprint_root(str(tmp_path / "nope"))

def test_cli_wins_over_env(tmp_path, monkeypatch):
    other = tmp_path / "other"; other.mkdir()
    monkeypatch.setenv("GIFAGENT_SPRINT_ROOT", str(other))
    assert resolve_sprint_root(str(tmp_path)) == tmp_path.resolve()

def test_env_fallback_when_cli_omitted(tmp_path, monkeypatch):
    monkeypatch.setenv("GIFAGENT_SPRINT_ROOT", str(tmp_path))
    assert resolve_sprint_root(None) == tmp_path.resolve()

def test_atomic_write_same_directory(tmp_path):
    target = tmp_path / "manifest.json"
    atomic_write_text(target, '{"ok": true}')
    leftovers = [p.name for p in tmp_path.iterdir() if ".tmp." in p.name]
    assert leftovers == []
    assert target.read_text(encoding="utf-8") == '{"ok": true}'

def test_receipt_filename_is_windows_safe():
    name = receipt_filename("w1")
    assert ":" not in name
    assert name.endswith(".json")
    assert name.startswith("w1_")
```

- [ ] **Step 3: 跑测试**

```bash
uv run pytest -q tests/token_sprint/test_coordinator_protocol.py -k "sprint_root or atomic_write or receipt_filename"
```

Expected: PASS。

- [ ] **Step 4: 不提交。** 进入 Task 2。

---

### Task 2: `preflight.py` two-process nonce

**Files:**

- Create: `scripts/token_sprint/coordinator/preflight.py`
- Modify: `tests/token_sprint/test_coordinator_protocol.py`

**CLI:**

```text
python scripts/token_sprint/coordinator/preflight.py --sprint-root <path> --write-nonce
python scripts/token_sprint/coordinator/preflight.py --sprint-root <path> --verify-nonce
```

`--write-nonce` 与 `--verify-nonce` 互斥，缺一 exit 2。nonce 文件固定为 `<sprint-root>/_preflight_nonce.txt`（UTF-8，单行 32 hex）。`--write-nonce` 覆盖写新 nonce 并打印路径。`--verify-nonce` 读回：文件缺失或不是 32 hex → exit 2；成功 → stdout `preflight ok` exit 0。不要依赖 env 在两次调用之间存活。

- [ ] **Step 1: 实现 CLI**（`if __name__ == "__main__"`，内部 `sys.path` 插入仓库根，与 `scripts/import_adaptive_candidates.py` 相同）。

- [ ] **Step 2: 测试必须真的起两次进程**（不是只调函数）：

```python
def test_preflight_two_processes(tmp_path):
    script = Path("scripts/token_sprint/coordinator/preflight.py")
    write = subprocess.run(
        [sys.executable, str(script), "--sprint-root", str(tmp_path), "--write-nonce"],
        check=True, capture_output=True, text=True,
    )
    verify = subprocess.run(
        [sys.executable, str(script), "--sprint-root", str(tmp_path), "--verify-nonce"],
        check=True, capture_output=True, text=True,
    )
    assert "preflight ok" in verify.stdout

def test_preflight_rejects_missing_sprint_root():
    script = Path("scripts/token_sprint/coordinator/preflight.py")
    proc = subprocess.run(
        [sys.executable, str(script), "--write-nonce"],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0
```

另测：`--sprint-root` 指向不存在目录 → 非零退出。

- [ ] **Step 3: 跑测试**

```bash
uv run pytest -q tests/token_sprint/test_coordinator_protocol.py -k preflight
```

Expected: PASS。

- [ ] **Step 4: 不提交。**

---

### Task 3: protocol hash chain (ready / done / receipt)

**Files:**

- Create: `scripts/token_sprint/coordinator/protocol.py`
- Modify: `tests/token_sprint/test_coordinator_protocol.py`

- [ ] **Step 1: 实现 `write_ready` / `write_done` / `write_receipt` / `verify_hash_chain`**

`write_ready` 先对 artifact 现算 SHA，写入 `<artifact>.ready`。返回该 path。
`write_done` 重新计算 artifact SHA，必须等于 ready 内 `artifact_sha256`，否则 raise。写入 `<artifact>.done`。
`write_receipt` 写入 `<sprint-root>/receipts/<receipt_filename>`（`receipts/` 不存在则创建）。receipt 内填四方 path + sha。
`verify_hash_chain`：重算四个文件，任一不匹配 raise `HashChainError`。

`.ready` 形成后本模块不提供「原地改 artifact」API。

- [ ] **Step 2: 测试**

```python
def test_hash_chain_happy_path(tmp_path):
    artifact = tmp_path / "shard-00.p3.v1.uuid.jsonl"
    artifact.write_text('{"candidate_id":"c1"}\n', encoding="utf-8")
    ready = write_ready(artifact, producer="exec-1", qc_summary={"schema_ok": 1.0})
    done = write_done(artifact, ready, reviewer="rev-1", qc_evidence={"sampled": 3})
    receipt = write_receipt(
        sprint_root=tmp_path, workstream="w1",
        producer="exec-1", reviewer="rev-1",
        artifact=artifact, ready=ready, done=done,
    )
    verify_hash_chain(artifact, ready, done, receipt)

@pytest.mark.parametrize("which", ["artifact", "ready", "done", "receipt"])
def test_hash_chain_rejects_mismatch(tmp_path, which):
    # build a valid chain, then mutate exactly one file's bytes, expect HashChainError
    ...

def test_ready_then_mutate_artifact_is_detected(tmp_path):
    artifact = tmp_path / "a.jsonl"
    artifact.write_text("a\n", encoding="utf-8")
    ready = write_ready(artifact, producer="e", qc_summary={})
    artifact.write_text("b\n", encoding="utf-8")  # forbidden in production; test must catch
    with pytest.raises(HashChainError):
        write_done(artifact, ready, reviewer="r", qc_evidence={})
```

- [ ] **Step 3: 跑测试**

```bash
uv run pytest -q tests/token_sprint/test_coordinator_protocol.py -k "hash_chain or ready_then"
```

Expected: PASS。

- [ ] **Step 4: 不提交。**

---

### Task 4: `reconcile_receipts.py` idempotent manifest rewrite + crash replay

**Files:**

- Create: `scripts/token_sprint/coordinator/reconcile_receipts.py`
- Modify: `tests/token_sprint/test_coordinator_protocol.py`

**CLI:**

```text
python scripts/token_sprint/coordinator/reconcile_receipts.py --sprint-root <path> --once
```

行为：

1. 扫描 `<sprint-root>/receipts/*.json`（忽略 `processed/` 与临时 `.tmp.` 文件）。
2. 对每个 receipt 调 `verify_hash_chain`。失败 → 该 receipt 留在原地，stderr 记录，处理下一个（`--once` 结束码：有失败则 1，全成功 0）。
3. 成功则按 `receipt_id` 幂等写入 canonical `sprint_manifest.json` 的 `artifacts` 映射：已存在且内容等价 → 仍归档；已存在但字段冲突 → 拒绝该 receipt（不覆盖）。
4. 原子重写 manifest（同目录临时文件 + `os.replace`）。**不要**把 manifest 自己的 SHA 写进文件。
5. 更新 `control_plane` 中其他控制面文件的 SHA（`inputs_manifest.json` 等若存在则重算）。
6. `revision` 每次实际有变更时 +1；纯幂等重放（manifest 字节级不变）不得 +1。
7. 成功后 `os.replace` 把 receipt 移到 `receipts/processed/`。若在写入 manifest 之后、移动之前崩溃：重启重放同一 `receipt_id` 必须得到相同 `artifacts` 内容。

- [ ] **Step 1: 实现。** 把「读现有 manifest → 应用一枚 receipt → 序列化」提成纯函数 `apply_receipt(manifest: dict, receipt: dict) -> dict`，方便崩溃重放测试。

- [ ] **Step 2: 测试**

```python
def test_reconcile_idempotent_duplicate_receipt(tmp_path): ...
def test_reconcile_crash_after_manifest_before_move(tmp_path, monkeypatch):
    """Patch os.replace to fail on the processed/ move once; rerun --once; artifacts equal."""
def test_reconcile_rejects_any_hash_mismatch(tmp_path): ...
def test_manifest_does_not_record_own_sha(tmp_path):
    data = json.loads((tmp_path / "sprint_manifest.json").read_text())
    assert "sha256" not in data
    assert data["schema"] == "gifagent.token_sprint.manifest.v1"
```

- [ ] **Step 3: 跑测试**

```bash
uv run pytest -q tests/token_sprint/test_coordinator_protocol.py -k reconcile
```

Expected: PASS。

- [ ] **Step 4: 不提交。**

---

### Task 5: `prepare_inputs.py` absolute source + copy hashes

**Files:**

- Create: `scripts/token_sprint/coordinator/prepare_inputs.py`
- Modify: `tests/token_sprint/test_coordinator_protocol.py`

**CLI:**

```text
python scripts/token_sprint/coordinator/prepare_inputs.py --sprint-root <path> \
  --source-db <abs> \
  --input-adaptive <abs-or-omit> \
  --input-checkpoint <abs-or-omit> \
  --input-export-glob <abs-glob-or-omit>
```

默认源（执行阶段才会用到真实路径；本 Task 测试只用 `tmp_path` 伪造）：

| key | default source | required |
|-----|----------------|----------|
| `library` | `C:/Users/sunhao/Desktop/code/GifAgent/dist/GifAgentUI/data/library.db` | yes |
| `adaptive_test_result` | `C:/Users/sunhao/Desktop/code/GifAgent/data/adaptive_test_result.json` | no |
| `batch_checkpoint` | `C:/Users/sunhao/Desktop/code/GifAgent/data/batch_checkpoint.json` | no |
| `export_results` | `C:/Users/sunhao/Desktop/code/GifAgent/data/exports/*.json`（仅 JSON，不复制 GIF） | no |

规则：

- `--source-db` 必须 `Path(source).resolve(strict=True)`，把解析后的绝对路径写入 `source_db_abs`（正斜杠）。文件锁/缺失 → exit 2。
- 复制用 `shutil.copy2` 到 `<sprint-root>/inputs/<basename>`，再哈希。
- 可选源缺失：`status: "absent"`，不得假装 copied。
- 原子写 `inputs_manifest.json`。
- **不要**打开生产库做 VACUUM（那是 W1）。本脚本只记录路径与副本。
- 只复制文本/JSON/SQLite 文件。任何 `.gif` / `.jpg` / `.mp4` / `.webm` 出现在 glob 里必须跳过并记 `skipped_media` 列表，不得读媒体内容。

- [ ] **Step 1: 实现。**

- [ ] **Step 2: 测试用 `tmp_path` 造一个最小 sqlite 文件当源库：**

```python
def test_prepare_inputs_resolves_strict_and_copies(tmp_path):
    src_db = tmp_path / "prod" / "library.db"
    src_db.parent.mkdir(); src_db.write_bytes(b"sqlite-fake")
    optional_missing = tmp_path / "nope.json"
    sprint = tmp_path / "sprint"; sprint.mkdir()
    subprocess.run([sys.executable, "scripts/token_sprint/coordinator/prepare_inputs.py",
                    "--sprint-root", str(sprint), "--source-db", str(src_db),
                    "--input-adaptive", str(optional_missing)], check=False)
    # missing required-false adaptive → status absent, but source_db copied
```

另测：`--source-db` 不存在 → 非零退出，不写残缺 manifest。

- [ ] **Step 3: 跑测试 + 全文件**

```bash
uv run pytest -q tests/token_sprint/test_coordinator_protocol.py
```

Expected: PASS。覆盖设计要求的：缺失/非法 `--sprint-root`、两进程 nonce、四方哈希任一不匹配即拒绝、重复 receipt 幂等、manifest 写入后崩溃重放、Windows 安全 receipt 文件名。

- [ ] **Step 4: 不提交。** Phase 0 完成后再进入 Phase 1。

---

## Phase 1 — Refactor checkpoint + integration worktree

此 Phase 才允许 git checkpoint 命令。

### Task 6: `refactor_base_sha` master 落地门

**Files:** none created. 工作笔记写入共享根（此时若共享根尚无，先 `mkdir -p` 主检出下的 `data/token_sprint`，该目录最终会被 gitignore）。

用户已选定「让重构 agent 提交」。本会话**不**替重构 agent 提交其 File Map。

- [ ] **Step 1: 记录当前 `master` 与工作区残留（只读）**

```bash
git rev-parse master
git rev-parse HEAD
git status --porcelain --untracked-files=all
git diff --stat master
```

对照重构计划 File Map。把不在 File Map 内的已跟踪改动和不明产物（`-I`、`nomic-embed-text`、数据结果）列入 `cleanup_pending`，**禁止**把它们打进任何 checkpoint。

- [ ] **Step 2: 判定**

**非 fallback（期望路径）：** 重构 agent 已把 scoped checkpoint 提交到 `master`。令 `REFACTOR_BASE=$(git rev-parse master)`。必须同时满足：

1. `git rev-parse master` == `git rev-parse HEAD`（当前就在 master）或明确 `git checkout master` 后相等。
2. 该 commit 能逐文件解释为重构 File Map 内代码/测试；`app/services/vector_math.py` 仅当调用方与测试一起闭环时才算纳入。
3. 重构 agent 的三件套（staged diff、测试清单、回归结果）可核对。

若 `git rev-parse master` 不等于重构 agent 声称的 SHA：**停止**。不得从游离 commit 继续。向用户报告两个 SHA。

**Fallback（重构无法短期形成干净 checkpoint）：** 经用户确认后冻结 `REFACTOR_BASE=00559804`。在即将写入的 manifest 里设 `fallback_frozen: true` 与 `fallback_reason`。然后从范围**删除**（不是暂挂）：W4 白名单中的 `vector_math.py`、W5 两条 `vector_math` 性质、以及基线中不存在的任何测试依赖。`vector_math_in_baseline: false`。

- [ ] **Step 3: 记录 `vector_math_in_baseline`**

```bash
git cat-file -e "${REFACTOR_BASE}:app/services/vector_math.py" && echo present || echo absent
```

若 present，再确认该 commit 中存在调用方（`preference_memory` 或测试）。只文件存在但无闭环 → 仍记 `false` 并列入 `cleanup_pending`，不要把它算进冲刺范围。

- [ ] **Step 4: 本 Task 无 pytest。** 门：`REFACTOR_BASE` 已写入工作笔记；fallback 时删除项已列出。不提交。

---

### Task 7: 干净 `sprint/integration` worktree

**Files:** 把 Phase 0 的未跟踪文件拷进 worktree（主检出不提交它们）。

- [ ] **Step 1: 从 `REFACTOR_BASE` 创建 worktree + 分支**

```bash
mkdir -p .worktrees
git worktree add .worktrees/sprint-integration -b sprint/integration "$REFACTOR_BASE"
```

`.gitignore` 已忽略 `.worktrees/`。worktree 必须是干净的：

```bash
git -C .worktrees/sprint-integration status --porcelain --untracked-files=all
```

Expected: 空。若非空：**停止**。

- [ ] **Step 2: 拷贝 coordinator 文件与全部 token-sprint 计划文件进 worktree**（显式路径，禁止 `cp -r` 整个脏工作区）：

```bash
WT=".worktrees/sprint-integration"
mkdir -p "$WT/scripts/token_sprint/coordinator" "$WT/tests/token_sprint" "$WT/docs/superpowers/plans"
cp scripts/token_sprint/__init__.py "$WT/scripts/token_sprint/"
cp scripts/token_sprint/coordinator/*.py "$WT/scripts/token_sprint/coordinator/"
cp tests/token_sprint/__init__.py "$WT/tests/token_sprint/"
cp tests/token_sprint/test_coordinator_protocol.py "$WT/tests/token_sprint/"
cp docs/superpowers/plans/2026-08-28-glm-token-sprint-design.md "$WT/docs/superpowers/plans/"
cp docs/superpowers/plans/2026-08-28-token-sprint-*-implementation.md "$WT/docs/superpowers/plans/"
```

- [ ] **Step 3: 在 worktree 内重跑协议测试**

```bash
cd .worktrees/sprint-integration
uv run pytest -q tests/token_sprint/test_coordinator_protocol.py
uv run pytest -q tests/test_adaptive_config.py
# 若基线含该文件则一并：
if [ -f tests/test_pipeline_facade.py ]; then uv run pytest -q tests/test_pipeline_facade.py; fi
```

Expected: PASS。

- [ ] **Step 4: 不提交。** 提交发生在 Task 8 一次性完成依赖与 gitignore 之后。

---

### Task 8: Cosmic Ray spike + 依赖冻结 + gitignore + sprint_base_sha

**Files (worktree):** `pyproject.toml`, `uv.lock`, `.gitignore`，以及 Phase 0/计划文件（第一次提交）。

**Spike 时限：30 分钟墙钟。** 在一次性临时目录隔离 venv 中进行，**不要**先改项目 `pyproject.toml`。

- [ ] **Step 1: 隔离 spike**

```bash
SPIKE=$(mktemp -d)
python -m venv "$SPIKE/venv"
# Git Bash:
"$SPIKE/venv/Scripts/python.exe" -m pip install cosmic-ray pytest
```

若 Windows 上 `pip install cosmic-ray` 失败、导入失败、或无法对下面最小目标生成并验杀突变 → **立即 fallback**，记录原因，本冲刺不再试 Cosmic Ray。W4 将使用 `scripts/token_sprint/mutation_harness.py`（该文件由 W4 创建，本会话不写）。

最小目标（写在 `$SPIKE/target.py`）：

```python
def sentinel_one() -> int:
    return 1
```

`$SPIKE/test_sentinel.py`：`assert sentinel_one() == 1`。
通过条件：能安装、能生成至少 1 个会改变返回值的突变、用 pytest 验杀该突变。超时或失败都算 fallback。

- [ ] **Step 2: 在 worktree 一次性加 dev 依赖**（本计划允许 `uv add`，W1–W8 计划禁止）

```bash
cd .worktrees/sprint-integration
uv add --dev hypothesis mypy
# 仅当 Step 1 通过：
uv add --dev cosmic-ray
```

不要添加别的包。不要改 `[tool.mypy]`（W6 启动时再由本会话落盘）。

- [ ] **Step 3: `.gitignore` 追加一行 `data/token_sprint/`**（若尚无）。不要改动其他 ignore 规则。

- [ ] **Step 4: 在 worktree 跑协议测试 + 冒烟**

```bash
uv run pytest -q tests/token_sprint/test_coordinator_protocol.py tests/test_adaptive_config.py
```

- [ ] **Step 5: 单次提交得到 `sprint_base_sha`**（显式 add，禁止 `-A`）

```bash
git add -- \
  pyproject.toml uv.lock .gitignore \
  scripts/token_sprint/__init__.py \
  scripts/token_sprint/coordinator/__init__.py \
  scripts/token_sprint/coordinator/common.py \
  scripts/token_sprint/coordinator/preflight.py \
  scripts/token_sprint/coordinator/prepare_inputs.py \
  scripts/token_sprint/coordinator/protocol.py \
  scripts/token_sprint/coordinator/reconcile_receipts.py \
  tests/token_sprint/__init__.py \
  tests/token_sprint/test_coordinator_protocol.py \
  docs/superpowers/plans/2026-08-28-glm-token-sprint-design.md \
  docs/superpowers/plans/2026-08-28-token-sprint-coordinator-implementation.md \
  docs/superpowers/plans/2026-08-28-token-sprint-w1-enrichment-implementation.md \
  docs/superpowers/plans/2026-08-28-token-sprint-w2-eval-harness-implementation.md \
  docs/superpowers/plans/2026-08-28-token-sprint-w3-data-audit-implementation.md \
  docs/superpowers/plans/2026-08-28-token-sprint-w4-mutation-implementation.md \
  docs/superpowers/plans/2026-08-28-token-sprint-w5-property-tests-implementation.md \
  docs/superpowers/plans/2026-08-28-token-sprint-w6-mypy-implementation.md \
  docs/superpowers/plans/2026-08-28-token-sprint-w7-code-audit-implementation.md \
  docs/superpowers/plans/2026-08-28-token-sprint-w8-shadow-fts-implementation.md
git commit -m "chore: freeze token-sprint coordinator protocol, plans, and dev deps"
```

若某个 sibling plan 文件尚未存在：先停，去主检出补齐再拷贝，不要提交残缺计划集。

```bash
SPRINT_BASE=$(git rev-parse HEAD)
echo "$SPRINT_BASE"
```

此后禁止再改 `pyproject.toml` / `uv.lock`。

---

## Phase 2 — Shared root + real inputs + initial manifest

共享根永远在**主检出**：`C:/Users/sunhao/Desktop/code/GifAgent/data/token_sprint`。不要在 worktree 里建第二份共享根。

### Task 9: 创建共享根并采集真实 inputs

**Files:** `data/token_sprint/inputs_manifest.json` 及 `inputs/` 副本（gitignore，不入库）。

- [ ] **Step 1:**

```bash
SPRINT_ROOT="/c/Users/sunhao/Desktop/code/GifAgent/data/token_sprint"
mkdir -p "$SPRINT_ROOT/receipts/processed" "$SPRINT_ROOT/inputs" "$SPRINT_ROOT/shards" "$SPRINT_ROOT/evalsets"
```

- [ ] **Step 2: 跨两次独立命令跑预检（本会话）**

```bash
python scripts/token_sprint/coordinator/preflight.py --sprint-root "$SPRINT_ROOT" --write-nonce
python scripts/token_sprint/coordinator/preflight.py --sprint-root "$SPRINT_ROOT" --verify-nonce
```

Expected: 第二次打印 `preflight ok`。不要 `export GIFAGENT_SPRINT_ROOT` 来「省掉」参数。

- [ ] **Step 3: 采集真实输入**（在 worktree 或主检出跑均可，脚本是纯文件系统）

```bash
python scripts/token_sprint/coordinator/prepare_inputs.py --sprint-root "$SPRINT_ROOT" \
  --source-db "C:/Users/sunhao/Desktop/code/GifAgent/dist/GifAgentUI/data/library.db"
```

可选 JSON 用默认路径；缺失必须出现在 manifest 的 `status: absent`。`source_db_abs` 必须是 `resolve(strict=True)` 后的绝对路径且文件存在。

生产库缺失：**停止 T-1**，不要用根层 `data/library.db` 顶替。

- [ ] **Step 4: 本 Task 验证 = 读 `inputs_manifest.json` 人工核对 `source_db_abs` 存在。** 不提交（共享根不入库）。

---

### Task 10: 初始 canonical `sprint_manifest.json` + usage-log

**Files:**

- Create: `docs/reports/2026-08-token-sprint/usage-log.md`（在 worktree 内，随后单独 commit 或并入下一提交）
- Runtime: `data/token_sprint/sprint_manifest.json`

- [ ] **Step 1: 写 usage-log 模板**

```markdown
# Token sprint usage log

| ts_utc | source | remaining_or_session | note |
|--------|--------|----------------------|------|
```

- [ ] **Step 2: 写初始 manifest**（用 `atomic_write_text`，不要手搓半截 JSON）。填入：

  - `refactor_base_sha` 或 fallback 字段
  - `sprint_base_sha` = Task 8 的 HEAD
  - `cosmic_ray.status` = `included` | `fallback`
  - `vector_math_in_baseline`
  - `cleanup_pending`
  - `control_plane.inputs_manifest.json.sha256` = 现算
  - `artifacts`: `{}`
  - 不要写自身 SHA

- [ ] **Step 3: 在 worktree 提交 usage-log（显式 add）**

```bash
git add -- docs/reports/2026-08-token-sprint/usage-log.md
git commit -m "docs: add token-sprint usage-log scaffold"
```

此提交**不是**新的 sprint 基线。W 分支仍从 Task 8 的 `sprint_base_sha` 创建。usage-log 后续由协调会话在 `sprint/integration` 快进。把这一点写进 manifest 旁注（工作笔记即可，不要把 usage-log commit SHA 当成 `sprint_base_sha`）。

若希望 W 分支也能看到 usage-log 模板：可以 amend **仅当** Task 8 提交是本会话刚创建、未推送、且用户明确要求。默认不 amend；W 不需要该模板也能开工。

---

### Task 11: 第二会话预检 + 协议测试终跑

- [ ] **Step 1:** 在 `sprint/integration` worktree 再跑一次两命令预检（新的 shell 进程，仍显式 `--sprint-root`）。

- [ ] **Step 2:** 另开一个将用于 W 的临时 worktree 或至少新的 shell cwd，再跑一遍预检。设计要求「协调会话与至少一个 W worktree 会话中各跑一遍」。最小满足：

```bash
git worktree add .worktrees/sprint-w4-mutation -b sprint/w4-mutation "$SPRINT_BASE"
python scripts/token_sprint/coordinator/preflight.py --sprint-root "$SPRINT_ROOT" --write-nonce
python scripts/token_sprint/coordinator/preflight.py --sprint-root "$SPRINT_ROOT" --verify-nonce
```

W4 worktree 此刻不要执行 W4 计划。预检通过即可。

- [ ] **Step 3:**

```bash
cd .worktrees/sprint-integration
uv run pytest -q tests/token_sprint/test_coordinator_protocol.py
uv run pytest -q tests/test_adaptive_config.py
```

Expected: PASS。

---

## Phase 3 — T-1 acceptance

### Task 12: 验收清单（全过才允许 W1–W8 执行）

打印并逐项打勾。任一项失败 = T-1 未完成，W 继续冻结。

- [ ] 非 fallback：`git rev-parse master` 等于记录的 `refactor_base_sha`。fallback：manifest 含冻结决议，且 W4/W5 的 `vector_math` 已从范围删除。
- [ ] canonical `sprint_manifest.json` 含 `refactor_base_sha`（或 fallback）与 `sprint_base_sha`；无自身 SHA 字段。
- [ ] `inputs_manifest.json`：`source_db_abs` 为存在的绝对路径；全部 W3 副本为 `copied` 或显式 `absent`。
- [ ] 跨调用预检在协调会话与一个 W worktree 各通过一次。
- [ ] `uv run pytest -q tests/token_sprint/test_coordinator_protocol.py` 通过，覆盖：缺失/非法 `--sprint-root`、两进程 nonce、四方哈希不匹配拒绝、重复 receipt 幂等、崩溃重放、Windows 安全 receipt 文件名。
- [ ] `sprint/integration` 冒烟：`uv run pytest -q tests/test_adaptive_config.py`；若基线含 `tests/test_pipeline_facade.py` 则一并绿。
- [ ] Cosmic Ray 决议写入 manifest；依赖已单独（Task 8）落在 `sprint/integration`；此后全冲刺依赖冻结。
- [ ] 所有 W 分支从 **Task 8 的** `sprint_base_sha` 创建（不要从 Task 10 的 usage-log commit 创建，除非已快进并更新了文档中的 SHA——默认不要）。

**Handoff 命令（仅验收后由协调会话执行）：**

```bash
SPRINT_BASE="<task-8-sha>"
git worktree add .worktrees/sprint-w1-enrichment -b sprint/w1-enrichment "$SPRINT_BASE"
git worktree add .worktrees/sprint-w2-eval-harness -b sprint/w2-eval-harness "$SPRINT_BASE"
git worktree add .worktrees/sprint-w3-data-audit -b sprint/w3-data-audit "$SPRINT_BASE"
git worktree add .worktrees/sprint-w4-mutation -b sprint/w4-mutation "$SPRINT_BASE"
git worktree add .worktrees/sprint-w5-property-tests -b sprint/w5-property-tests "$SPRINT_BASE"
git worktree add .worktrees/sprint-w7-code-audit -b sprint/w7-code-audit "$SPRINT_BASE"
git worktree add .worktrees/sprint-w8-shadow-fts -b sprint/w8-shadow-fts "$SPRINT_BASE"
# W6 默认不建。T+24h 红线未达且放大器 ①–④ 用尽时再：
# git worktree add .worktrees/sprint-w6-mypy -b sprint/w6-mypy "$SPRINT_BASE"
```

每个 worktree 作为独立 ZCode 工作区打开。合并只进 `sprint/integration`。合回 `master` 只在冲刺结束且用户确认后，由本会话执行。

---

## Verification matrix

| Gate | Command / evidence | Pass |
|------|--------------------|------|
| Protocol tests | `uv run pytest -q tests/token_sprint/test_coordinator_protocol.py` | 设计列出的用例全覆盖且绿 |
| Master 落地 | `git rev-parse master` | 等于 `refactor_base_sha`，或 fallback 决议已写入 |
| Unique baseline | `git -C .worktrees/sprint-integration rev-parse HEAD` | 等于 manifest `sprint_base_sha` |
| Inputs | `inputs_manifest.json` | `source_db_abs` 存在；可选源非静默 |
| Preflight | 两次独立进程，两个会话 | 均 `preflight ok` |
| Smoke | `pytest -q tests/test_adaptive_config.py` | 绿 |
| Dep freeze | `git log -1 -- pyproject.toml uv.lock` | 在 `sprint_base_sha` 或之前；之后无依赖 commit |
| Isolation | `git status --porcelain --untracked-files=all -- app/` in integration | 空（本会话不改 app/） |
| No GLM endpoint | `rg -i "bigmodel|GLM_API" app/ scripts/token_sprint/` | 无新命中 |

---

## Done looks like

- `sprint/integration` 存在且干净，HEAD 为唯一 `sprint_base_sha`。
- coordinator 三工具 + `protocol.py` + 协议测试在基线中。
- 共享根有合法 `sprint_manifest.json` 与 `inputs_manifest.json`。
- W 分支从该 SHA 创建的指令已发出；W1–W8 **仍未执行业务 Task**。
- Cosmic Ray 已裁定；W4 计划只走冻结路径。

---

## Explicit non-goals

- 执行任何 W1–W8 业务 Task。
- 把脏工作区或 `git add -A` 打成基线。
- 直连 GLM / 写入 API Key。
- 改重构 File Map 文件、改 models yaml、写生产库。
- 合回 `master`。
- 在 `sprint_base_sha` 之后再 `uv add`。
