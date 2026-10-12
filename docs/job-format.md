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

## 语音起点与 MP4 效果

`schema_version: 1` 同时支持以下片段字段。Taskboard 请求复用这一规划器，外层请求格式见
[Taskboard 契约](taskboard-contract.md)。

```json
{
  "id": "spoken-start",
  "source": "s1",
  "start": "00:30",
  "end": "03:40",
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
  ]
}
```

词必须按源时间排序、范围有效、不重叠。连续匹配不能跳过中间词，词间静音超过 1.5 秒会中断匹配。
`source_sha256` 绑定源视频字节，不能用识别音频的哈希替代。
上游 ASR 复用另外保存实际识别音频哈希和服务来源信息。凭据不写入任务、计划或报告。
