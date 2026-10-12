# Auto-Cut Slicing · 视频切片剪辑包

`0.2.0` 独立视频切片工作流：多条独立输出、指定顺序拼接、精确删词和删段落。
默认产品为可编辑剪映草稿；明确选择 Taskboard MP4 通道时，直接生成 **1920×1080、25 fps** 成片，
按原比例缩放并补边。单条失败会记录原因并继续其他输出。

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

按 [Taskboard 契约](docs/taskboard-contract.md) 填写本地请求，参考
[请求示例](examples/taskboard-request.json)。同一视频的 VMID 可以对应多个显示名称，所有输出复用同一
`source.id` 和源文件。素材、指令及结果通过本地文件交接。

```powershell
python -m auto_cut_slicing run examples/taskboard-request.json --result tmp/taskboard-result.json
python -m auto_cut_slicing run examples/taskboard-request.json --result tmp/taskboard-result.json --resume
```

每条输出选择 `instruction` 或有稳定 ID 的有序 `clips`，生成独立 MP4。
支持 `0:00-2:31;2:34-结束`、`0:04-1:23 +1:31（接下来）-2:29` 等保守指令格式。
结果 JSON 保存成功/失败、原始指令、源文件和成片哈希、计划、时长及解码验收。
只有全部成功时退出码为 0。已有 MP4 拒绝覆盖；恢复需要匹配请求和已记录的成功证据。

普通删词同时移除画面和声音，后续时间轴前移；明确“剪掉声音”则只静音精确词区间，时长不变。
“首先”“接下来”等语音起点使用源哈希绑定的 ASR 精确定位，数字时间只作为搜索锚点。
复杂画面替换支持参考 NaOH 句式，必须提供源哈希绑定的选帧和结束边界；
`inspect-freeze` 只提出像素变化候选，选中画面的内容和动画边界仍需检视。

## 可编辑草稿与单条导出

原有 [任务格式](docs/job-format.md) 和 [草稿示例](examples/slicing-job.json) 保持兼容。
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
示例中的素材路径为占位，执行前须准备真实素材；需要语音操作时还须准备匹配源文件的词级 ASR。

[验收说明](docs/validation.md) 区分结构/真实媒体回归与实际语音试听、画面语义、剪映打开验收。
维护规则见 [AGENTS.md](AGENTS.md)，依赖边界见 [架构说明](docs/architecture.md)。
