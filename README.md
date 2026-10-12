# Auto-Cut Slicing · 视频切片剪辑包

独立的视频切片工作流，支持**多条独立切片、指定顺序拼接、按词删除、按时间范围删除段落**。
默认交付可继续修改的剪映草稿，MP4 是明确请求后才生成的可选输出。

这个包独立管理需求、版本与 Git 历史，初始版本为 `0.1.0`，主分支是 `main`。
它通过锁定版本的外部桥接复用 Auto-Cut Lite 的草稿和词级 ASR 能力，不复制整套上游代码。
切片流程自己的规划器执行实际删除和后续时间轴前移；原文件完整保存在草稿本地素材中。

## 本机开发

Python 3.10–3.12，FFmpeg / FFprobe 可在 PATH 找到。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe scripts/install_repo_skills.py
.\.venv\Scripts\python.exe -m pytest -q
```

用 `Auto-cut-slicing.code-workspace` 打开独立工作区。CLI 也可直接用 `python -m auto_cut_slicing`，
规划、媒体探测和 MP4 渲染无需导入上游 Python 依赖。

## 配置上游能力

在新包目录运行，指定已具备剪映草稿依赖的上游解释器：

```powershell
python -m auto_cut_slicing configure --upstream-root E:\codex\Auto-cut-lite-src --upstream-python C:\path\to\python.exe
python -m auto_cut_slicing doctor
```

路径仅保存在忽略的 `.local/config.json` 中。`auto-cut-dependency.lock.json` 记录 Git 版本。
每次使用先验证上游 Lite 身份、锁定提交和源码清洁状态；不修改上游、不调用 Lite 审稿流程。
本版只支持源码仓依赖。安装包运行时接入需另加部署报告、manifest 和文件清单验证。

## 从素材到输出

1. 为需要删词的源视频生成词级 ASR。服务凭据使用本机已有配置，调用真实服务时才需要授权和网络。
2. 按 [任务格式](docs/job-format.md) 填写来源、片段、顺序和删除项，参考 [示例](examples/slicing-job.json)。
3. 编译计划，确认选取范围、压缩后的时长与切点。
4. 写出可编辑草稿；需要时另外渲染 MP4。

```powershell
python -m auto_cut_slicing transcribe tmp/input/talk.mp4 --output tmp/input/talk.words.json
python -m auto_cut_slicing plan examples/slicing-job.json --output tmp/plan.json
python -m auto_cut_slicing draft examples/slicing-job.json --drafts-root output/drafts --receipt tmp/draft-receipt.json
python -m auto_cut_slicing render examples/slicing-job.json --output-id montage --output output/montage.mp4
```

`draft` 默认生成任务内全部输出，可重复传 `--output-id` 选择子集。草稿和 MP4 都拒绝覆盖已有目标。
计划文件和批量回执也拒绝覆盖已有文件；批量回执放在生成的草稿目录之外。
例子里的素材路径是占位说明，必须替换为你的真实文件。

## 删除与追溯

- **删词**：匹配源文件哈希绑定的词级时间戳。只接受唯一、连续、完整词元的精确匹配，标点归一化；
  重复词需用 `rough_time` 和 `search_radius` 缩小范围。粗略时间不会直接作为删除边界。
- **删段落**：用明确的源时间范围，限制在当前选取片段内。源时间始终相对原视频，不因前面的删除改变。
- **保护词**：`must_keep` 必须能在已绑定的 ASR 中验证，删除范围不能吞掉它。
- **拼接**：按 `clips` 顺序排列，可来自多个源文件、重复使用同一来源或改变源顺序。
- **编辑性**：V1/A1 逐段写入保留范围，音视频源起点同步，完整原素材可用于恢复删除内容。
  删除记录和原文标签独立保存，隐藏的修改标签轨不遮挡画面。

## 当前验证范围

本版包含规划器、ASR 唯一定位校验、媒体探测、上游桥接、可编辑草稿写入及结构验收、可选 FFmpeg 导出。
测试使用合成素材与明确标注的 ASR 测试数据，不将测试数据视为真实识别证据。
结构验收不等于剪映打开验收，也不等于删词接缝已通过试听和全候选反向 ASR。
真实项目仍需验证这些项目后再宣称正式完成。

暂不包含自动发现精彩片段、字幕重排、BGM、转场、自然语言自动生成完整任务 JSON 或飞书文档编译。
自然语言入口由仓库技能指导代理编制并执行任务。
视频轨晚于容器时间轴起始的特殊素材会明确报错，本版尚不自动补齐其起始画面。

项目维护规则见 [AGENTS.md](AGENTS.md)，复用边界见 [架构说明](docs/architecture.md)，
开发验证见 [验收说明](docs/validation.md)。
