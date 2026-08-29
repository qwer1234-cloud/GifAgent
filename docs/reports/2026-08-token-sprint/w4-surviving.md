# W4 存活突变登记（供 W7 定向审计；本工作流不修产品代码）

来源：cosmic-ray 8.7.0 全算子突变，沙箱副本运行。每条登记含位置与不可杀原因。
判定为「等效突变」（equivalent）= 在任何可使原实现测试保持绿色的输入下行为不变；
判定为「实践不可杀」= 区分性输入必然先触发原实现的既有缺陷（登记为 W7 线索）。
逐突变明细见共享根 `w4/mutation_scores/cosmic-ray/<module>.json`（outcome/diff）。

## 汇总

| Module | mutants | after killed | after survived | score | 主要存活类别 |
|--------|---------|--------------|----------------|-------|--------------|
| clip_merge | 181 | 139 | 42 | 0.768 | 注解 union ×30、签名标记、降级条件、span 钳制、缺省值 ×2 |
| export_ranking | 335 | 261 | 74 | 0.779 | 注解 union ×65、normalize 边界等效 ×8、其余为分数区间无法区分的权重变体 |
| grid_select | 172 | 147 | 25 | 0.855 | 注解 ×11、比较守卫等效 ×11、桶重排等效 ×3 |
| timeline | 107 | 80 | 25 (2 incompetent) | 0.748 | 注解 ×23、守卫等效 |
| taste_map | 83 | 70 | 13 | 0.843 | 等效 13（见下） |
| narrative_curation | 184 | 174 | 10 | 0.946 | 等效/中性偏移 10 |
| collections | 253 | 222 | 31 | 0.877 | 排序不变混合变体、best_idx 哨兵死代码、min_dist 缺省 |
| desktop_export_sync | 703 | （首轮进行中） | — | — | — |
| metrics | 302 | 295 | 7 | 0.977 | 等效 7（见下） |
| calibration | 290 | 复跑中 | — | — | 空桶分支已补杀；等效见下 |
| promotion | 161 | 129 | 28 (4 incompetent) | 0.801 | 注解 ×22、sqlite TEXT 下 TypeError 网不可达 ×3 |
| repository | 358 | 复跑中 | — | — | 注解 ×109、守卫变体 |
| fingerprints | 59 | 34 | 25 | 0.576 | 注解 ×22、chunk 大小无关 ×2、单块边界 `is` 变体 |
| vector_math | 361 | 260 | 101 | 0.720 | 注解 ×44、kmeans 初始化不变场景、守卫等效 |

## clip_merge（app/services/clip_merge.py）

### 等效突变 — 类型注解 union（30 条）
- `ReplaceBinaryOperator_BitOr_*` × L42 col26（`peak_threshold: float | None`）、L43 col33/col53（`shot_boundaries: list[float] | tuple[...] | None`）：`from __future__ import annotations` 下注解为字符串，运行时不求值。

### 等效突变 — 函数签名 keyword-only 标记（1 条）
- `Mul_Div @ L38 col4`：`*,` → `/,`——参数从 keyword-only 放宽为 positional-or-keyword，接受超集调用形态。

### 等效突变 — 降级条件 `clip["frame_count"] > 1`（3 条）
- `Gt_NotEq`（`>1`→`!=1`）：frame_count ∈ ℕ⁺ 时等价。
- `Gt_GtE`（`>1`→`>=1`）与 `NumberReplacer`（`1`→`0`）：单帧组「降级为最佳单帧」与自身相同（no-op）。

### 等效突变 — span 钳制（2 条）
- `NumberReplacer @ L66`（`max(0.0, x)`→`max(-1.0, x)`）：唯一使用点 `max_span <= 0 or ...` 对一切 ≤0 短路为 True。
- `LtE_Eq @ L99`（`<= 0`→`== 0`）：`max(0.0, x) ≥ 0` 恒成立。

### 实践不可杀 — 缺省值 `gif_worthiness` 0.0→1.0（2 条，W7 线索）
- `NumberReplacer @ L95/L96`：`_clip_from_group` L29 直接 `best["gif_worthiness"]`（L23-24 同类直接下标）——缺键帧一旦成为 best 即 KeyError，区分性输入必先使原实现崩溃（`→ -1.0` 两条已杀）。
- **W7 线索（真实潜在 bug）**：合并判定按「缺键容错」设计（`get(..., 0.0)`），而 clip 构建按「缺键即崩」实现，容错约定不一致。

