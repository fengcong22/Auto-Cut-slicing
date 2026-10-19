# VM / PMO 离线剪辑协议 v1

本协议定义 schema v1 的 initial/parentless 范围，仍由 Taskboard 初稿阶段兼容使用；它不支持审核 parent 或语义删词。Taskboard 的生产资格、审批下载、审核轮次和 PMO 附件交付由本机 Bridge/Taskboard v2 workflow 单独控制。剪辑包本身始终只执行本地 `plan` / `render`，不接触飞书和 VM 凭据。

```powershell
python -m auto_cut_slicing.vm_pmo plan --input C:\run\input.json --context C:\run\execution-context.json
python -m auto_cut_slicing.vm_pmo render --input C:\run\input.json --context C:\run\execution-context.json --plan C:\run\plan.json
```

所有参数路径必须为绝对路径。输入和 context 必须固定为 `run_dir/input.json`、`run_dir/execution-context.json`。stdout 是单行 UTF-8 JSON，合法的 `ready` / `rendered` / `blocked` 回执退出码为 0；协议或进程异常返回 `failed` 和非零退出码。`blocked` 是有效业务结果，由宿主读取后停止该轮；非零退出不能登记业务成功。`plan` 写 `plan.json`、`plan-result.json`，`render` 写 `result.json` 和唯一 MP4。JSON 回执使用原子无覆盖发布；已存在目标不会被接纳或覆盖。`input_sha256` / `plan_sha256` 是对应 JSON 文件的**原始字节** SHA-256。

## 输入

`input.json` 顶层及各对象严格接受以下字段，不接受额外字段。示例值仅说明形状；哈希、大小和时长须来自本轮真实文件。

```json
{
  "protocol": "vm_pmo_review_video",
  "schema_version": 1,
  "package": {"alias": "slicing", "name": "auto-cut-slicing", "version": "0.3.0", "manifest_sha256": "PACKAGE-MANIFEST.json 文件字节的 64 位小写 SHA-256"},
  "binding": {"task_id": "task-1", "run_id": "run-1", "subject_key": "subject-1", "config_version": "config-1", "stage_id": "initial", "event_id": "event-1"},
  "record": {"base_token": "base-1", "table_id": "table-1", "record_id": "record-1"},
  "review_round": {"round_id": "round-1", "input_snapshot_id": "snapshot-1", "parent": null},
  "source": {"source_id": "source-1", "timeline_basis": "vm_original", "vmid": "vm-1", "sha256": "源视频文件字节的 64 位小写 SHA-256", "bytes": 123456, "duration_ms": 1000},
  "instructions": [{"instruction_id": "instruction-1", "source_field_id": "field-1", "raw_text": "0:00-0:00.20+0:00.40-0:00.60", "timeline_basis": "vm_original", "applicability": "current_round"}],
  "output_profile": {"id": "review_mp4_v1", "width": 1920, "height": 1080, "fps": 25, "audio": "preserve"},
  "delivery_target": {"attachment_field_id": "attachment-1"}
}
```

首版只接受 `binding.stage_id: "initial"`、`parent: null`、**一条**当前轮的 `vm_original` 意见，以及毫秒精度的纯确切时间范围。多个范围按原文顺序拼接，允许倒序或重复使用素材。不能包含语音 cue、删词、静音、画面替换或额外自然语言。任何未支持的操作或多条意见都返回 `blocked`，在 `instruction_results` 中保留每条原文；不会简化意见后继续渲染。源必须有音轨，输出固定等比补边 1920×1080、25 fps、AAC 音频。源时长是 ffprobe 容器时长四舍五入至毫秒，须与 `source.duration_ms` 相同。

`execution-context.json` 严格接受以下字段：

```json
{
  "protocol": "vm_pmo_review_video",
  "schema_version": 1,
  "input_sha256": "input.json 文件字节的 64 位小写 SHA-256",
  "run_dir": "C:\\run",
  "source_path": "C:\\media\\source.mp4",
  "plan_path": "C:\\run\\plan.json",
  "plan_result_path": "C:\\run\\plan-result.json",
  "result_path": "C:\\run\\result.json",
  "output_path": "C:\\run\\review.mp4"
}
```

路径必须是规范绝对路径，不能经过符号链接；`run_dir` 必须已存在。输入、context、源视频、计划和 ready 回执在读取前均须是普通文件且只有一个链接，符号链接、硬链接和多链接别名均被拒绝。输入及 context 固定在本轮 `run_dir`；源可位于独立受信采集目录。计划、回执和 MP4 目标必须是 `run_dir` 中互不相同的路径。源文件按 SHA-256、字节数、时长和音轨校验，不得以 PMO 字段值决定本机路径。

VM 源的每次探测、复核和渲染均固定使用 FFmpeg 的 MOV/MP4 解复用器，仅允许 `file` 协议，并禁用外部 data reference。清单（例如 ffconcat）和通过外部文件引用扩展实际素材的源均不能作为本轮 VM 视频；校验范围必须覆盖实际渲染的文件字节。此限制只用于 VM / PMO 适配器，既有通用 CLI 的默认输入行为保持不变。

## 计划和回执

`plan.json` 精确字段：`protocol`、`schema_version`、原样 `package`、`binding`、`record`、`review_round`、`source`、`delivery_target`、`input_sha256`、`source_sha256`、`instruction_results`、`segments`、`operations`、`expected_duration_ms`、`engine_plan_sha256`。每条 `instruction_results` 为 `{instruction_id,source_field_id,raw_text,status,segments}`；已解决时 `status: "resolved"`，`segments` 为片段 ID 列表。每个 `segments` 元素为 `{segment_id,source_id,start_ms,end_ms}`，按最终播放顺序排列。首版 `operations: []`，`expected_duration_ms` 是各片段时长之和。`engine_plan_sha256` 绑定维护中的切片规划器编译结果。

`plan-result.json` 精确字段：`protocol`、`schema_version`、原样 `package`、`binding`、`record`、`review_round`、`delivery_target`、`input_sha256`、`source_sha256`、`status`、`plan_sha256`、`instruction_results`、`error`。`ready` 时 `plan_sha256` 为 plan.json 字节哈希、`error: null`；`blocked` 时 `plan_sha256: null`、`error: {code,message}`，不发布 plan.json。输入或 context 自身损坏到无法取得可信路径时，只返回 stdout 错误，不写不可信路径。

`render` 重新校验输入、context、源文件和计划，重新编译并对照 `plan.json` 及 ready 回执；任何身份、字节、计划或素材变化均阻断，不发布 MP4。`result.json` 字段为 `protocol`、`schema_version`、原样 `package`、`binding`、`record`、`review_round`、`delivery_target`、`input_sha256`、`source_sha256`、`status`、`plan_sha256`、`instruction_results`、`artifacts`、`error`。`rendered` 时 `artifacts` 恰一条：`{kind:"review_mp4",path,sha256,bytes,duration_ms,width,height,fps,has_audio,full_decode_passed}`；失败时为空数组。`status` 为 `rendered`、`blocked` 或 `failed`，后两者带 `{code,message}`。`rendered` 只证明本地成片及媒体校验，不表示人工试听、语义画面验收或 PMO 上传完成。

计划和结果只针对单个 task/run/record/round，不复用上一轮的成功声明。独立任务可以复用同一获批 VM 源文件，但各自拥有独立的运行目录、计划、回执和成片。真实素材试听、画面语义、审核 parent 和附件读回仍须另行联调。
