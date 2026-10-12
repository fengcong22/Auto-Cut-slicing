# 任务与词级 ASR 格式

任务 JSON 使用 UTF-8。路径相对任务文件所在目录解析。时间可为秒数、`MM:SS` 或 `HH:MM:SS`，
小数最多保留到微秒。内部统一为整数微秒。

```json
{
  "schema_version": 1,
  "name": "切片任务",
  "sources": [{"id": "s1", "path": "source.mp4", "asr": "source.words.json"}],
  "outputs": [{
    "id": "slice1",
    "name": "第一条切片",
    "clips": [{
      "id": "part1", "source": "s1", "start": 10, "end": 30,
      "deletions": [
        {"id": "d1", "kind": "word", "text": "就是说", "rough_time": 15,
         "search_radius": 3, "must_keep": ["观点"], "source_text": "删掉“就是说”，保留“观点”"},
        {"id": "d2", "kind": "range", "start": 20, "end": 24,
         "source_text": "删除20至24秒重复段落"}
      ]
    }]
  }]
}
```

`outputs` 中每个条目为一条独立输出，`clips` 中的顺序就是拼接顺序。删除均相对对应源素材，
不是输出时间。`source_text` 是修改标签原文，必须非空，禁止在写入时附加诊断状态。
输出名称必须是单层安全目录名。稳定 ID 用于追溯，不以标签文本代替 ID。
任务内每个 clip 的 `id` 必须唯一；同一 clip 内删除项与 `edits` 的 ID 也不能重复。

`kind=word` 需要 `text` 和词级 ASR；不允许从一句话的总体时间猜内部词边界。
若 `text` 只匹配 ASR 的一个多字词元中的部分字符，应使用更细的字符级 ASR，再执行删除。
`must_keep` 是可选保护短语列表。`kind=range` 使用明确的 `start/end`，不需要 ASR；
如指定保护词则同样需要 ASR 证据。不同删除项不能重叠。

## 片段边界策略

每个 clip 可声明 `boundary_policy`，只接受 `"exact"` 或 `"sentence"`。
底层 `schema_version: 1` 任务省略此字段时使用 `exact`，保留既有草稿与计划行为。
Taskboard 的新请求默认使用 `sentence`，结构化 clip 可覆盖该请求默认值。

| 策略 | 选取规则与证据 |
| --- | --- |
| `exact` | 保留数字 `start/end`；纯范围任务无需 ASR。语音起点、删词、静音及保护词仍需词级证据。 |
| `sentence` | 普通起点严格落在句内时退到句首，终点严格落在句内时延至句末；需要源哈希绑定、有效 `utterances` 的 ASR。 |

仅当 `sentence.start < t < sentence.end` 时补全边界。完整句的起止及句间空隙保持原坐标；
句内词尾、词间停顿都属于该句，不构成句边界。源坐标 `0` 和源时长无需句级证据。
显式 `start_cue` 保留唯一完整词元匹配的语音起点，`sentence` 仍补全片段终点。
缺少完整句级证据时失败，不按粗略时间、静音或未验证的标点猜句边界。

同一来源中原本不重叠的 clips 若因补全而重叠，在渲染或草稿写入前失败；
原本明确重叠的选取仍支持重复使用素材。删除与效果的显式坐标继续使用原素材坐标，
不会因句首回退而任意平移；下述已明确锚定 `start_cue` 的定格起点仍跟随语音起点。

`sentence` 计划的 `boundary_adjustments` 逐 clip 记录策略、请求与解析后的起止微秒，
实际补全证据包括对应句原文、完整词索引、源/ASR 哈希及识别来源。未改变边界时证据列表可为空。
`source_text` 保留原始码点，边界诊断单独记录；`exact` 不额外加入句边界字段，保持旧计划哈希兼容。

## 语音起点与 MP4 效果

`schema_version: 1` 同时支持以下片段字段。Taskboard 请求复用这一规划器，外层请求格式见
[Taskboard 契约](taskboard-contract.md)。