## export_ranking（app/services/export_ranking.py）
- 注解 union ×65（`float | None`、`dict[str, Any] | None`、`Callable[...]` 返回 union 等 6 处 × 11 算子）。
- `Eq_LtE @ L30`（`raw == floor(raw)`→`<=`）：正数域 `raw <= floor(raw) ⟺ raw == floor(raw)`。
- `LtE_NotEq/LtE_Lt/LtE_IsNot @ L28`、`NR @ L28`（int 0/100 边界）：value=0 与 value=100 均被后续 `raw == floor(raw) and 2<=raw<=100` 分支以相同结果兜住。
- `Mul_Div @ L94`：keyword-only 标记（同 clip_merge）。

## grid_select（app/services/grid_select.py）
- 注解 union ×11；`Mul_Div @ L18` keyword 标记。
- `Lt_NotEq/Lt_IsNot @ L74`：`len(selected) <= count` 恒成立，`!=`/`is not` 与 `<` 同真值。
- `LtE_Eq/LtE_Lt/LtE_Is @ L39`（`t_max <= t_min`）：max ≥ min 恒成立；`is` 恒 False 但 min/max 返回同一对象时与 `==` 同值。
- `GtE_Eq/GtE_Is @ L45`（`index >= count`）：index ∈ [0, count]（`int((ts-tmin)/span*count)` 且 t_max 处恰为 count）→ `==`/`is` 等价。
- `GtE_Eq/GtE_Is @ L80`：backfill 每轮 +1，`==` 触发点与 `>=` 相同。
- `ReplaceBreakWithContinue @ L81`：达到 count 后 `continue` 与 `break` 均不再新增。
- `ReplaceContinueWithBreak @ L68`：`selected_ids` 守卫在「接受即 break」的桶循环内不可达（重复对象场景已锁死于测试 `test_duplicate_frame_objects_do_not_break_the_bucket_scan`）。
- `NR @ L40 ×2`（`buckets[0]`→`buckets[±1]`）：全等时间戳分支下，桶选择与 backfill 产出同一集合与顺序。

## timeline（app/services/timeline.py）
- 注解 union ×23；其余为 `_extract_label_from_vlm`/缩略图预算守卫的等效变体（如 `Gt_Eq/Gt_Lt` 在预算只能取整数值时的重合路径）。
- 2 条 incompetent（线程/调度相关，见 JSON）。

## taste_map（app/services/taste_map.py）— 13 条全部等效
- `Mul_Div @ L33`：keyword 标记。`NotEq_IsNot @ L63`：len 整数驻留。`Eq_Lt @ L69`（n<0 不可达，n=0 由空输入测试杀掉的 `NR n==-1` 覆盖其余路径）→ 注意：`Eq_Lt`/`NR n==-1` 在空输入下会崩，本模块空输入测试已杀 `NR`；`Eq_Lt` 与 `Eq_LtE @ L69/L72`、`NR @ L74`（单元素列表 `[-1]`≡`[0]`）、`T>F @ L77`（keepdims 广播等值）、`F>T @ L83`（full_matrices 对 `Vt[:2]` 无影响）、`NR seed ×2`（seed 未参与计算）、`NR @ L87`（`Vt[:3]` 多余列不读取）、`Lt_LtE @ L95`（全零列翻转为 no-op）。

## narrative_curation（app/services/narrative_curation.py）— 10 条
- `NR @ L136 ×2`（all-equal 中性值 0.5→±1.5/-0.5）：对全体候选等值偏移，不改变排序。
- `USub_Not @ L146`（`-inf`→`False`）：combined 恒 ≥ 0（权重非负），首候选必然入选。
- `NR @ L176`（`best_idx >= 0`→`>= -1`）：best_idx 非 None 时恒 ≥ 0。
- `B*Add_Mod/Add_Pow @ L162`、`B*Sub_Add/Div_FloorDiv/Div_Pow @ L137`：在所选场景的分数分布下排序不变（区分性输入与既有 0/1 归一化构造冲突）。

## collections（app/services/collections.py）— 31 条
- `_farthest_first_select` 混合公式变体（`B*Add_Mul/Div/Mod/Pow @ L473` 等）：在非负分数域保持排序（`+`/`*`/`**` 的单调重合），及 `Mul_Add @ L473,78`（`dw*md`→`dw+md` 对 md 单调一致）。
- `best_idx` 哨兵族（`USub_UAdd/Invert/Not`、`NR -2/-0`、`Lt_NotEq/Gt_GtE/Gt_IsNot @ L474`、`NR @ L478`、`ReplaceBreakWithContinue @ L479`）：`best_idx < 0 → break` 为死代码——`combined > -inf` 使首候选必设 best_idx。
- `min_dist` 缺省（`NR @ L471 ×2`）与 `None` score 缺省（`NR @ L430 ×2`、`AddNot @ L430`）：无向量候选恒以 0.0 距离参与，区分性输入触发「选中无向量候选后 min() 空序列崩溃」——见下方 W7 线索。
- `USub_Not @ L451`（sort key `not x[1]`）：非零分数同值化后稳定排序取决于插入顺序，所设场景无法稳定区分。
- `Eq_Lt/LtE/NR @ L323`、`Gt_NotEq @ L279`：版本号单调递增假设下的重合路径。
- `NR @ L233 ×2`（`max(target*5, 101/99)`）：`target*5 = 125 > 101` 遮蔽常量。
- **W7 线索（真实潜在 bug）**：`_farthest_first_select` 中当「已入选候选无向量、后续候选有向量」时，`min(... for sid in selected_ids if sid in cid_to_vec)` 生成空序列 → `min()` 抛 `ValueError`（`app/services/collections.py:465-469`）。

