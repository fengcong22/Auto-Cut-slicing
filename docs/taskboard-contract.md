# Taskboard 本地 MP4 契约 · v1

Taskboard 通道从一个本地源视频生成多条独立 MP4，固定 **1920×1080、25 fps**，
按原比例缩放并居中补边。保留源音频内容和时序，只有明确删除/静音改变相应区间；音频编码为 AAC。
输入、结果和媒体通过本地文件交接。

| Taskboard 接口 | 本包交接方式 |
| --- | --- |
| 输入 | 请求 JSON：本地视频路径、每条保留范围/指令、拼接顺序。 |
| 输出 | `output_path` 指定位置的独立 MP4。 |
| 结果 | stdout 最终 JSON 与 `--result` 回执：逐条成功/失败及成片路径。 |

Taskboard 需要能在素材所在机器启动上述命令并读取回执；实际宿主调用能力仍待联调。

当前串行离线渲染优先保证画面和音频时间准确。静音等效果的后续画面分片会从保留片段起点
重新解码，以保持不同源帧率的转换相位；定格选帧会暂存所选帧之前的一段解码画面。
长片段包含大量效果时会增加运行时间和内存，尚未针对该规模做性能验收。

## 调用

```powershell
python -m auto_cut_slicing run request.json --result tmp/result.json
python -m auto_cut_slicing run request.json --result tmp/result.json --resume
```

stdout 仅输出最终 JSON；诊断使用 stderr。所有输出成功时退出码为 0，任何失败时为 1。
JSON 内路径相对请求文件所在目录解析；命令行 `--result` 相对当前目录解析。
保存请求和结果时使用 UTF-8，素材及生成结果放在忽略目录中。

## 请求

```json
{
  "contract_version": 1,
  "request_id": "lesson-001",
  "source": {
    "id": "lesson-source",
    "vmid": "video-001",
    "path": "../tmp/input/lesson.mp4",
    "asr": "../tmp/input/lesson.words.json"
  },
  "outputs": [
    {
      "id": "joined",
      "name": "两段拼接",
      "instruction": "0:04-1:23 +1:31（接下来）-2:29",
      "output_path": "../output/taskboard/joined.mp4"
    },
    {
      "id": "opening",
      "name": "开场",
      "clips": [{"id": "opening-part", "start": 4, "end": 83}],
      "source_text": "保留4秒到1分23秒",
      "output_path": "../output/taskboard/opening.mp4"
    }
  ]
}
```

| 字段 | 规则 |
| --- | --- |
| `contract_version` | 必须为整数 `1`。 |
| `request_id` | 非空稳定请求 ID，恢复时必须一致。 |
| `source.id` / `source.path` | 必填；源 ID 与本地视频路径。 |
| `source.asr` | 可选；语音起点、删词、静音词或保护词需要源文件哈希绑定的词级 ASR。 |
| `source.vmid` | 可选来源标识。不同显示标题可以复用同一 VMID、`source.id` 和源文件。 |
| `outputs` | 非空列表，各 `id` 必须唯一。 |
| `output.id` / `output.output_path` | 必填；稳定输出 ID 与独立 `.mp4` 目标路径。 |
| `output.name` | 可选展示名称，文件位置由 `output_path` 决定。 |
| `output.instruction` / `output.clips` | 必须且只能声明其中一个。 |
| `output.source_text` | 结构化 `clips` 可附带的整体原文；操作项仍需各自的 `source_text`。 |
| `output.visual_evidence` | 复杂画面参考句式需要 `{source_sha256,frame_time,end}`。 |

结构化 `clips` 按列表顺序拼接，每项必须声明稳定 `id`、`start`、`end`。
`source` 可省略，默认本请求的 `source.id`；如声明必须引用该来源。时间使用数值秒或
`MM:SS` / `HH:MM:SS`，精确到微秒。片段、删除、语音起点和效果字段见
[任务格式](job-format.md)。源时间坐标始终相对完整原视频。

未知字段、未知操作、空/越界范围、歧义语音和保护词冲突会失败。相同目标路径及与输入文件冲突的
目标会失败，其他有效输出可以继续。请求版本、重复输出 ID 等外层错误在渲染前结束整个请求。

## 保守指令格式

| 参考原文 | 编译行为 |
| --- | --- |
| `0:00-2:31;2:34-结束` | 按顺序保留两个源范围，`结束` 展开为源时长。 |
| `0:00-4:56+6:14-6：40` | `+` 有序拼接，兼容全角结构标点。 |
| `0:04-1:23 +1:31（接下来）-2:29` | 第二段从“接下来”的精确 ASR 起点开始。 |
| `3:50（首先）-6:05` | 名义 3:50 周围定位“首先”，默认半径 3 秒。 |
| `0:31 剪掉“上一章”三个字，其他不变` | 保留完整源范围，精确删词，音视频同时删除并压缩时长。 |
| `0:06-（4:22 剪掉 “中学阶段”这几个字的声音，可以用停顿占位）-4:34` | 只静音精确 ASR 词区间，画面和总时长保留。 |

