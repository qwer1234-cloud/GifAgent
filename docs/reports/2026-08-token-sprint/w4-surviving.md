# W4 存活突变登记（供 W7 定向审计；本工作流不修产品代码）

来源：cosmic-ray 8.7.0 全算子突变，沙箱副本运行。每条登记含位置与不可杀原因。
判定为「等效突变」（equivalent）= 在任何可使原实现测试保持绿色的输入下行为不变；
判定为「实践不可杀」= 区分性输入必然先触发原实现的潜在缺陷（登记为 W7 线索）。

## clip_merge（app/services/clip_merge.py，181 突变 / 存活 42 / score 0.768）

### 等效突变 — 类型注解 union（33 条，全部不可执行路径）

- `core/ReplaceBinaryOperator_BitOr_{Add,Sub,Mul,Div,FloorDiv,Mod,Pow,RShift,LShift,BitAnd,BitXor}` × 3 处：
  - L42 col26：`peak_threshold: float | None = None` 的 `|`
  - L43 col33、L43 col53：`shot_boundaries: list[float] | tuple[float, ...] | None = None` 的两个 `|`
  - 原因：模块顶部 `from __future__ import annotations`，注解为字符串，运行时不求值；把 `|` 换成任何算术/位运算符都不影响行为。

### 等效突变 — 函数签名 keyword-only 标记（1 条）

- `core/ReplaceBinaryOperator_Mul_Div @ L38 col4`：签名中的 `*,` → `/,`。
  原因：`*` → `/` 使参数从 keyword-only 变为 positional-or-keyword，接受超集调用形态；既有调用全部传关键字，行为不变。

### 等效突变 — 降级条件 `clip["frame_count"] > 1`（4 条）

- `core/ReplaceComparisonOperator_Gt_NotEq @ L82 col36`（`> 1` → `!= 1`）：frame_count ∈ ℕ⁺，`>1 ⟺ !=1`。
- `core/ReplaceComparisonOperator_Gt_GtE @ L82 col36`（`> 1` → `>= 1`）：单帧组「降级为最佳单帧」与自身相同，no-op。
- `core/NumberReplacer @ L82 col38`（`1` → `2`）：已杀（2 帧组必须降级）。
- `core/NumberReplacer @ L82 col38`（`1` → `0`）：单帧组降级 no-op（同上）。

### 等效突变 — span 钳制（2 条）

- `core/NumberReplacer @ L66 col19`（`max(0.0, x)` → `max(-1.0, x)`）：下游唯一使用点 `max_span <= 0 or ...` 对一切 ≤0 的 max_span 均短路为 True。
- `core/ReplaceComparisonOperator_LtE_Eq @ L99 col31`（`max_span <= 0` → `== 0`）：`max_span = max(0.0, x) ≥ 0`，`<= 0 ⟺ == 0`。

### 实践不可杀 — 缺省值 `gif_worthiness` 缺省 0.0 → 1.0（2 条，W7 线索）

- `core/NumberReplacer @ L95 col46`（frame 缺省 0.0 → 1.0）
- `core/NumberReplacer @ L96 col49`（prev 缺省 0.0 → 1.0）
- 原因：`_clip_from_group` L29 直接 `best["gif_worthiness"]` 取键——任何缺 `gif_worthiness` 键的帧一旦自成一组的 best，原实现即抛 KeyError。
  因此「缺省值参与合并判定」的一切区分性输入都会先让原实现崩溃，无法构造原实现为绿的杀伤测试
  （缺省 → -1.0 的两条对应突变已由 `test_missing_frame_worthiness_default_at_zero_threshold` 杀死）。
- **W7 线索（真实潜在 bug）**：`app/services/clip_merge.py:29` 对 `best["gif_worthiness"]`、L31-32 对 `best["timestamp"]`、L23-24 对 `group[0/−1]["timestamp"]` 使用直接下标访问，帧缺键即崩溃；而合并判定处（L95-96）却按「缺键容错」设计。两处容错约定不一致。

（其余模块随 Task 4+ 增量追加。）
