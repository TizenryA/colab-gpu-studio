# 前端黑白极简重写（ui-bw-rewrite）

只改前端（`web/`），后端接口、`web/src/lib/gpu-api.ts` 的 API 客户端均未改动。未部署、未推送。

## 预览
```bash
cd web && npm install && npm run dev        # 开发预览（需后端）
# 或离线演示（假数据，不连 Google）：
cd web && npm run build && cd .. && uv run python scripts/offline_server.py --port 18788
# 打开 http://127.0.0.1:18788 ，演示密码见终端输出
```
验证：`cd web && npm run build && npm run test:contract`

## 改动文件
- 样式：`src/styles.css`（黑白色板 + 全局组件）、`src/gpu.module.css`（页面样式，整体重写）；删除 `src/styles/offline.css`、`src/components/gpu-param-form.module.css`；`index.html` theme-color、`src/main.tsx` 默认深色。
- 新增：`src/components/ui.tsx`（Card / InfoTip 说明小提示 / Segmented / Pager）、`src/lib/labels.ts`（技术参数 → 中文短名、模式与策略说明）。
- 重写：`App.tsx`（外壳、底部导航 创作/任务/作品/算力、弹层）、`pages/CreatePage.tsx`、`JobsPage.tsx`、`GalleryPage.tsx`、`ComputePage.tsx`、`SettingsPage.tsx`、`WorkflowManager.tsx`、`components/GpuParamForm.tsx`、`ReferencePicker.tsx`、`ComputeSpecs.tsx`、`lib/ui-helpers.ts`（工作流状态文案、CU 数字）。

## 对应的 6 个问题
1. 比例选择：3 列芯片（带比例小图 + 宽×高），宽高输入只在「自定义」下出现；详细参数折叠为「高级参数 N 项」。
2. 尺寸只有一套（CreatePage 的「画面尺寸」），GpuParamForm 不再渲染尺寸，选择框不再跳动。
3. 工作流管理：按 workflow_key 分组，同模型多版本折叠，分页（每页 6 组），每组默认显示 4 个版本。
4. 作品缩略图：`object-fit: cover`，1:1 裁切，无白边；点选后底部浮条 预览/再次生成/删除（删除需二次点击）。
5. 配色：纯黑/白/灰，无橄榄绿；默认深色，设置里可选浅色/跟随系统。
6. 文案：全部中文短词；模式（节省/批次/智能）、模拟 vs Colab、生成模式等有 ⓘ 说明。

## 已知注意
- `MODE_HELP`（fast/hq/refine 等）的说明文字是按常见含义写的，请对照真实工作流确认。
- 离线 mock 没有视频/带宽高参数的示例，视频尺寸只用真实 manifest 里 width/height 校验过逻辑，未在真机看过。
- 截图在 390px 宽 Chromium 下检查过；未在真实 iOS Safari 上测。