```json
{
  "id": "spoken-start",
  "source": "s1",
  "start": "00:30",
  "end": "03:40",
  "boundary_policy": "sentence",
  "start_cue": {"text": "首先", "search_radius": 3},
  "edits": [
    {"id": "mute1", "kind": "mute_word", "text": "中学阶段", "rough_time": "02:00",
     "search_radius": 3, "must_keep": ["核心观点"], "source_text": "剪掉“中学阶段”这几个字的声音"},
    {"id": "freeze1", "kind": "freeze_frame", "start": "00:30", "end": "00:35.600",
     "frame_time": "00:31", "source_text": "语音是“首先”配后面1s只有NaOH的画面"}
  ]
}
```

- `start_cue`：在名义 `start` 周围定位唯一连续完整词元的 ASR 匹配，默认搜索半径 3 秒。
  实际片段从该词精确源起点开始，可以早于或晚于名义时间。匹配及源哈希证据记录在 `start_cues` 中。
- `mute_word`：使用与删词相同的 ASR、唯一性和 `must_keep` 检查，将精确词区间静音，保留画面及原时长。
  `rough_time` 为可选锚点，`search_radius` 默认 30 秒。
- `freeze_frame`：在源 `[start,end)` 内以源 `frame_time` 的画面替换视频，音频继续，时长不变。
  定格范围必须位于片段内，选帧可以来自完整源文件的其他位置。
  若定格 `start` 正好等于片段的名义 `start` 且片段有 `start_cue`，定格起点跟随精确语音起点；
  原始与解析后的起点均保存在效果证据中。

删除先生成保留段，再把效果映射到保留段的目标时间轴。完全被删除、无保留区间的效果会报错。
同类效果不能重叠；未知字段、未知操作、无效坐标或空范围会在输出写入前失败。
`source_text` 原文保持不变，诊断状态独立存入回执。
规划后的 `effects` 包含源/目标范围、时长、稳定操作 ID 和定格帧坐标。

`mute_word` 与 `freeze_frame` 是 MP4 专用效果。`draft` 不支持这些效果并明确报错；
普通范围选择、删除与语音起点仍可生成可编辑草稿。

## ASR 输入

`transcribe` 命令输出此格式，手工导入其他识别器的词级结果时也须遵守来源绑定：

```json
{
  "schema_version": 1,
  "source_sha256": "当前源视频文件的SHA256",
  "provider": "实际提供方",
  "model": "实际模型或资源标识",
  "adapter": "转换适配器版本",
  "words": [
    {"text": "就是说", "start": 14.2, "end": 14.8},
    {"text": "观点", "start": 14.9, "end": 15.3}
  ],
  "utterances": [
    {
      "text": "就是说，观点。", "start": 14.2, "end": 15.3,
      "words": [
        {"text": "就是说", "start": 14.2, "end": 14.8},
        {"text": "观点", "start": 14.9, "end": 15.3}
      ]
    }
  ]
}
```

词必须按源时间排序、范围有效、不重叠。连续匹配不能跳过中间词，词间静音超过 1.5 秒会中断匹配。
`source_sha256` 绑定源视频字节，不能用识别音频的哈希替代。
上游 ASR 复用另外保存实际识别音频哈希和服务来源信息。凭据不写入任务、计划或报告。

句边界解析额外要求非空 `utterances`：每项含原始 `text` 与词级 `words`，文本内容须与词元一致，
按顺序展开后的词元文本及起止须与顶层已验证的 `words` 完全一致。
句边界由完整句末标点与完整词元共同确定，取该句首词起点和末词终点，不能用 utterance 总时间猜内部词边界。
语句可跨 utterances；小数点、缩写和省略号不能直接证明句末，句末也不能落在多字词元内部。
不完整或不一致的句级证据会失败。ASR 标点仅提供句边界候选证据，不代表人工语义或最终语音验收。
