# GifAgent 前端重构方案 — 排版自适应与布局修复

## 背景

GifAgent 的 GUI 端基于 Gradio + pywebview 构建，默认窗口大小为 1400×900。当前 UI 存在以下排版/自适应问题：

1. **Review 页左右列比例失衡** — 左侧 Gallery（`scale=1`）与右侧操作区（`scale=3`）比例在不同窗口尺寸下表现差异大；在较窄窗口中左列被过度压缩
2. **Settings 页内容过长溢出** — 28 个配置字段全部平铺在两栏中，在小窗口下需要大量滚动，且 `min_width` 硬编码导致不同屏幕上组件被截断
3. **Control 页 Job Table 横向溢出** — 7 列 Dataframe 在窄窗口下无法完整显示
4. **Today/Search/Collections/Lab/Profile 各页缺少一致的间距和分组视觉层次** — 使用的是 Gradio 默认样式，内容区域没有明确的视觉分隔
5. **Timeline SVG 固定 800px 宽度** — 不跟随容器宽度缩放
6. **Gallery 图片容器高度固定 600px** — 内容少时大面积留白，内容多时不够用
7. **全局缺少自定义主题 / CSS** — 仅有少量内联 CSS（`REVIEW_LAYOUT_CSS` + `CONFIG_TOOLTIP_CSS`），没有系统性的响应式断点或 CSS 变量
8. **pywebview 窗口 `width=1400, height=900`** 写死，未对高 DPI 或更小屏幕做适配

## 用户需求

> 至少页面的排版正常，能够随页面的扩展而自适应

## 重构范围

> **重要**：本次重构**仅涉及 Gradio UI 层的布局和 CSS**，不改变任何后端 API、业务逻辑、数据流或 Tab 事件绑定。所有现有功能保持不变。

---

## Proposed Changes

### 1. 全局样式基础设施

#### [NEW] `app/ui/static/layout.css`

创建统一的 CSS 文件，提供：

- **CSS 变量** — 间距、颜色、字号、圆角的统一 token
- **响应式断点** — 通过 `@media` 查询在 `< 1024px`、`< 768px` 时自动调整列布局为堆叠
- **Gallery 自适应高度** — `min-height` + `max-height` 替代固定 `height=600`
- **Dataframe 横向滚动** — `overflow-x: auto` 防止表格溢出
- **Timeline SVG 宽度** — `width: 100%; max-width: 100%` 响应式

```css
/* 核心 CSS 变量 */
:root {
    --ga-gap-sm: 0.5rem;
    --ga-gap-md: 1rem;
    --ga-gap-lg: 1.5rem;
    --ga-radius: 8px;
    --ga-gallery-min-h: 300px;
    --ga-gallery-max-h: 70vh;
}

/* 响应式断点 */
@media (max-width: 1024px) {
    .gradio-row { flex-direction: column !important; }
}

/* Gallery 自适应 */
#candidate-gallery { min-height: var(--ga-gallery-min-h); max-height: var(--ga-gallery-max-h); }

/* Dataframe 横向可滚动 */
.gradio-dataframe { overflow-x: auto; }

/* Timeline SVG 自适应 */
[id^="timeline-"] svg { width: 100% !important; max-width: 100%; height: auto; }
```

---

### 2. Review Tab 布局修复

#### [MODIFY] `app/ui/tabs/review.py`

**当前问题**：
- 左栏 `scale=1`，右栏 `scale=3`，导致左侧 Gallery 在窄窗口被严重压缩
- Gallery 高度固定 `600px`

**修改方案**：
- 将左右栏比例调整为 `scale=2 : scale=3`，给予 Gallery 更多空间
- Gallery `height` 从固定 `600` 改为 `"auto"`，由容器和 CSS 控制自适应
- GIF 预览区域 `min-height` 从硬编码 `340px`/`300px` 改为相对单位

**CSS 修改** (`REVIEW_LAYOUT_CSS`)：
```diff
-#selected-gif-preview {
-    display: flex !important;
-    align-items: center;
-    justify-content: center;
-    width: 100%;
-    min-height: 340px;
-}
+#selected-gif-preview {
+    display: flex !important;
+    align-items: center;
+    justify-content: center;
+    width: 100%;
+    min-height: 240px;
+    max-height: 50vh;
+}
```

---

### 3. Settings Tab 分组折叠

#### [MODIFY] `app/ui/tabs/settings.py`

**当前问题**：
- 28 个配置字段全部展开平铺，垂直空间占用过大
- `min_width=160` / `min_width=180` 硬编码，小窗口下可能溢出

**修改方案**：
- 将 3 个配置组（LLM / VLM+Adaptive / Preference Memory）包裹在 `gr.Accordion` 中，默认折叠 VLM+Adaptive 部分
- 去掉 `min_width` 硬编码，改用 Gradio 的 `scale` 属性自适应

---

### 4. Control Tab 表格自适应

#### [MODIFY] `app/ui/tabs/control.py`

