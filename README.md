# Auto-Cut Slicing · 视频切片剪辑包

`0.3.0` 独立视频切片工作流：多条独立输出、指定顺序拼接、精确删词和删段落。
默认产品为可编辑剪映草稿；明确选择 Taskboard MP4 通道时，直接生成 **1920×1080、25 fps** 成片，
按原比例缩放并补边。单条失败会记录原因并继续其他输出。

## 项目级剪辑经验

开始每次剪辑或返修前，阅读 [剪辑经验与交付准则](docs/editing-standards.md)，交付前执行
[实际剪辑交付清单](docs/validation.md#实际剪辑交付清单)。已确认的删词、就近完整语音切入、
末句补全、干净画面定格、语义停顿、PMO 一源多成片和命名规则均按此沿用。
具体选帧、词界和停顿时长仍按本次源素材复核。

`AGENTS.md` 和剪辑技能均指向该准则，新会话无需重新提供历史聊天。
技能的版本化源文件为 `skills/auto-cut-slicing/SKILL.md`；修改后运行
`python scripts/install_repo_skills.py` 刷新本仓库 `.codex/skills/` 中的安装副本。

## 本机开发

Python 3.10–3.12，FFmpeg / FFprobe 在 PATH 中可用。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe scripts/install_repo_skills.py
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
```

用 `Auto-cut-slicing.code-workspace` 打开工作区。源素材、生成文件和本机配置放在忽略的
`tmp/`、`output/`、`.local/` 中。

## Taskboard 批量 MP4

在 Taskboard「新增 Auto-Cut 包」中选择本仓库目录后，点击「验证并读取」可读取
[包身份](.codex-plugin/plugin.json) 和 [工作流声明](PACKAGE-MANIFEST.json)，识别为
`auto-cut-slicing` 版本 `0.3.0`、`vm_pmo_review_video` 配置模板 v1 / 执行协议 v2，并保存配置草稿。
声明不包含嵌入运行时，运行核心版本为空；Taskboard 使用自身的本机 VM runtime 调用该包，审核启用还会校验
v2 contract、固定 driver、Python 路径和实时 Base 字段。包声明本身不授予执行资格，必须通过 Taskboard 的受控
Bridge 登记、自动执行策略和配置激活。包声明中的版本需与 `pyproject.toml` 和 Python 包版本同步。

按 [Taskboard 契约](docs/taskboard-contract.md) 填写本地请求，参考
[请求示例](examples/taskboard-request.json)。同一视频的 VMID 可以对应多个显示名称，所有输出复用同一
`source.id` 和源文件。素材、指令及结果通过本地文件交接。

VM/PMO v1 仍冻结为 initial 纯确切时间范围协议；v2 本机 `plan` / `render` 增加由明确 parent
MP4 开始的审核轮次，并接入源哈希绑定的删词/语音 cue 规划。需要词级证据时由剪辑包读取本机已配置
的 ASR 连接生成并复用时间戳；ASR 不可用、词语匹配不唯一或证据与 parent 哈希不符时阻断。
协议见 [VM / PMO v1](docs/vm-pmo-offline-contract-v1.md) 和
[VM / PMO v2](docs/vm-pmo-offline-contract-v2.md)。Taskboard v2 已接入初稿审批下载、审核 parent/轮次、
MP4 校验、附件追加读回和阶段写回；离线合成素材验证覆盖多轮审核和语义证据阻断。真实测试表的审批、
附件更新及最终归档仍须单独验收。跨系统运行方式见
[VM / PMO 联调](../feishu-codex-Taskboard-main/docs/vm-pmo-offline-integration.md)。

```powershell
python -m auto_cut_slicing run examples/taskboard-request.json --result tmp/taskboard-result.json
python -m auto_cut_slicing run examples/taskboard-request.json --result tmp/taskboard-result.json --resume
```

每条输出选择 `instruction` 或有稳定 ID 的有序 `clips`，生成独立 MP4。
支持 `0:00-2:31;2:34-结束`、`0:04-1:23 +1:31（接下来）-2:29` 等保守指令格式。
新请求默认 `boundary_policy: "sentence"`。规划器对普通数字起点做句首回退、对句内终点做句末补全，
但实际剪辑起点须按上述准则围绕标注时间复核：优先就近完整词/短语，用 `start_cue` 固化，
避免为凑整句带入前面的引入语。约 1 秒是复核提示，不是截词硬限值，也不是结尾延长上限。
句内词尾或停顿也会补全当前句；完整句边界与句间空隙保持原坐标。显式语音起点仍从指定词开始。
除源文件首尾外，待解析句边界需要源哈希绑定、含有效 `utterances` 的 ASR 证据；缺失时失败，不猜边界。
需要严格数字范围时在请求或单个结构化 clip 中设置 `boundary_policy: "exact"`。
结果 JSON 保存成功/失败、原始指令、源文件和成片哈希、计划、时长及解码验收。
只有全部成功时退出码为 0。已有 MP4 拒绝覆盖；恢复需要匹配请求和已记录的成功证据。
句边界补全造成的新片段重叠在渲染前报错；原本明确重叠的选取仍可用于重复素材。

普通删词同时移除画面和声音，后续时间轴前移；明确“剪掉声音”则只静音精确词区间，时长不变。
“首先”“接下来”等语音起点使用源哈希绑定的 ASR 精确定位，数字时间只作为搜索锚点。
复杂画面替换支持参考 NaOH 句式，必须提供源哈希绑定的选帧和结束边界；
`inspect-freeze` 只提出像素变化候选，选中画面的内容和动画边界仍需检视。

## 可编辑草稿与单条导出

原有 [任务格式](docs/job-format.md) 和 [草稿示例](examples/slicing-job.json) 保持兼容。
底层任务 clip 未声明边界策略时仍使用 `exact`；缺少策略的旧 Taskboard 回执也按 `exact` 恢复，保留原计划哈希。
草稿保留完整源素材，并将每段保留范围写为可编辑、同步的 V1/A1 片段。
静音与定格属于 MP4 专用效果，草稿入口会明确拒绝。

```powershell
python -m auto_cut_slicing configure --upstream-root C:\path\to\Auto-cut-lite-src --upstream-python C:\path\to\python.exe
python -m auto_cut_slicing doctor
python -m auto_cut_slicing transcribe tmp/input/talk.mp4 --output tmp/input/talk.words.json
python -m auto_cut_slicing plan examples/slicing-job.json --output tmp/plan.json
python -m auto_cut_slicing draft examples/slicing-job.json --drafts-root output/drafts --receipt tmp/draft-receipt.json
python -m auto_cut_slicing render examples/slicing-job.json --output-id montage --output output/montage.mp4
```

草稿和真实词级 ASR 通过锁定版本的 Auto-Cut Lite 源码桥接复用；本机路径仅写入 `.local/config.json`。
桥接先验证 Lite 身份、提交和源码清洁状态。纯范围规划、画面分析及 MP4 渲染使用本地 Python 和 FFmpeg。
示例中的素材路径为占位，执行前须准备真实素材；语音操作及 Taskboard 默认的句边界解析还须准备匹配源文件的 ASR。
`exact` 的纯范围任务无需 ASR。ASR 句末标点是边界候选证据，实际语音完整性仍需试听验收。

[验收说明](docs/validation.md) 区分结构/真实媒体回归与实际语音试听、画面语义、剪映打开验收。
维护规则见 [AGENTS.md](AGENTS.md)，依赖边界见 [架构说明](docs/architecture.md)。
