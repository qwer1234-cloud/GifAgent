# Task 0 失败测试摘要(2026-09-12)

命令:`.venv/Scripts/python.exe -m pytest tests/test_candidate_capture_ingest.py tests/test_curve_source_features.py tests/test_curve_online.py tests/test_curve_training_snapshot.py tests/test_learning_jobs.py tests/test_curve_deployment.py -q`

结果:`12 failed, 26 passed` —— 12 个新增回归全部在 HEAD `d08049a2` + 当前工作树上失败,26 个既有测试保持通过。

## 失败与 R1–R9 对应关系

| 缺陷 | 测试 | 失败模式 |
|---|---|---|
| R1 | `test_learning_jobs.py::test_missing_snapshot_never_completes_learning_job` | `TypeError: run_learning_once() got an unexpected keyword argument 'now_epoch'`(目标整数 epoch 接口不存在) |
| R1 | `test_learning_jobs.py::test_missing_snapshot_never_completes_learning_job_legacy_clock` | `AssertionError: assert 'SUCCEEDED' == 'NEEDS_ATTENTION'` —— 无快照时 worker 空成功,证明阶段从未执行 |
| R4 | `test_curve_online.py::test_curve_online_uses_shared_export_scorer` | `AssertionError: assert []` —— 共享 scorer 从未被调用;当前实现用私有混合公式算出 0.9*80+0.1*1.0=72.1 |
| R5 | `test_curve_deployment.py::test_activate_deployment_revalidates_qualification_bundle` | `TypeError: unexpected keyword argument 'qualification_dir'` —— 激活入口没有资格包/运行时配置校验路径,profile hash 被当作 config hash 且 `config_matches=True` 硬编码 |
| R6 | `test_curve_training_snapshot.py::test_manifest_rows_must_match_declared_source_roles` | `AssertionError: assert 'source_role_mismatch' in []` —— 验证器只比较声明集合,不检查实际行 |
| R7 | `test_learning_jobs.py::test_claim_is_atomic_between_two_connections` | `ImportError: cannot import name 'claim_learning_job'` —— 不存在原子 CAS 领取 |
| R7 | `test_learning_jobs.py::test_run_learning_once_respects_live_epoch_lease` | `AssertionError: assert 'SUCCEEDED' == 'IDLE'` —— epoch 格式租约被 ISO 字符串比较抢占('1800000500' < '2026-…' 为 True) |
| R8 | `test_candidate_capture_ingest.py::test_capture_versions_do_not_overwrite_old_payload` | `AttributeError: 'str' object has no attribute 'payload_path'`(目标 `PersistedCapture` 返回类型不存在) |
| R8 | `test_candidate_capture_ingest.py::test_capture_version_conflict_preserves_first_row_payload` | `AssertionError: 文件 hash 与首行记录不符` —— capture-v2 覆盖了 capture-v1 指向的同一 JSON 文件 |
| R8 | `test_candidate_capture_ingest.py::test_same_capture_identity_with_different_content_conflicts` | `ImportError: cannot import name 'CaptureConflict'` —— 同身份不同内容静默 UPDATE 而非冲突 |
| R9 | `test_curve_source_features.py::test_available_features_cannot_be_synthesized_from_missing_values` | `Failed: DID NOT RAISE ValueError` —— 缺失特征被补 0.0 冒充 AVAILABLE |
| R9 | `test_curve_source_features.py::test_extractor_identity_is_required_on_both_sides` | `AssertionError: assert 'extractor_sha256_missing' in []` —— 单侧缺失 extractor 身份仍判兼容 |

R2(persist/extract/score 无生产调用方)与 R3(提取固定返回 unavailable)需要 Task 3 的共享 pipeline 集成测试和 Task 2 的外置运行时才能暴露,按计划 Step 5 的最小清单不在本批次内。
