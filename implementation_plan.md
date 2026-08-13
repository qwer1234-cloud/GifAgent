# GifAgent 响应式 UI 布局实施方案

## 背景

GifAgent 的 GUI 端基于 Gradio + pywebview 构建，默认窗口大小为 1400x900。
本次交付聚焦布局与样式的响应式调整：让 Review / Settings / Control / Search /
Collections 在较窄窗口下自动换行与收缩，并让 Timeline SVG 跟随容器宽度缩放。

本次实施**仅**涉及 Gradio UI 层的布局参数与 scoped CSS，不修改后端 API、
任务引擎、数据库、自适应管线、配置、README/Agent 文档或任何运行期数据。
所有既有组件变量、事件绑定、评分/快捷键、分页、刷新与 API 调用保持不变。

## 明确不做

- 不引入宽泛选择器（如 `.gradio-row`、`.gradio-dataframe`、通用 button、
  生成类名或裸元素选择器），只使用应用自有 `ga-` 前缀钩子。
- 不截断、缩短、替换或改写 Folder 值。
- 不移除全部 `min_width`：保留实用显式值（160/180），未显式指定的沿用
  Gradio 默认 320。
- 不做深色主题工作、Tab 重排、旧版 UI 重构，也不重设计 Today / Lab / Profile。
- 不把"强制重新构建 exe"作为本方案的交付项。
- 不在测试中硬编码测试总数断言。

## 变更清单

### 1. 新增 `app/ui/static/layout.css`

共享响应式 CSS，只包含 `ga-` 前缀的 scoped 选择器：

- 布局行钩子：`.ga-review-layout`、`.ga-settings-row`、`.ga-control-layout`
  （`flex-wrap: wrap` + `gap`，行换行交给 CSS，而不是 Row 参数）。
- 主从列钩子：`.ga-review-preview`、`.ga-review-controls`、
  `.ga-control-main`、`.ga-control-side`（最小宽度 `min(100%, 20rem)`）。
- Gallery 钩子：`.ga-review-gallery`、`.ga-search-gallery`
  （`min-height`/`max-height` + `overflow: auto`）。
- 选中预览：`.ga-selected-preview`（宽度 100%，限制高度，`overflow: hidden`）。
- 表格钩子：`.ga-control-table`、`.ga-collections-table`
  （局部 `overflow-x: auto`，`min-width: 0`）。
- 洞察区钩子：`.ga-collections-insight`（顶部间距）。
- `@media (max-width: 1100px)` 断点将主从列 `flex-basis` 设为 100%，
  实现窄窗口下的堆叠。

### 2. `app/ui/workbench.py`

- 新增小助手 `load_layout_css()`，以 UTF-8 读取
  `app/ui/static/layout.css`。
- `launch_kwargs()` 中 `css` 变为：
  `CONFIG_TOOLTIP_CSS + REVIEW_LAYOUT_CSS + load_layout_css()`，
  两段既有 CSS（配置提示、Review 布局）原样保留，共享 CSS 追加在后。
- `Blocks` 不传 `css_paths`，仍通过 `launch_kwargs` 的 `css` 键注入。

### 3. `app/ui/tabs/review.py`

- 主 `Row` 加 `ga-review-layout`；预览/Gallery 侧 `scale=2` +
  `ga-review-preview`，控制侧 `scale=3` + `ga-review-controls`。
- 候选 Gallery：`columns=None`（自动列数）、移除固定 `height=600`、
  加 `ga-review-gallery`；保留 `candidate-gallery` elem_id 与既有 CSS。
- 选中候选 `Image` 加 `ga-selected-preview`。
- 保留全部组件键、状态、事件、评分按钮、快捷键、分页与刷新行为。

### 4. `app/ui/tabs/settings.py`

- 将现有配置区包裹为三段 Accordion，顺序固定：
  1. `LLM`（open）
  2. `VLM and Adaptive`（`open=False`）
  3. `Preference Memory`（open）
- 外层 `Row` 加 `ga-settings-layout`，两个配置列加 `ga-settings-group`，
  字段行加 `ga-settings-row`。
- 保留实用 `min_width=160/180`；未显式指定的列沿用 Gradio 默认 320。
- 保留 `CONFIG_FIELD_KEYS`、每个组件变量、保存输入顺序、加载输出顺序、
  处理器、帮助文案、`?` 工具提示、校验逻辑与 profile 行为。

### 5. `app/ui/tabs/control.py`

- 任务队列主 `Row` 加 `ga-control-layout`；主内容列 `scale=3` +
  `ga-control-main`，摘要列 `scale=1` + `ga-control-side`。
- 任务表 `Dataframe` 设 `wrap=True`，并加 `ga-control-table`。
- `_format_jobs` 中的 Folder 值保持完整路径原样输出，绝不截断/改写。
- 不向 `Row` 传 `wrap`；保留全部 API 调用、事件、定时器、日志、
  队列状态、start / cancel / retry 行为与组件返回键。

### 6. `app/ui/tabs/search.py`

- 结果 Gallery：`columns=None`、移除固定 `height=600`、加
  `ga-search-gallery`。
- 保留搜索、过滤、分页、状态、选择与时间线行为。

### 7. `app/ui/tabs/collections.py`

- 集合列表 HTML 容器加 `ga-collections-table`，使长表格在局部
  `overflow-x` 滚动，不撑破页面。
- 将 Taste Map 与 Narrative 内容放入 `Taste Map & Narrative` Accordion
  （`open=False`），Accordion 加 `ga-collections-insight`。
- 保留 CRUD、选择、刷新、事件与返回组件。

### 8. `app/ui/components/timeline.py`

- 逻辑宽度保持 800，高度保持现有 `timeline_height + 4`。
- SVG 改为 `viewBox="0 0 800 <height>"`、`width="100%"`、
  `preserveAspectRatio="xMidYMid meet"`，跟随容器宽度等比缩放。
- 保留时间线数据、ticks、分段、labels、GIF 预览、PotPlayer 提示与
  全部 JavaScript 行为。

### 9. `app/ui/launcher.py`

- `webview.create_window` 保持 `width=1400`、`height=900`，新增
  `min_size=(1024, 680)`，其余行为不变。

## 测试

聚焦测试命令（精确）：

```text
uv run pytest -q tests/test_launcher_gradio_options.py tests/test_config_help_annotations.py tests/test_candidate_review_layout.py tests/test_workbench_structure.py tests/test_control_task_api.py tests/test_quality_lab_ui.py tests/test_timeline.py
```

随后执行：

```text
git diff --check
```

两者都必须通过。

## 验收标准

- 首个业务编辑为 `app/ui/static/layout.css`。
- 只引入应用自有 `ga-` 前缀的 scoped 选择器。
- 共享 CSS 通过 `launch_kwargs` 的 `css` 拼接加载，既有内联 CSS 保持不变。
- Review / Settings / Control / Search / Collections、timeline 与 launcher
  均已按上述方案更新。
- Folder 值完整保留，所有事件接线不变。
- 无不受支持的 Gradio 参数（无 `css_paths`、无 Row `wrap`）。
