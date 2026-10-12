---
name: auto-cut-slicing
description: 视频切片独立工作流：选取多条短片、有序拼接、句边界补全、按词级 ASR 删词或静音、按源时间删段落、静帧替换。默认输出可编辑剪映草稿；明确要求成片或 Taskboard 接入时批量生成 MP4。适用于本仓库 auto-cut-slicing，真实删除会压缩时间轴。
---

# Auto-Cut Slicing

在本独立仓库执行视频切片任务。从仓库根目录阅读 `docs/job-format.md` 并参考
`examples/slicing-job.json`，以 `python -m auto_cut_slicing` 使用维护命令。

确认素材位置、选取范围、输出组织和删除项。一个任务的 `outputs` 表达多条独立输出，
每条输出的 `clips` 表达有序拼接；相同素材可以多次使用。
未指定的精彩片段不要声称已自动识别。没有素材时先做不依赖素材的开发，不能捏造真实识别或验收。

删词先使用 `transcribe` 或用户提供的源文件哈希绑定词级 ASR。
保持 `source_text` 原文，稳定 ID 独立追溯。歧义匹配需补充词级证据或缩小搜索范围，
粗略时间不替代词边界。指定的 `must_keep` 需要验证保留。
删段落可用明确的源范围；每项时间都是原素材时间。

默认草稿流程先运行 `plan`，再运行 `draft`；单条明确导出可使用 `render`。
调用草稿桥接或真实 ASR 前运行 `doctor`，只使用锁定的 Auto-Cut Lite 源码依赖。
本流程不调用 Lite 审稿运行器，也不沿用 Lite 保留全部时长的切分语义。

明确要求成片、MP4 或 Taskboard 批次时，阅读 `docs/taskboard-contract.md`，参考
`examples/taskboard-request.json`，使用 `run request.json --result result.json`。
此通道直接输出 1920×1080、25 fps 的 MP4，无需生成草稿或导入上游草稿运行时。
同一原视频 ID/VMID 的多条要求复用同一 source；每条 outputs 独立生成成片，clips 顺序就是拼接顺序。
相对路径以请求文件目录为基准。保留稳定 ID、原始 instruction 或结构化 clips 和输出位置。
新请求默认 `boundary_policy: "sentence"`，结构化 clip 可覆盖；严格数字范围显式选择 `exact`。
普通数字起点严格落在句内时退到句首，终点延至句末；句内词尾或停顿仍补全当前句。
完整句首尾和句间空隙保持坐标。显式 `start_cue` 仍从唯一完整词元的语音起点开始，终点补全。
除源文件首尾外，待解析句边界需要源哈希绑定、含有效 `utterances` 的词级 ASR；
缺少完整证据须失败，不以粗略时间或静音猜句界。补全造成新的同源片段重叠在输出前失败，
原本明确重叠的素材复用仍允许。句首回退不平移显式删除/效果坐标，既有 cue 锚定定格仍跟随 cue。
批次中一条失败后继续其他输出；检查 JSON 回执逐条报告失败原因。恢复相同请求才使用 `--resume`，
成功成片需验证源/计划/文件哈希和完整解码；不能把存在的 MP4 当作成功，不能覆盖无关文件。
初始 pending 回执即保存请求默认策略，失败或重启后继续使用；缺少策略的历史回执按 `exact`
恢复并保持旧计划哈希。底层任务 clip 省略策略也为 `exact`，不把兼容规则套用于新请求。

未明确要求静音时，“剪掉某词”同时删除语音与画面。只有明确声音删除/停顿占位才使用 `mute_word`，
保留画面和时长。括号中的“首先/接下来”是 `start_cue`：以实际词开始，数字仅作搜索提示。
`exact` 的纯范围任务无需 ASR；cue、删词、静音、保护词以及 `sentence` 非容器边界均需对应 ASR 证据。
静帧替换保留同期音频，使用后续选定单帧持续至下一动画/内容边界。
`inspect-freeze` 给出实际解码帧和像素变化边界候选，`semantic_verified=False`；
必须查看选帧内容及边界，再把 source_sha256/frame_time/end 写入 visual_evidence。
像素检测不能证明画面只有 NaOH，缺证据或未知指令必须报错，不能省略修改要求。
复杂或尚不支持的自然语言应由代理明确编制结构化 clips；不得猜词边界或默默忽略操作。

报告输出路径、每条输出时长、哈希/解码结果、修改及失败回执；句策略检查 `boundary_adjustments`，
草稿还报告修改标签轨。`source_text` 保留原始码点，诊断字段单独记录。
ASR 句末标点是句边界候选证据；仅结构验收不能声称已试听、语义完整、剪映实际打开或完成全候选反向 ASR。
