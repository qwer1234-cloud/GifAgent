# Config 参数注解设计

## 目标

为 GUI 的 Config 页面中每一个可编辑参数增加中文注解。参数标签旁显示统一的 `?` 标识，鼠标悬停时展示该参数的用途、单位或取值含义，帮助用户在不离开页面的情况下理解配置。

## 范围

- 修改 `app/ui/candidate_review.py` 的 Config 页面。
- 覆盖当前页面展示的 20 个配置参数：LLM 7 个、VLM 2 个、自适应采样 10 个、Preference Memory 1 个。
- 保持现有 `load_config`、`save_config` 的字段顺序、类型转换和 YAML 其他 section 保留行为不变。
- 使用当前 Gradio 版本可兼容的标签/提示能力，不新增运行时依赖。

## 交互设计

每个参数使用统一的标签格式，例如 `temperature ?`。`?` 作为帮助标识，鼠标悬停时显示中文 tooltip；说明内容包含参数作用，必要时包含单位、范围、默认行为或与其他参数的关系。

注解集中维护在一个字段名到中文说明的映射中，并由一个小型辅助函数生成标签和提示参数，避免 20 个组件的文案和交互不一致。

## 数据与历史记录

配置保存仍写入 `configs/models.yaml`，不覆盖 YAML 中 GUI 未展示的配置项。构建 GUI 时继续使用已有 `scripts/rebuild_exe.sh`：先备份 `dist/GifAgentUI/data/`，重建可执行文件后恢复数据目录。这样保留数据库、导出物、索引、检查点及历史反馈记录。

## 测试与验收

- 新增测试验证 Config 页面字段清单与注解映射一一对应，且每条中文注解非空。
- 先运行测试确认新增测试因注解映射/辅助函数不存在而失败，再实现最小改动并验证通过。
- 运行完整 `uv run pytest tests/ -v`。
- 使用 `bash scripts/rebuild_exe.sh` 构建，并验证 `dist/GifAgentUI/GifAgentUI.exe` 存在且 `dist/GifAgentUI/data/` 被保留。