时间锚点不替代词边界。引号中的原话和完整指令码点保持不变；原始 `instruction` 会存入结果的
`source_text`，每项编辑也保留原文。无法识别的附加文字会报错。
复杂画面自然语言支持参考 NaOH 句式，不能用任意自然语言推断完整画面剪辑。

## 画面证据

参考请求 `0:30（语音是“首先”配后面1s只有NaOH的画面）-3:40` 必须附带：

```json
{"source_sha256": "所选源视频的64位SHA256", "frame_time": 31, "end": 35.6}
```

`frame_time` 指定所选源画面，`end` 指定替换结束源坐标。替换从解析后的“首先”起点开始，
画面定格，原音频继续；缺失或不匹配的证据使该输出失败。
哈希示例是说明占位，实际必须填写真实源 SHA-256。

```powershell
python -m auto_cut_slicing inspect-freeze tmp/input/lesson.mp4 --frame-time 31
python -m auto_cut_slicing inspect-freeze tmp/input/lesson.mp4 --frame-time 31 --end 35.6
```

分析要求 `frame_time` 是实际解码帧时间戳，不会悄悄改选邻帧。
未给定 `--end` 时，提出下一处明显相邻灰度像素变化的边界候选；无候选时提出源结束。
给定 `--end` 时保留该明确边界。结果带 `boundary_evidence`、`limitations`，并始终声明
`semantic_verified: false`。像素差异不证明所选帧“只有 NaOH”，也不证明下一段动画的语义边界。
应检视选帧与所需边界后，再将分析结果中的 **`source_sha256`、`frame_time`、`end` 三字段**复制到请求。
请求的 `visual_evidence` 不接受分析结果的其他诊断字段。

## 结果与恢复

合法请求的结果文件逐步原子保存，包含：

| 范围 | 字段与含义 |
| --- | --- |
| 整体 | `contract_version`、`request_id`、`request_sha256`、`source_sha256`、`ok`、`status`、`outputs`。 |
| 每条输出 | `output_id`、展示 `name`、原始 `source_text`、目标 `path`、`status`。结构化输入保留 `clips`。 |
| 编译成功 | `plan_sha256`、`segments`、`edits`、`effects`、`start_cues`、`expected_duration_us`。 |
| 输出成功 | `sha256`、实际 `duration_us`、`duration_tolerance_us`、`width`、`height`、`fps`、`has_audio`、`full_decode_passed`。 |
| 输出失败 | `error: {code,message,retryable}`；可根据错误定位缺失证据、范围、路径或渲染问题。 |
| 独立成功证据 | `completion_receipt` 保存已经成功发布的成片证据，后续暂时复检失败仍保留。 |

批量终态为 `complete`、`partial_failure` 或 `failed`，只有 `complete` 时 `ok: true`。
执行中整体为 `in_progress`，单条可能为 `pending`、`ready`、`complete` 或 `failed`。
完整解码与时长检查不代表试听验收，`spoken_final_acceptance` 保持 false。

首次执行拒绝已有结果文件。`--resume` 必须找到相同请求 ID、请求内容哈希和输出清单的已有结果；
源视频哈希须一致。已完成输出再次编译后，校验路径、计划哈希、成片哈希、1080p25 格式、音轨、
时长和完整解码。独立成功证据确保一次暂时故障不会抹掉之前的成功身份。
首次无法读取源文件且没有任何成功成片时，恢复可以建立初始源哈希。
未成功输出可以重新尝试；目录中已有但没有匹配成功回执的 MP4 不会被视为成功，也不会被覆盖。
结果路径必须与请求、素材、ASR 及所有 MP4 目标分开。
同一结果文件由一个进程串行执行。若中断发生在 MP4 发布后、成功回执保存前，该文件缺少已保存的
成功证据，会返回 `OUTPUT_EXISTS`；需核查并移开该文件再重试，程序不会自动接纳它。

常见错误码如下；`message` 给出具体定位信息：

| 错误码 | 含义 |
| --- | --- |
| `INVALID_REQUEST` / `INVALID_OUTPUT` / `INVALID_RANGE` | 外层契约、单条指令或时间范围无效。 |
| `ASR_REQUIRED` / `ASR_INVALID` | 缺少词级证据，或证据来源、匹配/保护检查未通过。 |
| `VISUAL_EVIDENCE_REQUIRED` / `VISUAL_EVIDENCE_INVALID` | 缺少静帧证据或证据不绑定当前源文件。 |
| `SOURCE_INVALID` / `SOURCE_CHANGED` | 源素材不可用或已记录的源字节/媒体属性变化。 |
| `PATH_CONFLICT` / `OUTPUT_EXISTS` / `RESULT_EXISTS` | 文件位置冲突或拒绝覆盖已有目标。 |
| `RESUME_MISMATCH` / `OUTPUT_CHANGED` | 恢复请求不匹配，或已成功成片的映射、字节、格式/解码变化。 |
| `RENDER_FAILED` | 渲染执行失败，可以检查环境后重试。 |

`retryable` 是执行故障提示；它不授权覆盖文件或跳过证据校验。外层错误仍返回 JSON 和非零退出码，
但可能尚未创建结果文件。
