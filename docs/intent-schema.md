# 剪辑意图计划 · edit-intent v1

`edit-intent` 是 AI（或人工）理解剪辑意见后写出的计划。它只表达**选择**：保留哪些片段、
删除或静音哪几个词、从哪个词开始、用哪一帧覆盖。所有时间都必须引用可复核的来源，
包会按来源重新计算并比较，凭空写出的数字会被拒绝。执行仍由现有规划器和渲染器完成。

整体流程见 [AI 参与剪辑工作流](ai-editing-workflow.md)。机器可读定义：

- [edit-intent.v1.schema.json](../schemas/edit-intent.v1.schema.json)
- [evidence-manifest.v1.schema.json](../schemas/evidence-manifest.v1.schema.json)
- [qa-report.v1.schema.json](../schemas/qa-report.v1.schema.json)
- [intent-context.v1.schema.json](../schemas/intent-context.v1.schema.json)：命令读取的本地上下文
- 示例：[examples/edit-intent.json](../examples/edit-intent.json)

JSON Schema 只检查结构。本文的“校验规则”是语义要求，validator 必须全部执行。

## 实现状态

| 部分 | 状态 |
| --- | --- |
| 本文、JSON Schema、示例 | 已定稿；`auto_cut_slicing/schema.py` 按仓库内 schema 校验，不依赖第三方包 |
| `intent validate` / `intent compile` 命令 | 已实现（`auto_cut_slicing/intent.py`），执行 V01–V14 |
| 规划器按 `word_indices` 定位 | 不改规划器：编译时用 `text` + `rough_time` + 收窄的 `search_radius` 让现有唯一匹配落到选定的那一处，再由 V14 核对 |
| `audio_point` / `gap_choice` 时间来源 | 已实现：引用 `evidence audio-gap` 的测量结果 |
| VM/PMO 协议携带意图 | 包侧已实现（`vm_pmo` schema v3），manifest 已声明 v3；Taskboard v3 driver 已接入（feishu-codex-Taskboard #25、#49），真实测试记录上的端到端验收未做，见 [v3](vm-pmo-offline-contract-v3.md) |

不得把“未实现”的命令写成已执行，也不得声称 validator 已经校验过某个意图文件。

## 文件关系

```text
evidence/manifest.json      ← 证据工具产出，逐个文件绑定源 SHA-256
        │ evidence_id
edit-intent.json            ← AI 写出：意见覆盖 + 片段/操作 + 时间来源
        │ intent compile（或手工映射）
job / request clips         ← 现有格式，见 job-format.md / taskboard-contract.md
        │ plan / run / vm_pmo plan
plan.json → MP4 → qa.json   ← qa-report 绑定 intent、plan 与成片哈希
```

所有时间单位为整数微秒（`*_us`），坐标均为**源时间**，与规划器一致。
审核轮次的“源”是冻结的 parent MP4（`timeline_basis: previous_review_video`）。

## 时间来源

每个时间值写成一个 `time_ref`，同时给出 `value_us` 便于阅读；validator 按来源重算，不相等即拒绝。

| `ref` | 含义 | validator 如何复核 |
| --- | --- | --- |
| `user_anchor` | 用户在意见中写出的时间，例如“0:31” | 取 `raw_text[span]` 重新解析，必须得到 `value_us` |
| `word_edge` | ASR 某个词的起点或终点 | `alignment_sha256` 与当前对齐一致，`index` 处词文本等于 `text`，取 `edge` 得到 `value_us`。若声明 `claim: sentence_end/sentence_start`，还要用句界模块确认 |
| `frame` | 真实解码帧的源时间戳 | `source_sha256` 与源一致，`value_us` 出现在 `evidence_id` 对应条目的 `frame_times_us` 中 |
| `source_edge` | 源文件开头或结尾 | `0` 或源时长 |
| `audio_point` | audio-gap 测得的衰减结束、低能量区间或下一词起点 | 证据文件哈希与清单一致，`value_us` 等于其中同名测量值 |
| `gap_choice` | 在已测得的窗口内选定的切点 | `[lower_us, upper_us]` 必须等于证据中的 `out_window`（出点）或 `in_window`（入点），值在窗口内；`user_duration` 的值由时长算出，见下 |

