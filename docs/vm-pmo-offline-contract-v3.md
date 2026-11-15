# VM / PMO 离线剪辑协议 v3 · 设计草案

**状态：设计草案，未实现。** 包版本 `0.3.0` 的 manifest 仍声明 contract v2，`vm_pmo` 只接受 schema v1 / v2。
本文定义 Taskboard 自动调度时 AI 如何参与规划，供两边实现时对照。实现前不得按本文发起运行。

v1、v2 见 [v1](vm-pmo-offline-contract-v1.md)、[v2](vm-pmo-offline-contract-v2.md)，行为和计划哈希保持不变。
AI 规划的步骤与规则见 [AI 参与剪辑工作流](ai-editing-workflow.md) 和 [意图计划](intent-schema.md)。

## 要解决的问题

v2 的意见解析只接受固定句式（`instructions.py`）或人工填写的 `manual_revision`。
新的自然语言说法即使能对应到已有操作，也会以 `UNSUPPORTED_INSTRUCTION` 阻断。
v3 让 Taskboard 启动的 AI 按工作流写出 `edit-intent.json`，由包校验后编译成现有 clips 执行，
同时保留固定句式作为不需要 AI 的快速通道。

## 分步命令

所有命令仍然只从受信 context 读取路径，在固定运行目录内写文件，且不覆盖已有文件。

| 步骤 | 命令 | 产出 |
| --- | --- | --- |
| 1 | `vm_pmo analyze --input --context` | `alignment.words.json`（需要时）、`evidence/` 与 `evidence/manifest.json`、`analyze-result.json` |
| 2 | Taskboard 启动 AI，AI 按工作流写入 `edit-intent.json` | 意图文件；AI 不调用 `render` |
| 3 | `vm_pmo plan --input --context` | 读取意图（或固定句式），校验 V01–V14 后写 `plan.json` 与 `plan-result.json` |
| 4 | `vm_pmo render --input --context --plan` | 与 v2 相同 |
| 5 | `vm_pmo qa --input --context` | `output.words.json`、`qa.json`、接缝预览帧 |
| 6 | AI 复核 QA 材料，补写 `ai_reviewed` 项 | `qa-review.json`：只能补充 AI 复核结论，不能修改自动检查结果 |

`planning_mode: "rule"` 时跳过第 2 步，第 3 步使用固定句式解析，与 v2 的结果一致。
`planning_mode: "ai_intent"` 时，第 3 步必须找到意图文件；固定句式也能解析时，两者编译结果不一致即阻断（交叉核对）。

## 输入（相对 v2 的变化）

- `schema_version: 3`，context `schema_version: 3`。
- 新增 `planning_mode`：`"rule"` 或 `"ai_intent"`。
- `instructions` 允许多条，各自 `instruction_id` 唯一；每条都要被意图覆盖。
  `must_keep` 与 `visual_evidence` 字段沿用 v2。
- 新增可选 `prior_rounds`：历史轮次的意见快照，只读，用于理解上下文，不作为本轮要执行的意见。
- 新增 `ai_session`：`{agent, model, session_id}`，由 Taskboard 填写，写入回执。
- `manual_revision` 沿用 v2 语义，与 `planning_mode: "ai_intent"` 互斥。

## Context（相对 v2 新增）

| 字段 | 说明 |
| --- | --- |
| `evidence_dir` | 运行目录内的 `evidence/` |
| `intent_path` | 运行目录内的 `edit-intent.json` |
| `analyze_result_path` | `analyze-result.json` |
| `qa_path` | `qa.json` |
| `qa_review_path` | `qa-review.json` |

路径规则同 v2：绝对、规范、在运行目录内、互不相同，不跟随链接。

## 结果

- `analyze-result.json`：`ready` 或 `blocked`，列出证据清单哈希；ASR 不可用时 `blocked`，错误码 `WORD_ALIGNMENT_UNRESOLVED`。
- `plan-result.json` 在 v2 基础上增加：
  - `intent_sha256`；
  - `coverage`（每段意见的处理方式）；
  - `blockers`（阻断代码、意见 span、缺失证据、候选列表）；
  - 每个操作对应的 `intent_id`。
  阻断时错误码为 `INTENT_BLOCKED`，具体原因在 `blockers` 中。
- render 结果与 v2 相同。
- `qa.json` 遵循 [qa-report.v1](../schemas/qa-report.v1.schema.json)，`overall` 为 `failed` 或 `ready_for_review`。
  Taskboard 在审批界面展示计划、证据、QA 和其中的 `needs_human` 项；`failed` 不进入交付。

## Taskboard 侧职责

- 选择 `planning_mode`，在第 2 步启动 AI，并使用 [启动提示模板](prompts/taskboard-ai-editing.md)。
- AI 会话的工作目录是包仓库，可写范围限于运行目录。
- 第 2 步超时或 AI 没有写出意图文件时，记为阻断，不回退成固定句式猜测。
- 审批、下载、上传、阶段回写和归档保持现有流程。

## 兼容与升级

- v1 / v2 输入继续按原路径处理，计划哈希不变；新增的 v3 代码不改动 v1 / v2 分支。
- 包发布 v3 时 manifest 声明 contract v3、driver `vm_pmo_review_video_v3`。Taskboard 需要同时支持 v2 和 v3 driver。
- **在途任务风险**：`_validate_input` 要求 `package.version` 等于 `0.3.0`，且 manifest 哈希一致。
  包升级后，按旧版本登记、尚未完成的 v2 运行会以 `PACKAGE_MISMATCH` 阻断。
  升级前须清空在途运行，或由 Taskboard 按新包重新登记；这需要 Taskboard 侧确认处理方式。
- 本地批量请求（`run`，contract v1）计划增加 `output.intent`，与 `instruction`、`clips` 三选一；旧请求和 `--resume` 不变。

## 待确认问题

- Taskboard 无界面启动的 Codex 能否读取本地图片。不能读取时，画面检查全部为 `needs_human`。
- AI 步骤的超时、费用上限和重试次数由 Taskboard 设定，本包只要求重试时复用同一运行目录与证据。
- 成片反向 ASR 每轮多一次 ASR 调用，是否默认开启。
