# VM / PMO 离线剪辑协议 v3

**状态：包侧与 Taskboard 侧均已实现，manifest 已声明 v3（2026-11-17）；真实测试记录验收（第三阶段）尚未进行。**

- `vm_pmo` 已接受 schema v3，并提供 `analyze`、`plan`、`render`、`qa` 四个命令（见下文“已实现的包侧细节”）。
- 包版本仍为 `0.3.0`；`PACKAGE-MANIFEST.json` 声明 contract v3 / driver `vm_pmo_review_video_v3`。
  切换前已按“升级前清空”确认没有在途生产运行（仅一条测试运行，按用户决定任其以 `PACKAGE_MISMATCH` 阻断）。
- Taskboard 侧 v3 driver 已合并（fengcong22/feishu-codex-Taskboard#25）。启用还需要 Taskboard 配置
  `CODEX_VM_PMO_AI_MODEL` 和沙箱可读的 FFmpeg 目录 `CODEX_VM_PMO_AI_TOOL_DIR`。
- 包仍接受 schema v1 / v2 输入，行为与计划哈希不变。
- 第 6 步 `qa-review.json` 只预留了路径，包内没有读写它的命令。

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
  已确定：升级前清空在途运行，确认没有未完成的 v2 运行后再切换包版本；Taskboard 不做重新登记迁移。
- 本地批量请求（`run`，contract v1）计划增加 `output.intent`，与 `instruction`、`clips` 三选一；旧请求和 `--resume` 不变。

## 已实现的包侧细节

命令：

```powershell
python -m auto_cut_slicing.vm_pmo analyze --input <run_dir>\input.json --context <run_dir>\execution-context.json
python -m auto_cut_slicing.vm_pmo plan    --input ... --context ...
python -m auto_cut_slicing.vm_pmo render  --input ... --context ... --plan <run_dir>\plan.json
python -m auto_cut_slicing.vm_pmo qa      --input ... --context ... [--reverse-asr]
```

stdout 仍是单行 JSON。`ready` / `rendered` / `blocked` / `checked` 退出码为 0，`failed` 为 1。

**输入 v3**：在 v2 字段之外，必填 `planning_mode`；`ai_intent` 时必填 `ai_session {agent, model, session_id}`，
`rule` 时不得出现 `ai_session`。可选 `prior_rounds`：`[{round_id, instructions: [{instruction_id, raw_text}]}]`，
只读、不执行。`manual_revision` 只能与 `rule` 一起使用。

**Context v3**：在 v2 字段之外，必填 `evidence_dir`、`intent_context_path`、`intent_path`、`analyze_result_path`、
`qa_path`、`qa_review_path`。全部在 `run_dir` 内、互不相同；JSON 路径不得位于 `evidence_dir` 内，
也不得占用固定文件 `alignment.words.json`、`output.words.json`、`qa-result.json`、`.plan.lock`。

**analyze**（仅 `ai_intent`）：
- 校验源，调用 ASR 写 `alignment.words.json`（已存在则复用、只复核）。
- 由 input 生成 `intent_context_path` 指向的 intent-context（源用绝对路径，`evidence_dir` 为相对路径），
  AI 的证据命令都读取这个文件。
- 写一条完整转写证据 `transcript-full`。
- `analyze-result.json`：`status`、`planning_mode`、`alignment_sha256`、`intent_context_sha256`、
  `evidence_manifest_sha256`、`evidence_ids`、`error`。ASR 失败时为 `blocked` / `WORD_ALIGNMENT_UNRESOLVED`。
- 重复调用返回已有结果，ready 时先复核对齐和 intent-context 未被改动。

**plan（`ai_intent`）**：
1. 复核 analyze 结果、对齐文件和 intent-context，与当前 input 不一致即阻断。
2. 意图文件不存在：`INTENT_MISSING`。
3. 执行 V01–V13：有问题时为 `INTENT_INVALID`，`blockers` 逐条列出规则、代码、消息和 JSON 路径；
   意图自身 `status: blocked` 时为 `INTENT_BLOCKED`，`blockers` 原样带出 AI 写的阻断。
4. 意图必须恰好一个 output；编译并执行 V14。
5. 交叉核对：只有一条意见、且固定句式解析器也能解析并编译成功时，两边的保留段（源、起点、时长）、
   效果和总时长必须一致，否则 `INTENT_RULE_MISMATCH`。固定句式解析不了时记为 `not_applicable`，不阻断。

计划在 v2 字段之外增加 `planning_mode`、`output_id`、`ai_session`、`intent_sha256`、`evidence_manifest_sha256`、
`coverage`、`rule_cross_check`（`agreed` / `not_applicable` / `null`）；每个操作带 `intent_id`。
`instruction_results` 按覆盖表把每条意见映射到它产生的片段。

`plan-result.json` 增加 `planning_mode`、`intent_sha256`、`coverage`、`blockers`。
ready 时 `blockers` 为空；blocked 时 `coverage` 为 null，`blockers` 为具体原因（无细分原因时为 `[error]`）。
已有计划被重复调用时，额外复核意图文件哈希。render 照常完整重算并对照计划，意图在规划后被改动即 `PLAN_CHANGED`。

**plan（`rule`）**：与 v2 相同（单条意见、固定句式或 `manual_revision`），不需要 analyze，
回执的 `intent_sha256`、`coverage` 为 null。

**qa**（仅 `ai_intent`）：要求 `result.json` 为 `rendered`，且计划、MP4 哈希与之一致；重算计划后调用内容 QA，
写 `qa_path` 和同目录的 `qa-frames/`。`qa.json` 的 `bindings.plan_sha256` 绑定本轮 `plan.json` 的字节
（QA 内部使用的引擎计划由它重算，哈希为 `plan.json` 中的 `engine_plan_sha256`）。`--reverse-asr` 时先对成片调用 ASR 写 `output.words.json`（会产生费用），默认关闭。
结果同时写入固定文件 `<run_dir>\qa-result.json`（stdout 内容相同）：`status`（`checked` / `blocked` / `failed`）、
`plan_sha256`、`qa_sha256`、`overall`、`counts`、`reverse_asr`、`error`。回执只写一次，阻断也不例外；
重复调用直接返回已有回执，不重新检查。`checked` 只表示检查已运行，
`overall` 才是结果；`needs_human` 项仍须人工复核。

**测试边界**：`tests/test_vm_pmo_v3.py` 用合成媒体、合成 ASR 和手写意图代替真实 ASR 与 AI 步骤，
只验证确定性机制，不代表真实识别、AI 判断、试听或 Taskboard 交付。

## 已确定的决定（2026-11-17 用户确认）

| 问题 | 决定 |
| --- | --- |
| 包升级时的在途 v2 运行 | 升级前清空，确认没有未完成的 v2 运行再切换；不做重新登记迁移 |
| AI 写意图步骤的超时与重试 | 15 分钟；不自动重试。超时或没写出意图文件记为阻断，交审批环节由人决定是否重跑（重跑复用同一运行目录与证据） |
| 成片反向 ASR | 默认开启：Taskboard 调用 `qa` 时传 `--reverse-asr`，每轮多一次 ASR 费用 |

## 待确认问题

- Taskboard 无界面启动的 Codex 能否读取本地图片。不能读取时，画面检查全部为 `needs_human`。
- AI 步骤的费用上限（超时已定为 15 分钟）。