`gap_choice` 是唯一允许 AI 在区间内自行取值的来源，只用于尾音、气口和语义停顿的长度。

audio-gap 的窗口：
- `out_window = [decay_end, next_onset − 40ms]`：出点保留上一词完整衰减，且不进入下一词起音前一帧。
- `in_window = [decay_end + 40ms, next_onset]`：入点不带上一词残音，也不切掉下一词起音。

`basis: user_duration` 时，从 `duration_span` 解析唯一时长 d（`1s`、`0.6秒`、`一秒`、`1s左右` 都可），
出点取 `上一词 ASR 终点 + d`，入点取 `next_onset − d`，再夹到窗口内；值必须等于这个结果，不能另选。
它的区间来自测量，区间之外不可取值；成片 QA 中对应的 `pacing` 检查只能是 `needs_human`。

## 意见覆盖

`coverage` 把每条 `raw_text` 切成若干 `span` 并说明处理方式，是意见遗漏检查的依据。

| `disposition` | 用于 | 结果 |
| --- | --- | --- |
| `operation` | 被某个片段或操作执行的内容，`intent_ids` 至少一项 | 继续 |
| `constraint` | 限定性要求，如“其他不变”“不要动画面” | 继续；QA 的 `unlisted_change` 检查据此判断有没有多改 |
| `context` | 不需要执行的说明，如称呼、礼貌用语、对上一稿的评价 | 继续；`reason` 必填 |
| `unsupported` | 理解了但当前能力做不到，如插入纯静音 | 阻断 |
| `needs_evidence` | 需要的证据不存在，如没有 ASR、缺少可用帧 | 阻断 |
| `ambiguous` | 有多种合理理解或多处候选，且意见没有给出区分依据 | 阻断 |

覆盖规则：
1. `quote` 必须等于 `raw_text[start:end]`。
2. 把空白和标点之外的每个字符都计入，必须至少被一个 span 覆盖。span 可以重叠。
3. 有 `unsupported`、`needs_evidence` 或 `ambiguous` 时，`status` 必须为 `blocked`，
   并在 `blockers` 中写出对应代码、span 和缺失的证据。
4. 每个片段和操作的 `covers` 必须指向 `disposition: operation` 的 span；
   每个 `operation` span 的 `intent_ids` 必须都真实存在。没有任何意见依据的操作不允许出现。

## 重复语句与候选选择

`word_span` 用词序号闭区间指出选中的那一处，`selection` 说明为什么是这一处。
validator 在 `[scope_start_us, scope_end_us)` 内重新查找 `text` 的全部完整词元连续匹配
（规则同 [job-format](job-format.md) 的删词匹配），结果必须与 `candidates` 完全一致，
选中的那一处必须在其中，且 `text` 等于对应词的规范化拼接。

`scope` 不能为了制造唯一而随意收窄。两端只能取：本片段的保留边界、源首尾，或意见中 `user_anchor` 的时间。

| `method` | 允许的条件 | validator 复核 |
| --- | --- | --- |
| `unique_in_scope` | 范围内只有一处 | `candidates` 恰好一项 |
| `user_anchor` | 意见写了时间，且该处是半径内唯一的候选 | 半径内候选恰好一项，且就是选中项。半径上限：语音起点 3 秒，删词与静音 30 秒（与现有默认一致） |
| `user_ordinal` | 意见写了“第二遍”“最后一次”等序数 | 候选按起点排序后取第 `ordinal` 个；`ordinal_from_end` 为真时从末尾数。`ordinal_span` 必须能解析出同一序数（阿拉伯数字、中文数字、“最后/末”），解析不了就阻断 |
| `user_context` | 意见引用了相邻的上下文，如“‘我们再看一下这个实验’里的‘再看一下’” | `context_text` 等于 `raw_text[context_span]`，在范围内唯一匹配，且包含选中项 |

