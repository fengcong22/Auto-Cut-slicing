# AI 剪辑会话启动提示

两种入口的启动提示。`<…>` 为启动方填写的值。提示只给出位置和边界，规则以仓库文档为准，
不要把规则复制进提示，以免文档更新后两处不一致。

## Taskboard 自动调度（包侧 v3 已实现；Taskboard 接入后使用）

```text
你在 auto-cut-slicing 仓库中执行一次 VM/PMO 剪辑规划。

运行目录：<run_dir>
输入：<run_dir>\input.json
上下文：<run_dir>\execution-context.json
意图上下文：<run_dir>\intent-context.json（证据命令的 --context 用它）
证据清单：<run_dir>\evidence\manifest.json（已由 analyze 生成，含完整转写 transcript-full）

先阅读：
1. AGENTS.md
2. docs/editing-standards.md
3. docs/ai-editing-workflow.md
4. docs/intent-schema.md

任务：按 docs/ai-editing-workflow.md 第 1–4 步，为 input.json 中的全部 instructions 写出
<run_dir>\edit-intent.json。需要更多证据时只使用工作流“能力状态”表中列出的命令。

边界：
- 只在运行目录内写文件，不修改仓库文件，不运行 render，不上传或回写任何系统。
- 这是无人值守运行，不能向用户提问。需要用户决定、证据不足或能力不支持时，
  写出 status 为 blocked 的意图文件并说明原因。
- 时间只能引用 docs/intent-schema.md“时间来源”中的来源。
- 结束时输出一段简短报告：每条意见的处理方式、阻断原因、仍需人工复核的项目。
```

QA 复核阶段（第 6 步）使用同样的开头，把“任务”改为：

```text
任务：阅读 <run_dir>\qa.json 及其引用的帧和转写证据，对状态为 needs_human 且属于画面类的检查，
在能看图时写出复核结论到 <run_dir>\qa-review.json；不能看图或看不清时保持 needs_human。
不得修改 qa.json 中 automatic 方法的结果，不得把 listening 或 pacing 标为通过。
```

## Codex 聊天手动交办（现在可用）

```text
请在 auto-cut-slicing 仓库按 docs/ai-editing-workflow.md 处理这次剪辑。
源视频：<路径>
剪辑意见（原文，请逐字保留）：
<意见>
交付方式：<MP4 / 剪映草稿>
工作文件放在 tmp/<任务名>/。遇到重复语句无法区分、缺少证据或做不到的要求时，先列出来问我。
```