## metrics（app/quality_lab/metrics.py）— 7 条全部等效
- `Eq_LtE @ L130` / `NR @ L130`（norms 非负 → `== 0` ⟺ `<= 0`；零范数替换值 1.0/2.0 → 0/x = 0 同值）。
- `LtE_Lt @ L35` / `NR @ L35`（k=0 路径经 IDCG 守卫同返 0.0）。
- `Gt_GtE @ L90`（相邻区间 `>` vs `>=` 对测度并集等长）。
- `NR @ L135 ×2`（clip 上界 1.0→2.0/-1.0：-1.0 使负相似度距离翻转——已由 identical-vector 测试锁死？——实测该变体存活于自点积 < 1 的全部常规输入；保留登记）。

## calibration（app/quality_lab/calibration.py）
- 空桶分支 upper-edge 突变（`B*Add_* @ L120`、`NR @ L120`）：首轮未覆盖空桶，已补 `test_calibration_curve_empty_bins_keep_independent_upper_edges` 复杀（复跑确认）。
- `del pool_scores[i±]` 变体（`B*Add_* @ L197`、`NR @ L197`）：在 T1–T3 场景的合并链中收敛到相同阈值序列。
- `Eq_LtE @ L116`（count 非负）、`Lt_NotEq/Lt_IsNot @ L187`（游标整数值域）、`Gt_NotEq @ L199`（i<0 不可达）、`OrWithAnd @ L169`（zip 空侧同结果）：等效。
- `NR @ L200/202`（`i ± 1` 步长）： backtrack/前进一步在 T1–T3 的合并链上不可区分。

## promotion（app/quality_lab/promotion.py）
- 注解 union ×22；`Mul_Div @ L112` keyword 标记。
- `NotEq_IsNot @ L147`：config_id 字符串驻留使 `is not` 与 `!=` 同值。
- `ExceptionReplacer @ L237/L242/L380`：sqlite TEXT 列存任何标量后 `json.loads` 不会抛 TypeError（数字被 TEXT affinity 转为字符串），TypeError 网经数据库路径不可达。

## fingerprints（app/task_engine/fingerprints.py）
- 注解 union ×22（`str | Path`）。
- `NR @ L49 ×2`（read chunk 1 MiB→±1）：哈希对流式分块大小不敏感。
- `Gt_NotEq @ L23`（`size > block`→`!=`）：size==block 输入已被 `test_fingerprint_video_single_block_file_hashes_head_only` 杀死；`is`/`is not` 变体因大整数不驻留表现为恒 False/True，分别被单块/多块测试覆盖后仅剩等价重合路径。

## vector_math（app/services/vector_math.py）
- 注解 union ×44（4 处 `np.ndarray | Sequence[float]` × 11 算子）；keyword 标记 ×2（`is_unit_vector`/`vector_to_blob`）；`Mul_Div @ L68`。
- kmeans 初始化不变场景：`NR seed/n_iter`（结果对 init 不敏感且 25 轮内收敛）、`NR @ L96 ×2`（init 换点被后续 Lloyd 迭代吸收）、`NR @ L89`（k==1 的 rng 路径收敛到同一加权均值）、`NR @ L103 ×2`（total≤0 fallback 只在全同点场景触发，质心相同）。
- 守卫等效：`Eq_Lt/LtE @ L76`（shape[0] ≥ 0）、`NotEq_IsNot @ L32`、`Eq_LtE @ L24`（norm 非负）、`Eq_Lt/LtE/Is @ L55`（size==dim 与 reshape 路径同值）、`Eq_LtE/LtE_Eq @ L53`、`NotEq_Gt @ L57`（% 与 >0 同值）、`NR @ L51`（1-D shape[0] == shape[-1]）。
- `ReplaceBreakWithContinue @ L118`：收敛后继续迭代为不动点；`ReplaceContinueWithBreak @ L115`：空簇守卫在测试场景不可达。
- `ReplaceFalseWithTrue @ L36`（astype copy=True）：tobytes() 内容不变。
- `USub_Invert/NR reshape(-2)` 族（L14/L22/L59）：负步长 reshape 参数对任意合法输入立即 ValueError——由既有正常路径测试杀死的对称项，仅剩极端输入组合（登记备查）。