以上条件都不满足时，不能由 AI 挑一处：写 `INTENT_AMBIGUOUS_MATCH`，把全部候选
（序号、源时间、前后各约 10 个词的上下文）放进 `blockers[].candidates`。
聊天入口可以把候选交给用户选择；用户的回答作为新的补充意见记录，再按 `user_context` 或 `user_ordinal` 引用。

找不到任何匹配时写 `INTENT_NO_MATCH`。ASR 把目标切进一个多字词元内部时属于证据不足，
写 `INTENT_EVIDENCE_MISSING`，不能按字数分摊时间。

## 片段与操作

`outputs[].clips` 的顺序就是拼接顺序。每个片段：

- `start.mode`
  - `time`：从一个时间来源开始，通常是 `word_edge`。
  - `cue`：从一个完整词或短语的语音起点开始；可加 `visual_anchor`，语音起点与画面帧相差不得超过 0.5 秒。
  - `sentence`：退到包含 `at` 的句首。只在用户确实要整句时使用；按
    [剪辑准则](editing-standards.md) 优先就近的完整词或短语，不为凑整句带入引入语。
- `end.mode`
  - `time`：在时间来源处结束。
  - `sentence`：补到包含 `at` 的句末。
- `operations`：片段内的删除、静音和定格。
  - `delete_phrase`：音画同时删除，时长缩短。
  - `mute_phrase`：只静音，保留画面和时长；仅当意见明确只删声音时使用。
  - `delete_range`：按两个时间来源删除一段。
  - `freeze_cover`：在 `[start,end)` 内用 `frame` 的画面覆盖，同期声音继续，时长不变。
    `purpose: clean_cut_in` 用后面的干净新画面清理残留；`semantic_pause` 用前面的收尾画面配合保留的源尾段做停顿。
    `visual_review` 写明看过的证据和观察结果；没有看图能力时状态为 `needs_human`。
    剪辑准则要求每个开头和接缝都清理前一主题残留，这类定格不一定出现在意见文字里：
    此时 `covers` 可以为空，但必须写 `standard_rule: "clean_cut_in"` 且 `purpose: "clean_cut_in"`，
    `source_text` 取所在片段那条意见的原文。删除、静音和其他操作仍必须对应意见文字。
- `must_keep`：保护短语，来源是意见中的 span，或 Taskboard 输入中的 `must_keep` 字段。

`rationale` 写理由，只进入意图文件和回执，**不写进** `source_text`。

## 编译映射

`intent compile` 实现之前按此表手工转换；实现之后由命令执行，并在计划回执中保存意图文件哈希。

| 意图 | 现有 clip 字段 |
| --- | --- |
| 片段 | `id = intent_id`，`source = source_id`，`boundary_policy: "exact"` |
| `start.mode: time` | `start = at.value_us` |
| `start.mode: cue` | `start = 选中词起点`，`start_cue.text = phrase.text`，`search_radius` 收窄到只包含选中项（小于它到最近其他候选距离），上限 3 秒；有画面锚点时 `start_cue.visual_anchor = 帧时间`，半径不超过 0.5 秒 |
| `start` / `end` 为 `sentence` | 先用句界证据解析成具体的 `word_edge`，再写成 exact 的 `start` / `end`，回执中保留解析依据 |
| `end.mode: time` | `end = at.value_us` |
| `delete_phrase` | `deletions[]`：`kind: "word"`，`text`，`rough_time = 选中词起点`，收窄的 `search_radius`，`must_keep` |
| `mute_phrase` | `edits[]`：`kind: "mute_word"`，其他字段同上 |
| `delete_range` | `deletions[]`：`kind: "range"`，`start` / `end` |
| `freeze_cover` | `edits[]`：`kind: "freeze_frame"`，`start` / `end` / `frame_time` |
| 所有操作的 `source_text` | 该操作 `covers` 所属那条意见的完整 `raw_text`，原样保留码点 |

一个操作的 `covers` 只能来自同一条意见，以保证 `source_text` 唯一；片段可以覆盖多条意见。
收窄后的半径若不能大于 0（两处候选起点相同），按证据不足阻断。

