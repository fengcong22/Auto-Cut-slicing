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

`kind=word` 需要 `text` 和词级 ASR；不允许从一句话的总体时间猜内部词边界。
若 `text` 只匹配 ASR 的一个多字词元中的部分字符，应使用更细的字符级 ASR，再执行删除。
`must_keep` 是可选保护短语列表。`kind=range` 使用明确的 `start/end`，不需要 ASR；
如指定保护词则同样需要 ASR 证据。不同删除项不能重叠。

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
