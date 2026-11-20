# VM / PMO 离线剪辑协议 v3

**状态：包侧与 Taskboard 侧均已实现，manifest 已声明 v3（2026-11-17）；真实测试记录验收（第三阶段）尚未进行。**

- `vm_pmo` 已接受 schema v3，并提供 `analyze`、`plan`、`render`、`qa` 四个命令（见下文“已实现的包侧细节”）。
- 包版本仍为 `0.3.0`；`PACKAGE-MANIFEST.json` 声明 contract v3 / driver `vm_pmo_review_video_v3`。
  切换前已按“升级前清空”确认没有在途生产运行（仅一条测试运行，按用户决定任其以 `PACKAGE_MISMATCH` 阻断）。
- Taskboard 侧 v3 driver 已合并（fengcong22/feishu-codex-Taskboard#25）。启用还需要 Taskboard 配置
  `CODEX_VM_PMO_AI_MODEL` 和沙箱可读的 FFmpeg 目录 `CODEX_VM_PMO_AI_TOOL_DIR`。
- 包仍接受 schema v1 / v2 输入，行为与计划哈希不变。
- 第 6 步由 `vm_pmo qa-review` 执行（见下文）。

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
  Taskboard 在审核轮填写以往各轮（初稿及更早的审核轮）的原意见，按时间从早到晚排列。
- 新增 `ai_session`：`{agent, model, session_id}`，由 Taskboard 填写，写入回执。
- `manual_revision` 沿用 v2 语义，与 `planning_mode: "ai_intent"` 互斥。

## 审核截图（`interface.inputs: ["review_images"]`）

manifest 在 `interface.inputs` 中声明 `review_images`，表示本包的 v3 AI 规划接受审核截图作为意见的一部分。
Taskboard 只在包作了这个声明时，才登记审核意见附件字段里带截图的审核轮；未声明时仍以
`VM_REVIEW_IMAGES_UNSUPPORTED` 阻断，不忽略截图继续。

- 截图只交给第 2 步的 AI 会话（作为图片附件，并在提示中列出本机路径），用来理解意见指的是哪里。
  它不进入 `input.json`，`plan` / `render` / `qa` 不读取截图，计划哈希与截图无关。
- 截图不是时间来源：时间与画面仍须用证据命令（`evidence frames` 等）对本轮源视频取证，
  不能凭截图估计时间。截图与源画面对不上或看不出指哪里时，按“不确定就阻断”写 `blocked` 意图。
- 文件由 Taskboard 下载到运行目录之外（AI 不可写），并记录 SHA-256；只接受图片类型、单张不超过 20 MB、最多 10 张。
- `rule` 规划（结构化人工返修）不使用截图。

## 审核轮回到原片（`interface.inputs: ["review_original_source"]`）

v2 的审核轮以上一轮成片为源，成片里没有的内容（上一轮剪掉的话、更早的开头）无法恢复。
manifest 声明 `review_original_source` 后，Taskboard 可以让 `ai_intent` 审核轮在**原 VM 素材**上重新规划整条视频：

- `review_round.source_basis: "vm_original"`；`source` 是原 VM 素材（`timeline_basis: "vm_original"`）。
  `review_round.parent` 仍描述上一轮已交付的成片，但不再要求与 `source` 哈希一致。只允许 `ai_intent`。
- `instructions` 包含**全部轮次**的意见，每条都要被意图覆盖：
  - 初稿意见：`timeline_basis: "vm_original"`，`applicability: "carried_over"`；
  - 每轮审核意见：`timeline_basis: "previous_review_video"` 并带 `time_map_id`；本轮的为 `current_round`，更早的为 `carried_over`。
  - 后一轮意见与前一轮冲突时以后一轮为准，被取代的部分在覆盖表中写 `context` 并说明。
- `time_maps`：`[{time_map_id, plan_sha256, segments: [{source_start_us, duration_us, target_start_us}]}]`，
  即审核人当时看的那一稿的计划片段表（成片时间 → 原片时间），片段须首尾相接、落在原片内。
- intent-context 中这些意见带 `time_map`。意图里 `user_anchor` 的 `value_us` 写**原片时间**：
  validator 先解析意见中的成片时间，再按该意见的片段表换算后比对；正好落在接缝上的时间属于从该处开始的片段，
  成片末尾对应最后一段的末尾；落在成片之外即 `INTENT_TIME_UNSOURCED`。
- 多条意见时固定句式交叉核对为 `not_applicable`。上一轮的 `edit-intent.json` 已在原片时间轴上，可作为本轮起点修改。

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
5. 交叉核对：只有一条意见、且固定句式解析器也能解析并编译成功时，两边的保留段（源、起点、**覆盖的词**）、
   效果和边界必须一致，否则 `INTENT_RULE_MISMATCH`。固定句式解析不了时记为 `not_applicable`，不阻断。
   边界按“覆盖的词”比较：固定句式把用户写的时间落在时钟上，AI 落在词的边界上，因此两端之间只要
   **没有任何词开始**，较长的一端就只是多包含了同一段静音（例如写 4:34 而 AI 停在停顿上限），视为同一次剪辑；
   一旦中间夹着另一个词的起点，就是不同的剪辑，仍然阻断。两端起点不同、或效果不同，同样阻断。

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

**qa-review**（仅 `ai_intent`，第 6 步）：

```powershell
python -m auto_cut_slicing.vm_pmo qa-review --input <run_dir>\input.json --context <run_dir>\execution-context.json
```

- 前提：`qa-result.json` 为 `checked`，且 `qa.json` 的哈希等于其中的 `qa_sha256`。
- 读取 `qa_review_path` 指向的复核文件（`schemas/qa-review.v1.schema.json`），校验后把画面结论合并进 `qa.json`。
- 合并前先把 QA 原样保存到固定文件 `qa.before-review.json`；合并失败时恢复原文件并删除这份副本。
- 结果写入固定文件 `qa-review-result.json`（只写一次；重复调用返回已有回执）：
  `status`（`reviewed` / `blocked` / `failed`）、`plan_sha256`、`qa_sha256_before`（等于 `qa-result.json` 的 `qa_sha256`）、
  `qa_sha256`（合并后的 `qa.json`）、`review_sha256`、`overall`、`counts`、`error`。
- 阻断代码：`RENDER_REQUIRED`（没有可复核的 QA）、`REVIEW_MISSING`（没有复核文件）、`REVIEW_INVALID`（复核文件与报告不符）、`PLAN_CHANGED`。
- `qa-result.json` 不会被改写，它始终证明 QA 当时产出了什么；交付判定应读取 `qa-review-result.json` 并按其 `qa_sha256` 校验当前 `qa.json`。
- context 的 v3 路径不得指向 `qa-review-result.json` 或 `qa.before-review.json`。

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
