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

| 能力 | 现行做法 | 规划中的命令（未实现） |
| --- | --- | --- |
| 源绑定词级 ASR | `python -m auto_cut_slicing transcribe <源> --output <words.json>` | `analyze` 中自动生成 |
| 阅读转写 | 直接读 `words.json` 的 `words` / `utterances`，词序号即列表下标 | `evidence transcript` |
| 查找全部出现位置 | 调用 `auto_cut_slicing.alignment.find_phrase_matches`（见下文“现行替代做法”） | `evidence find-phrase` |
| 帧证据 | 用 `inspect-freeze --frame-time` 确认时间是真实解码帧；图片导出到 `tmp/` 供查看 | `evidence frames` / `contact-sheet` |
| 停顿测量 | 读 `words.json` 中相邻词的间隔并试听；没有自动测量，停顿结论为 `needs_human` | `evidence audio-gap` |
| 意图校验与编译 | 按 [编译映射](intent-schema.md#编译映射) 手工转成 clips，执行 V14 对照 | `intent validate` / `intent compile` |
| 规划与渲染 | `plan` / `render` / `draft` / `run`；VM/PMO 用 `vm_pmo plan` / `render` | 不变 |
| 成片反向 ASR | 对成片运行 `transcribe <成片> --output <out.words.json>` | `qa` 中自动执行 |
| 内容质量检查 | 按下文“质量检查”逐项手工记录，格式遵循 qa-report | `qa` |
| Taskboard 自动入口携带意图 | **不支持**。v2 只接受固定句式或 `manual_revision` | [v3 草案](vm-pmo-offline-contract-v3.md) |
| AI 在 Taskboard 中看图 | **未验证**。在验证前，画面判断一律交给审批环节由人复核 | — |

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
- 开头或接缝残留前一主题时，用后面已确认干净的新画面 `freeze_cover`（`clean_cut_in`），覆盖到内容自然恢复，声音继续。
- 接缝过快需要停顿时，只能保留源中已有、无下一词的尾段，并用前画面 `freeze_cover`（`semantic_pause`）；
  尾段不够时报告 `INTENT_UNSUPPORTED_OPERATION` 或 `INTENT_NEEDS_USER_DECISION`，不伪造空白。

### 4. 写出意图计划

- 写 `edit-intent.json`，结构见 [edit-intent.v1](../schemas/edit-intent.v1.schema.json)，示例见
  [examples/edit-intent.json](../examples/edit-intent.json)。
- 阻断时也写出完整文件（`status: blocked`），已理解的部分照常记录。
- 自查 [校验规则](intent-schema.md#校验规则) V01–V13。`intent validate` 实现前，
  在报告中写明“未经 validator 校验，已人工逐条自查”。

### 5. 编译与规划

- 按 [编译映射](intent-schema.md#编译映射) 得到 clips，放进本地 `job`、`request` 或（v3 实现后）运行目录。
- 运行 `plan`（或 `run` / `vm_pmo plan`）。规划器报错时，按错误补证据或改意图，不绕过检查。
- 执行 V14：对照计划中的 `edits`、`start_cues`、`effects`、`boundary_adjustments`，确认词区间和起点正是选中的那一处。

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
| `seam_gap` / `seam_discontinuity`：接缝间隔、爆音 | 词间隔与波形对照 | `passed` / `failed` / `needs_human` |
| `opening_frame` / `seam_frames` / `freeze_frames`：画面无残留、无闪回 | 查看导出的帧 | `ai_reviewed` / `needs_human` / `failed` |
| `unlisted_change`：没有意见之外的改动 | 计划操作与覆盖表对照 | `passed` / `failed` |
| `listening` / `pacing`：听感与节奏 | 只能由人完成 | `needs_human` / `not_run` / `failed` |

- 反向 ASR 只是一种证据：识别错误可能造成假阳性或假阴性，`failed` 时先回听确认再改。
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
| 画面复核 | 能看图时可写 `ai_reviewed`，仍提示用户确认 | 看图能力验证之前一律 `needs_human` |
| 交付 | 本地路径和报告 | 只返回计划、证据与 QA；审批、上传、回写由 Taskboard 完成 |

## 禁止事项

- 写出没有来源的时间，或用“大约”“左右”的时间代替词界。
- 为了得到唯一匹配而收窄搜索范围，或在多处候选中凭感觉挑一处。
- 跳过、合并或改写意见中的任何部分；把理由、诊断或状态写进 `source_text`。
- 用 `mute_word` 代替删除，用低能量区间冒充纯静音，或把下一句的词带进来凑停顿。
- 手改计划、回执或 MP4；绕过规划器直接用 FFmpeg 生成交付物。
- 把合成素材或合成 ASR 的测试结果写成真实素材的验收结论。

## 现行替代做法

查找某个短语的全部出现位置（源路径和 ASR 文件替换为实际值）：

```powershell
$env:PYTHONIOENCODING = 'utf-8'
python -c "from auto_cut_slicing.media import describe_source; from auto_cut_slicing.alignment import load_alignment, find_phrase_matches; s = describe_source('tmp/x/source.mp4'); a = load_alignment('tmp/x/source.words.json', s); print(find_phrase_matches(a, '我们再看一下', clip_start_us=0, clip_end_us=s['duration_us']))"
```

确认一个选帧时间是真实解码帧，并查看其后的像素变化候选：

```powershell
python -m auto_cut_slicing inspect-freeze tmp/x/source.mp4 --frame-time 31.04
```

`inspect-freeze` 只给像素变化候选（`semantic_verified: false`），画面内容仍需查看图片判断。

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