**当前问题**：
- Job Table 7 列在窄窗口下溢出
- Job ID 和 Folder 列内容可能很长

**修改方案**：
- 给 Dataframe 添加 `wrap=True` 使长内容换行
- 在 `_format_jobs` 中截断 Folder 路径显示（仅保留末尾目录名）
- 将 Job table 和 summary 的 Row 比例从 `2:1` 调整为 `3:1`

---

### 5. Timeline SVG 自适应宽度

#### [MODIFY] `app/ui/components/timeline.py`

**当前问题**：
- SVG `width` 硬编码为 `800px`，不随容器变化

**修改方案**：
- SVG 改用 `viewBox` + 百分比宽度，使其响应式缩放：
```diff
-    svg = f"""<svg width="{timeline_width}" height="{timeline_height + 4}"
+    svg = f"""<svg viewBox="0 0 {timeline_width} {timeline_height + 4}"
+         width="100%" preserveAspectRatio="xMidYMid meet"
```

---

### 6. Search Tab 布局优化

#### [MODIFY] `app/ui/tabs/search.py`

**修改方案**：
- Gallery `height` 从固定 `600` 改为 `"auto"`
- 为 Gallery 添加 `elem_id="search-gallery"` 以便 CSS 控制

---

### 7. Collections Tab 表格和间距

#### [MODIFY] `app/ui/tabs/collections.py`

**修改方案**：
- HTML 表格添加 `overflow-x: auto` 包裹容器
- Taste Map / Narrative 区域用 `gr.Accordion` 折叠，减少页面初始高度

---

### 8. Workbench Shell CSS 集成

#### [MODIFY] `app/ui/workbench.py`

**修改方案**：
- 在 `launch_kwargs()` 中加载新的 `layout.css` 文件内容
- 将 CSS 合并到现有的 `css` 参数中

---

### 9. Launcher 窗口自适应

#### [MODIFY] `app/ui/launcher.py`

**修改方案**：
- pywebview 窗口设置 `min_size=(1024, 680)` 最小尺寸
- 保持默认 1400×900 不变，但允许用户自由缩放

---

## 文件变更汇总

| 操作 | 文件 | 变更说明 |
|------|------|---------|
| **NEW** | `app/ui/static/layout.css` | 全局响应式 CSS：变量、断点、Gallery/Dataframe/Timeline 自适应 |
| MODIFY | `app/ui/workbench.py` | 加载并注入 `layout.css` |
| MODIFY | `app/ui/tabs/review.py` | 列比例 `2:3`、Gallery 自适应高度、CSS 调整 |
| MODIFY | `app/ui/tabs/settings.py` | 配置分组折叠、去掉 `min_width` 硬编码 |
| MODIFY | `app/ui/tabs/control.py` | 表格截断、比例调整 |
| MODIFY | `app/ui/tabs/search.py` | Gallery 自适应、`elem_id` |
| MODIFY | `app/ui/tabs/collections.py` | 表格 `overflow-x`、折叠区域 |
| MODIFY | `app/ui/components/timeline.py` | SVG 响应式（`viewBox` + 百分比宽度） |
| MODIFY | `app/ui/launcher.py` | 添加 `min_size` 约束 |

> **注意**：所有修改不影响现有事件绑定逻辑和后端 API，仅调整布局参数和样式。修改后需要重新构建 exe：`uv run pyinstaller --noconfirm build_exe.spec`。

## Open Questions

1. **是否需要深色主题**？当前使用 `gr.themes.Soft()`（浅色主题）。如果需要跟随 pywebview 的系统主题或强制暗色，需要额外调整。
2. **Tab 顺序是否需要调整**？当前 7 个 Tab 全部水平排列，窗口较窄时 Tab 名称可能被截断。是否考虑将不常用的 Tab（实验室、合集）放入"更多"子菜单？
3. **是否需要保留 `legacy_candidate_review.py`（74KB 旧版 UI）**？它已被 workbench 替代，但通过环境变量 `GIFAGENT_LEGACY_QUEUE_UI` 仍可启用。重构是否完全忽略它？

## Verification Plan

### Automated Tests
```bash
uv run pytest tests/ -v
```
现有 400+ 测试验证后端逻辑不受影响。

### Manual Verification
1. **Web 模式**：`uv run python app/ui/candidate_review.py` 后在浏览器中 1920px / 1366px / 1024px / 768px 宽度下检查各 Tab 布局
2. **GUI 模式**：重新构建 exe 后拖拽窗口大小验证自适应
3. **各 Tab 排版检查清单**：
   - [ ] Review：Gallery + 预览区在各窗口尺寸下不溢出、不留大面积空白
   - [ ] Settings：配置项在窄窗口下不截断、可折叠
   - [ ] Control：Job Table 可横向滚动、不溢出
   - [ ] Search：搜索结果 Gallery 自适应
   - [ ] Collections：HTML 表格不溢出
   - [ ] Timeline：SVG 跟随容器宽度
