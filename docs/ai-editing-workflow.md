# AI 参与剪辑工作流

本文规定 AI（Codex）如何读取剪辑意见、收集证据、写出[剪辑意图计划](intent-schema.md)、
调用受控工具执行并检查意见是否落实。Codex 聊天中手动交办和 Taskboard 自动调度走同一套步骤，
差别只在遇到需要用户决定的问题时怎么处理。

开始前先读 [剪辑经验与交付准则](editing-standards.md)；交付前执行
[实际剪辑交付清单](validation.md#实际剪辑交付清单)。当前用户的明确要求优先。

## 基本原则

1. **AI 负责理解和选择，不负责产生时间。** 所有时间都来自用户写出的时间、ASR 词界、真实解码帧、
   源首尾或音频测量，并按 [时间来源](intent-schema.md#时间来源) 记录；包会重算核对。
2. **意见逐字覆盖。** 意见里每一段文字都要说明是执行、约束、说明，还是做不到。没有说明的内容视为遗漏并阻断。
3. **不确定就阻断，不猜。** 重复语句没有区分依据、缺证据、能力不支持时，写出阻断原因和缺什么，
   不用近似操作代替，也不默默跳过。
4. **执行只走维护中的规划器和渲染器。** AI 不直接拼 FFmpeg 命令产出交付文件，不手改计划或回执。
5. **检查结论分级记录。** 自动检查、AI 看图或读文本的复核、人工试听分开写；AI 复核不等于人工验收，
   任何自动结果都不能写成“已试听”“已验收”。

## 职责分工

| 方 | 负责 |
| --- | --- |
| Taskboard | 启动 AI、任务排队、审批、素材获取、最终交付与回写；把原意见、源文件和运行目录交给本包 |
| 本包 | 剪辑规范、本工作流、证据工具、意图校验与编译、规划与渲染、内容质量检查 |
| AI（Codex） | 按本文读意见、调用证据工具、写意图计划、复核 QA 材料、报告结果和阻断原因 |

本包不访问飞书或 VM 服务，不发起审批，不上传附件。

## 能力状态

新会话必须按此表区分可以直接执行的命令和尚未实现的设计，不得把后者写成已执行。

所有命令都读取一份 [intent-context](../schemas/intent-context.v1.schema.json)：源文件、词级转写、原意见和证据目录，
路径相对该文件。命令以 `python -m auto_cut_slicing` 开头。

| 能力 | 状态与命令 |
| --- | --- |
| 源绑定词级 ASR | 已实现：`transcribe <源> --output <words.json>`（调用锁定的 Auto-Cut Lite 火山 ASR，会产生费用） |
| 阅读转写 | 已实现：`evidence transcript --context C --source S --id ID --start T --end T`，含词序号、句子和 ≥200ms 停顿 |
| 查找全部出现位置 | 已实现：`evidence find-phrase --context C --source S --id ID --text 短语 [--scope-start --scope-end --anchor]` |
| 帧证据 | 已实现：`evidence frames --context C --source S --id ID --at T [--at T] [--neighbors N]`，导出真实解码帧 PNG |
| 停顿测量 | 已实现：`evidence audio-gap --context C --source S --id ID (--after-word N \| --at T)`，测量上一词衰减结束、下一词起音和可切窗口 |
| 意图校验 | 已实现：`intent validate INTENT --context C`，执行 V01–V13，返回问题列表 |
| 意图编译 | 已实现：`intent compile INTENT --context C --job J --receipt R [--plan P]`，生成现有 job，规划后执行 V14 |
| 规划与渲染 | 不变：`plan` / `render` / `draft` / `run`；VM/PMO 用 `vm_pmo plan` / `render` |
| 成片反向 ASR | 手动：对成片运行 `transcribe`，再把结果传给 `qa --video-asr` |
| 内容质量检查 | 已实现：`qa --context C --intent I --plan P --output-id O --video V --report R [--video-asr W]`，并导出接缝帧 |
| AI 复核 QA 画面（qa-review） | 已实现：`qa` 同时写 `<report>-review-request.json`，看图后写 qa-review 文件，`qa-review --report --review` 校验并合并（`clean` → `ai_reviewed`，`defect` → `failed`，`unclear` 仍 `needs_human`）；VM/PMO 用 `vm_pmo qa-review` 并写 `qa-review-result.json`。包不调用模型，真实模型的看图质量未验证 |
| VM/PMO v3 包侧命令 | 已实现：`vm_pmo analyze` / `plan`（`planning_mode: ai_intent`）/ `render` / `qa` / `qa-review`，见 [v3](vm-pmo-offline-contract-v3.md)；manifest 已声明 v3（2026-11-17） |
| Taskboard 自动入口携带意图 | 已接入：Taskboard 的 v3 driver 启动 AI 写意图并调度 analyze → plan → render → qa → qa-review（feishu-codex-Taskboard #25、#49）。真实测试记录上的端到端验收未做 |
| AI 在 Taskboard 中看图 | 已接入但**未在真实素材上验证**：Taskboard 把导出帧作为图片附给 AI 会话。AI 判 `clean` 只记为 `ai_reviewed`，不等于验收；能否抓到真实残留需用真实成片验证 |
| 审核截图 | 已接入：manifest 声明 `interface.inputs: ["review_images"]` 后，Taskboard 把审核截图作为图片附给第 2 步的 AI 会话，用于理解意见位置；截图不是时间来源，见 [v3](vm-pmo-offline-contract-v3.md#审核截图interfaceinputs-review_images) |
| 审核轮回到原片 | 已实现：manifest 声明 `review_original_source` 后，审核轮在原片上重新规划整条视频并覆盖全部轮次意见；审核意见中的时间按该稿片段表换算成原片时间，见 [v3](vm-pmo-offline-contract-v3.md#审核轮回到原片interfaceinputs-review_original_source) |
| 审核轮续接对话 | Taskboard 在审核轮用 `codex exec resume` 接回上一轮的同一 AI 会话，提示中给出上一轮运行目录（只读）和新的运行目录；无法续接时新开会话 |

## 步骤

### 0. 准备

- 确认入口：聊天交办（本地 `job` / `request`）还是 Taskboard 运行目录。
- 记录源文件路径、SHA-256、时长，以及 `timeline_basis`：本地源、VM 原片，或审核轮次的 parent MP4。
- 原意见逐码点保存，不改写、不合并补充意见。补充意见作为新的一条 instruction 单独记录。
- 工作文件放在忽略目录：本地任务用 `tmp/<任务>/`，Taskboard 用运行目录。

### 1. 收集证据

- 意见涉及词、句、语音起点、`must_keep` 或 `sentence` 边界时，生成或复用源绑定的 `words.json`。
  ASR 不可用时停止，报告 `INTENT_EVIDENCE_MISSING`，不按粗略时间继续。
- 对意见中出现的每个引用短语查找**全部**出现位置，记下候选。
- 每个片段开头、每处拼接、每个候选定格帧，导出前后帧供查看，并确认帧时间是真实解码帧。
- 证据文件登记到 `evidence/manifest.json`（格式见
  [evidence-manifest.v1](../schemas/evidence-manifest.v1.schema.json)），每个文件记录哈希和所绑定的源。

### 2. 拆分意见并写覆盖表

- 把每条意见切成 span，逐段给出 `disposition`（见 [意见覆盖](intent-schema.md#意见覆盖)）。
- 判断语义时以剪辑准则为准，例如：“剪掉某词”未说只删声音时音画同时删除；
  “（首先）”这类注释是语音起点；“配后面某一秒的画面”指选帧位置，覆盖持续到内容自然恢复的位置。
- 新的说法只要能对应到已有操作就照常处理，不要求符合固定句式。
  确实对应不到任何操作时标为 `unsupported`，写明缺的是什么能力。

### 3. 选定时间和位置

- 每个时间按 [时间来源](intent-schema.md#时间来源) 写成 `time_ref`。
- 引用短语出现多次时，按 [重复语句与候选选择](intent-schema.md#重复语句与候选选择) 选择；
  意见没有给出区分依据时阻断并列出候选。
- 起点：以用户标注的时间为局部锚点，选就近的完整词或短语，通常偏移在约 1 秒内；
  偏移更大时复核内容并在 `rationale` 中写明理由。不为凑 ASR 整句带入更早的引入语。
- 终点：落在词内或句内时补到当前句末，不带入下一句。
- 用户写的结束时间也是局部锚点（用户 2026-11-17 确认）：落在末词词尾或其后的停顿里时，不要停在该时间，
  按下面“出点”一条向后延到 `out_window` 内，相对用户时间最多约 1 秒，并在覆盖表 `reason` 写明。
  需要超过约 1 秒或会带入下一词时不延长。validator 不检查这个上限，按本条自行遵守。
- 开头或接缝残留前一主题时，用后面已确认干净的新画面 `freeze_cover`（`clean_cut_in`），覆盖到内容自然恢复，声音继续。
  意见没有提到时也要做：`covers` 留空并写 `standard_rule: "clean_cut_in"`。
  选帧时导出切入点之后的若干帧逐张查看，取第一张完全干净、且后续自然播放仍干净的帧；覆盖终点就取这一帧。
- 接缝过快需要停顿时，只能保留源中已有、无下一词的尾段，并用前画面 `freeze_cover`（`semantic_pause`）；
  尾段不够时报告 `INTENT_UNSUPPORTED_OPERATION` 或 `INTENT_NEEDS_USER_DECISION`，不伪造空白。
- 出点不要停在最后一个词的 ASR 终点：先对该词之后的停顿运行 `evidence audio-gap`，出点用 `gap_choice` 落在
  `out_window` 内。实测样片中 ASR 终点之后普遍还有 0.06–0.32s 衰减，停在 ASR 终点会截掉尾音。
  **默认停顿（用户 2026-11-16 试听确认）**：每个切口在上一词衰减结束与下一段起音之间留约 1 秒无语音。
  出点取 `min(out_window 上界, decay_end + 1s)`；接缝不足 1 秒的部分，由后段入点用 `in_window` 前移补足；
  开头前导同样最多约 1 秒。源中停顿不够时取最大值并在 rationale 写明。用户写了时长时用 `user_duration`。
- **停顿期间的画面用前一段的最后一帧**：后段入点前移后，在后段 `[入点, 后段起音)` 上加
  `freeze_cover`（`purpose: semantic_pause`），`frame` 取前一段出点前最后一个解码帧（先用 `evidence frames` 取证）。
  `end` 取后段画面开始动的第一帧（转场动画开始）与后段起音（audio-gap 的 `low_energy_end`，含转场音效）中较早者。
  用 `evidence frames --neighbors` 在入点之后逐帧看：若那一帧还露出前一主题，`end` 改为旧内容刚被完全盖住的那一帧。
  之后转场照常播放；不要定格到转场结束再硬切，也不要用后段的帧填停顿。开头没有前一段，残留才用后面的干净新画面。

### 4. 写出意图计划

- 写 `edit-intent.json`，结构见 [edit-intent.v1](../schemas/edit-intent.v1.schema.json)，示例见
  [examples/edit-intent.json](../examples/edit-intent.json)。
- 阻断时也写出完整文件（`status: blocked`），已理解的部分照常记录。
- 运行 `intent validate`，直到没有问题；阻断状态的意图也要能通过校验。

### 5. 编译与规划

- 运行 `intent compile`：它按 [编译映射](intent-schema.md#编译映射) 生成 job，调用规划器并执行 V14，
  确认计划中的词区间和起点正是选中的那一处。报错时补证据或改意图，不绕过检查。

### 6. 渲染

- 只用 `render` / `run` / `vm_pmo render` / `draft` 生成交付物，不覆盖已有文件。返修使用新的输出路径。

### 7. 质量检查

逐项写入 `qa.json`（格式见 [qa-report.v1](../schemas/qa-report.v1.schema.json)）。

| 检查 | 方法 | 可以给出的状态 |
| --- | --- | --- |
| `coverage_complete`：意见每段都有处理 | 读意图文件 | `passed` / `failed` |
| `operation_compiled`：每个操作都出现在计划中 | 对照计划 | `passed` / `failed` |
| `phrase_absent`：删除的短语在成片对应位置消失 | 成片反向 ASR，在 `target_join_us` 前后约 2 秒内查找 | `passed` / `failed` / `not_run` |
| `phrase_present` / `must_keep_present`：保护短语仍在 | 成片反向 ASR | `passed` / `failed` / `not_run` |
| `cue_first_word`：片段从指定词开始 | 成片反向 ASR 在片段目标起点后的第一个词 | `passed` / `failed` / `not_run` |
| `duration_match`：时长与计划一致 | 渲染结果 | `passed` / `failed` |
| `cut_not_inside_word`：切点不在词内 | 源 ASR 词界对照计划切点 | `passed` / `failed` |
| `sentence_complete`：终点句子完整 | 句界证据 | `passed` / `failed` / `needs_human` |
| `seam_gap`：切点不截衰减、不切进下一词起音 | 对每个切点实测上一词衰减与下一词起音（同 audio-gap） | `passed` / `needs_human` |
| `opening_frame` / `seam_frames` / `freeze_frames`：画面无残留、无闪回 | 查看导出的帧（定格另导出最后一帧和恢复播放第一帧） | `ai_reviewed` / `needs_human` / `failed` |
| `seam_pause_picture`：接缝停顿期间仍是前一段画面，且后段从画面开始动的那一帧切入 | 停顿区间逐帧与前一段末帧比较（峰值，不只看均值，小面积转场也能抓住）；另测后段源里画面开始动的第一帧 | `passed` / `failed` / `needs_human` / `not_run` |
| `mute_silence`：静音区间确实无声 | 测量成片该区间的音频电平，阈值 −60 dBFS，不需要 ASR | `passed` / `failed` / `not_run` |
| `frame_count`：成片帧数与计划一致 | ffprobe 逐帧时间戳，与计划时长×帧率对照 | `passed` / `failed` |
| `frame_timing`：帧间隔均匀 | 相邻帧时间戳之差与该帧率的名义间隔对照 | `passed` / `failed` |
| `black_frames`：没有黑场 | 逐帧亮度在 64×36 灰度代理上测量 | `passed` / `failed` |
| `blurred_frames`：没有糊到不可用的帧 | 拉普拉斯方差，排除黑场后统计 | `passed` / `failed` |
| `luma_flashes`：没有非计划的亮度突变 | 相邻帧亮度差；计划接缝和定格边界附近不计 | `passed` / `failed` |
| `still_frames`：静帧只出现在计划的定格区间，或原片本身在同一处就是静止画面（课件页、暂停的录屏） | 逐帧像素签名（32×18 采样）比较，相邻定格区间先合并；其余静帧逐帧对照计划放在该处的原片帧，一致即为原片内容而非卡帧 | `passed` / `failed` |
| `speech_present`：成片有语音 | 成片反向 ASR | `passed` / `failed` / `not_run` |
| `unlisted_change`：没有意见之外的改动 | 计划操作与覆盖表对照 | `passed` / `failed` |
| `listening` / `pacing`：听感与节奏 | 只能由人完成 | `needs_human` / `not_run` / `failed` |

- 反向 ASR 只是一种证据：识别错误可能造成假阳性或假阴性，`failed` 时先回听确认再改。
- `opening_frame` / `seam_frames` / `freeze_frames` 的 `needs_human` 可以由 AI 看图复核：QA 同时写出
  `<report>-review-request.json`，列出每个画面项要看的帧（含哈希）和固定问题。AI 按要求看帧后写
  qa-review 文件，`qa-review --report --review` 校验并合并：`clean` → `ai_reviewed`（`ai_visual`），
  `defect` → `failed`，`unclear` → 仍 `needs_human`。自动检查结果、`listening`、`pacing` 不会被复核改写。
  复核文件必须绑定当前 `qa.json` 的哈希、逐帧哈希和每一个问题，缺一项即拒绝。
- 上表 `frame_count` 到 `still_frames` 是直接检查成片文件的机械项，不依赖 ASR 或主观判断，能抓住跳帧、丢帧、黑场、意外静帧和画面糊掉。它们只判断“这帧是否可用”，不判断内容对不对。
- `seam_pause_picture` 标 `needs_human` 是因为像素分不清两种情况：定格盖住的运动帧可能还露着前一主题（转场没盖完，该继续定格），也可能转场已经播完（定格过头，该提前切入）。检查会把运动起点到起音之间的帧导出，由人确认切入点是旧内容刚被盖住的那一帧。
- `overall` 只有 `failed` 和 `ready_for_review` 两种，没有“已验收”。
- `qa.json` 中的 `listening` / `pacing` 不能标为 `passed`。人工试听的结论记在
  [交付清单](validation.md#实际剪辑交付清单) 的验收记录里，不回写 `qa.json`。

### 8. 报告

报告内容：

- 输出路径、时长和哈希；
- 每条意见的处理方式及对应操作；
- 选中位置及依据；
- 阻断原因和缺失的证据；
- QA 各项状态，以及哪些项仍需人工完成。

结构检查、AI 复核和人工试听分开写。

## 两个入口的差别

| 情况 | 聊天交办 | Taskboard 自动调度 |
| --- | --- | --- |
| 候选有歧义、需要用户决定 | 列出候选请用户选择；回答作为新的补充意见记录后继续 | 不能提问：写 `blocked` 意图和阻断原因，交审批环节处理 |
| 能力不支持 | 说明做不到什么，并给出可行的替代做法供用户确认 | `blocked`，原意见保留在回执中 |
| 画面复核 | 能看图时可写 `ai_reviewed`，仍提示用户确认 | 看图复核已接入（`qa-review`）；结论 `unclear` 或看图失败时保持 `needs_human` |
| 交付 | 本地路径和报告 | 只返回计划、证据与 QA；审批、上传、回写由 Taskboard 完成 |

## 禁止事项

- 写出没有来源的时间，或用“大约”“左右”的时间代替词界。
- 为了得到唯一匹配而收窄搜索范围，或在多处候选中凭感觉挑一处。
- 跳过、合并或改写意见中的任何部分；把理由、诊断或状态写进 `source_text`。
- 用 `mute_word` 代替删除，用低能量区间冒充纯静音，或把下一句的词带进来凑停顿。
- 手改计划、回执或 MP4；绕过规划器直接用 FFmpeg 生成交付物。
- 把合成素材或合成 ASR 的测试结果写成真实素材的验收结论。

## 命令示例

```powershell
python -m auto_cut_slicing evidence find-phrase --context tmp/x/context.json --source talk --id p-zaikan --text 我们再看一下 --anchor 3:30
python -m auto_cut_slicing evidence frames --context tmp/x/context.json --source talk --id fr-seam --at 158.75 --at 163 --neighbors 1
python -m auto_cut_slicing intent validate tmp/x/edit-intent.json --context tmp/x/context.json
python -m auto_cut_slicing intent compile tmp/x/edit-intent.json --context tmp/x/context.json --job tmp/x/job.json --plan tmp/x/plan.json --receipt tmp/x/compile.json
python -m auto_cut_slicing render tmp/x/job.json --output-id review --output tmp/x/review.mp4
python -m auto_cut_slicing qa --context tmp/x/context.json --intent tmp/x/edit-intent.json --plan tmp/x/plan.json --output-id review --video tmp/x/review.mp4 --report tmp/x/qa.json
```

证据写入后，意图的 `bindings.evidence_manifest_sha256` 要等于 `evidence/manifest.json` 当前的哈希；
新增证据会改变它，需要同步更新意图。`qa` 的接缝帧写到报告旁的 `<报告名>-frames/`，不写入证据清单。

## 测试素材与验证

验证本工作流使用独立测试素材，不使用生产样片：

- **确定性测试**（pytest、合成媒体、合成 ASR）：
  - 覆盖遗漏：意见有三个要求、意图只覆盖两个，必须阻断。
  - 时间来源：重算不一致、来源哈希不对时必须拒绝。
  - 重复语句的四种选择方法，以及没有依据时阻断。
  - 编译映射的收窄半径确实落在选中的那一处。
  - V14 对照。
  - 已知位置的提示音，用来验证切点精确到采样。
- **AI 评测**（不进 CI）：同一意图的多种说法，以及歧义、不支持、缺证据的意见。
  让 AI 按本文生成意图后，与人工标注的预期比较。记录模型版本、通过率、错误阻断和凭空编造时间的次数。
- **真实测试素材**：单独录制，包含重复句、口头禅、板书变化和转场，并配人工标注的标准答案
  （词界、应选的出现位置、干净帧）。按 [真实验收清单](real-acceptance-checklist.md) 执行，人工试听和画面复核单独记录。

测试素材、标注和评测结果放在忽略目录，项目中只提交可复用的规则和合成夹具。

## 沉淀

新的确认反馈先归纳为“触发条件 → 编辑选择 → 验证方法”写入 [剪辑准则](editing-standards.md)；
影响意图结构或校验时同步本文、[intent-schema](intent-schema.md) 和 JSON Schema，并按其“版本”规则处理。