## 校验规则

validator 按顺序执行，任何一条不满足都在写计划前失败，不生成 MP4。编号供测试引用。

| 编号 | 规则 |
| --- | --- |
| V01 | 文件通过 JSON Schema；未知字段拒绝 |
| V02 | `raw_text` 与输入中的原意见逐码点相同，`raw_text_sha256` 为其 UTF-8 字节的哈希 |
| V03 | `bindings` 中的源哈希、时长、对齐哈希与当前文件一致；`evidence_manifest_sha256` 与清单文件一致 |
| V04 | 所有 `evidence_id` 在清单中存在，且清单条目的 `source_sha256` 与所引用的源一致 |
| V05 | 覆盖规则 1–4 全部满足；`covers` 为空的操作只能是带 `standard_rule` 的 clean_cut_in 定格 |
| V06 | `status: proposed` 时没有阻断性 disposition，也没有 `blockers` |
| V07 | 每个 `time_ref` 按“时间来源”表重算后等于 `value_us` |
| V08 | 每个 `word_span` 的候选集合与选择依据按“重复语句与候选选择”复核通过 |
| V09 | 片段满足 `0 ≤ start < end ≤ 源时长`；操作位于所在片段内；同类操作不重叠 |
| V10 | `cue` 的画面锚点与语音起点相差不超过 500 000 µs |
| V11 | `freeze_cover` 的帧来自同一源的帧证据；`visual_review.evidence_ids` 都存在 |
| V12 | `must_keep` 都能在片段内找到，且不与任何删除或静音重叠 |
| V13 | `intent_id` 在全文件内唯一 |
| V14 | 编译后的 clips 通过现有规划器，且规划器解析出的每个词区间、cue 起点与意图中选中的词序号一致 |

V14 是最后一道核对：即使手工编译，也要对照计划中的 `edits`、`start_cues`、`effects` 确认落在选中的那一处。

## 阻断代码

| 代码 | 含义 |
| --- | --- |
| `INTENT_UNCOVERED_TEXT` | 意见中有内容没有被任何 span 覆盖 |
| `INTENT_AMBIGUOUS_MATCH` | 多处候选，意见没有给出区分依据 |
| `INTENT_NO_MATCH` | ASR 中找不到要求的词或短语 |
| `INTENT_TIME_UNSOURCED` | 某个时间没有合法来源，或重算不一致 |
| `INTENT_EVIDENCE_MISSING` | 缺少 ASR、帧证据、句界证据或更细的词元 |
| `INTENT_EVIDENCE_STALE` | 证据绑定的源、对齐或清单哈希与当前不一致 |
| `INTENT_UNSUPPORTED_OPERATION` | 意见要求当前能力做不到的操作 |
| `INTENT_CONFLICT` | 意见之间或与 `must_keep` 互相矛盾 |
| `INTENT_NEEDS_USER_DECISION` | 需要用户在几种合理方案中选择，例如停顿是否值得延长片段 |

阻断时同样写出完整的 `edit-intent.json`（`status: blocked`），已经理解的部分照常记录，便于下一轮补充证据后继续。

## 当前不支持的要求

以下要求应标记为 `unsupported` 并阻断，不能用近似操作代替：

- 插入源中不存在的静音或停顿（`freeze_frame` 不增加时长；只能保留已有的源尾段）。
- 变速、倒放、转场特效、字幕、贴图、调色、音量包络、背景音乐。
- 跨源素材（VM/PMO 每个请求只有一个 source；本地批量请求也只有一个 source）。
- 草稿通道中的 `mute_phrase`（MP4 专用）。`freeze_cover` 已可写入草稿，见 [job-format](job-format.md)。
- 依赖画面内容识别、但没有可看的帧证据又没有人工复核的判断。

## 版本

`schema_version` 只增不改。新增可选字段不升版本；改变已有字段含义、时间来源或校验规则时升为 v2，
同时保留 v1 的 validator，历史意图文件按其版本复核。
