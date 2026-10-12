# 句边界补全实施计划

本文件记录句界机制的历史实施。现行素材剪辑判断以 [剪辑经验与交付准则](../editing-standards.md)
为准：起点围绕标注时间选择完整词/短语并用 cue 固化，不能把默认句首回退直接当成交付决定。

**目标：** 新 Taskboard 切片默认保留完整句首尾，显式语音 cue 与源时间编辑保持准确，旧任务可按原计划恢复。

**架构：** 在同一维护规划器中增加源哈希绑定的句边界解析；Taskboard 只负责默认策略、clip 覆盖及回执持久化。
句界由 utterance 原文的完整句末与已验证完整词元共同提供，不引入新的渲染器或上游运行时。

**技术：** Python 3.10–3.12、pytest、FFmpeg / FFprobe。

**契约：** [任务格式](../job-format.md)、[Taskboard 契约](../taskboard-contract.md)、[验收说明](../validation.md)。

## 实施约束

- 策略仅接受 `sentence` / `exact`；新 Taskboard 请求默认 `sentence`，低层 clip 省略策略仍为 `exact`。
- 数字起止仅在 `sentence.start < t < sentence.end` 时补全，词尾及句内停顿属于该句，完整句边缘及句间空隙不移动。
- `start_cue` 使用唯一完整词元起点；句策略仍补全终点。显式编辑源坐标不随句首回退平移，已有 cue 锚定定格保持原规则。
- 非容器边界要求完整、源哈希绑定的 utterance/word 对应证据；不得猜边界或把 ASR 标点视为人工语义验收。
- 新增重叠、未知策略、不完整或不一致证据在输出写入前失败；原本明确重叠仍支持。
- 保留 `source_text` 原始码点。新策略从初始 pending 回执持久化；历史回执缺少策略按 `exact` 恢复，旧计划哈希不变。
- 完整源文件、同步可编辑保留段、真实 ripple 删除、上游身份校验及忽略目录规则继续适用。

## 实施与核验

- [x] `auto_cut_slicing/boundaries.py` 与 `tests/test_boundaries.py`：校验 utterance 原文/词元及全局词序列、源/ASR 哈希；以完整词边界解析句末，覆盖小数、缩写、省略号、跨 utterance 和未闭合尾句。
- [x] `auto_cut_slicing/planner.py` 与 `tests/test_sentence_planner.py`：增加 clip 策略和 `boundary_adjustments`，验证词尾/停顿补全、精确边缘/间隙不变、cue 优先、编辑坐标不移动及新增重叠拒绝。
- [x] `auto_cut_slicing/batch.py` 与 `tests/test_batch.py`：支持请求默认/clip 覆盖；初始 pending 保存默认策略，覆盖编译失败与中断恢复，以及无策略旧回执的原计划哈希恢复。
- [x] 同步 README、任务/Taskboard/验收文档及 canonical 技能；运行 `python scripts/install_repo_skills.py` 同步仓库本地技能副本。
- [x] 运行 `python -m pytest -q`、`python -m ruff check .`、`python scripts/smoke_test.py` 与 `git diff --check`，实际结果见验收说明：完整回归 300 项通过。
- [x] 使用真实 NaOH 素材重新规划与渲染：语音起点 30.510 秒不变，220.000 秒句内终点补至 220.840 秒，保留范围 `[30.510,220.840)`、计划时长 190.330 秒；复核原文、定格、同期音频、哈希、时长与完整解码。
- [x] 单独记录真实末句试听、所选画面语义和目标应用打开验收的状态：选帧内容已检视；本轮未完成人工试听、目标应用打开或 Taskboard 宿主联调。结构、合成检查及反向 ASR 不替代人工验收。

独立代码与真实交付审查已完成；最终提交及工作区状态以 Git 记录为准。

本计划中的核验项是实施目标；勾选或宣布完成须有实际命令和素材证据。
