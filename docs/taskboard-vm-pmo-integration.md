# Taskboard VM / PMO 接入对照与联调方案

日期：2026-10-12；2026-10-13 补齐包声明和离线 VM/PMO v1 适配。状态：身份读取、配置预览，以及 Taskboard 到真实包的纯确切范围离线 `plan` / `render` 已联调；生产审批、受控 task/run、审核 parent 和 PMO 上传尚未接通。
离线协议字段、回执和能力边界见 [VM / PMO 离线剪辑协议 v1](vm-pmo-offline-contract-v1.md)。本文保留跨系统差异与联调计划，不表示 Taskboard 已可启用此包。

对照来源为 Taskboard 仓库中的
`docs/superpowers/specs/2026-10-09-vm-pmo-review-video-interface-draft.md`、
`examples/vm-pmo-review-video-preview/README.md` 及当前工作区代码；执行接入前须重新检查
已验收代码与安装运行时的接口版本。剪辑包现行行为见 [本地 MP4 契约](taskboard-contract.md)，
已验收编辑判断见 [剪辑经验与交付准则](editing-standards.md)。

## 已核实的接入条件

Taskboard 的 `taskboard/server/autocut-local-runner.mjs` 使用固定参数数组启动本机 Python，
具备本机进程调用机制。旧 runner 专用于已安装 Lite 运行时与 `review-document-run`，不能直接换一个工作区路径就调用本剪辑包。
Taskboard 已新增 VM/PMO 专用离线 runner 与结果校验，并通过合成媒体联调；该 runner 不连接生产审批、来源登记和 PMO 上传。

VM 模板在 `taskboard/shared/feishu-package-workflow.mjs` 中被明确阻断执行，
`taskboard/server/feishu-package-workflow-drafts.mjs` 只提供配置草稿。
VM 下载能力已经独立存在，但尚未与本轮 task/run、审核成片身份、MP4 上传交付串联。
预览示例只有包身份声明，不含执行器。剪辑包已补齐
[plugin.json](../.codex-plugin/plugin.json) 和 [PACKAGE-MANIFEST.json](../PACKAGE-MANIFEST.json)，
可识别为 `auto-cut-slicing` `0.2.0`、VM/PMO v1 并保存配置草稿；未声明嵌入运行时或执行能力。
包声明尚未新增可执行能力；实际宿主检查应返回 `runtimeVersion: null`，启用继续以 `PACKAGE_WORKFLOW_NOT_READY` 阻断。

| 范围 | 剪辑包当前实现 | 接入需要补充 |
| --- | --- | --- |
| 入口 | 本地 `run` 保持兼容；独立 VM `plan/render` 与 Taskboard 专用离线 runner 已联调，宿主使用固定 argv 并处理 timeout/cancel。 | 接入受控来源、生产资格和自动调度；不得复用 Lite ZIP runner。 |
| 身份 | VM v1 已冻结并回传 package、task/run、record、stage/event、review_round、delivery_target；首版 initial、parent 为 null。宿主逐项校验。 | 将离线身份接入真实任务与配置快照，增加审核 parent 及跨轮次保护。 |
| 路径 | VM v1 由受信 context 提供绝对路径，固定 input/context 文件名；宿主和包校验运行目录、路径冲突和文件链接边界。 | 接通获批素材采集与受控目录，管理生产素材生命周期。 |
| 计划 | VM v1 保留原意见、resolved/blocked 状态、有序毫秒 segments 与原字节哈希；render 重新编译并核对计划和 ready 回执。 | 扩展源绑定语音/视觉证据及带证据编辑；未解决要求继续阻断。 |
| 成片结果 | VM 每轮唯一 review_mp4，回传身份、大小、时长、格式、哈希及完整解码；宿主独立检查回执与实际 MP4。 | 接通附件上传、token 持久化与绑定读回，完成业务交付。 |
| 状态与退出码 | VM v1 已实现 ready/blocked 与 rendered/blocked/failed；宿主分别验证退出码、回执状态和实际成片。旧 run 状态不变。 | 增加生产审批等待、任务阶段和 delivered 状态的持久化编排。 |
| 坐标 | VM v1 使用整数毫秒并严格转换为引擎微秒，首版只接受毫秒对齐的确切范围。 | 带证据编辑扩展须保留词界精度和舍入证据，审核轮次明确坐标基准。 |
| 恢复 | 离线输入、源、计划和回执均绑定本轮身份与哈希；已有目标无覆盖，变更或未知结果阻断。 | 接入生产恢复队列，将 rendered 与 delivered 分开，上传重试复用已验证成片。 |

## 以用户已确认要求为准的差异

| 草案或配置现状 | 本项目采用的接入要求 |
| --- | --- |
| 草案写审核成片沿用 720p | VM 下载可为 1280×720；成片统一 1920×1080、25 fps，等比补边。采集规格与输出规格分开声明，升采样不代表画质提升。 |
| NaOH 操作被定义为等时长动态画面窗口，定帧不支持 | 已验收需求是 `freeze_frame`：选后续干净单帧，覆盖至新内容自然恢复，原音继续。不能把静帧冒充动态 donor 窗口替换。 |
| 草案推荐“视频名称（10字以内）”，VM 配置字段没有成片名称映射 | Taskboard 增加明确的成片命名字段 ID，冻结 PMO 原名；安全文件名和原名映射分别保存。 |
| 第一步只传本地视频和保留段 | 仅适用于已明确精确边界且没有附加修改的联调样例；真实指令中的 cue、删词、换画面和语义补全必须逐项处理或阻断，不能省略。 |

输入中的语音证据应绑定本轮源哈希；不能把另一轮的 ASR 直接复用。
普通时间锚点优先就近完整词/短语，需用 cue 固化时保留源证据；终点补完当前句。
画面语义和接缝节奏按项目准则复核，不能由格式正确或全片解码自动证明。

## 双方职责

| 剪辑包负责 | Taskboard 负责 |
| --- | --- |
| 校验本轮输入、源与计划绑定；复用统一 planner/render。 | 读取并冻结 PMO 字段，准备已获批素材、截图及准确路径映射。 |
| 逐条解析要求；有歧义或缺证据时返回 blocked。 | 管理 task/run、审核轮次、配置快照、本机执行资格与调度。 |
| 按计划导出准确路径 MP4，验证媒体并返回哈希、大小、时长与结果。 | 独立校验结果与 MP4，上传并追加附件，读回确认，更新任务和阶段。 |
| 保留原意见与变更/验证依据，明确自动检查覆盖范围。 | 冻结下一轮 parent，持久化上传 token、交付状态与重试记录。 |

同一 VMID 对应多个 PMO 记录时，Taskboard 复用源采集结果，但每条记录使用独立运行、成片与交付身份。
单条失败继续其他合法任务。剪辑包不持有 VM/飞书凭据，不自行上传或更新 PMO。

首剪基于 `vm_original`；审核基于明确绑定的 `previous_review_video`。
首剪历史保留范围在审核轮次只作为历史背景，不能再裁一次；附件顺序不证明谁是上一轮。
若上轮成片已没有增加自然停顿所需的源尾段，本轮不能私自回原视频取材或套用旧坐标，
须记录能力/素材限制并明确解决方案。现有 `freeze_frame` 不插入时长。

## 联调顺序与完成条件

以下为独立阶段，每阶段提交可复核的契约、样例和测试结果，再接下一层。

1. **冻结一份可运行的最小协议样例（已完成双方离线联调）。** 确定 input/context/plan/plan-result/result 的完整字段、
   required、版本、错误码、路径边界及退出码；使用本地 fixture，单源、单成片、有序 keep_segments、
   原音和 1080p25。参数命名和 result.status 必须由双方读取同一份样例验证。
2. **剪辑包适配（最小范围已完成）。** 独立模块入口 `python -m auto_cut_slicing.vm_pmo`，
   从 context 将本轮素材与证据转换为统一 planner 输入。plan 固化原字节哈希和有序片段，
   render 复核输入/源/计划后渲染并原子发布，回传唯一 MP4；原有本地 v1 保持兼容。
3. **宿主离线调用已完成，生产资格与调度待接入。** Taskboard 已实现 `vm_pmo_review_video_v1`
   专用 runner、身份和 context 路径校验、计划/回执状态校验及独立 MP4 验证；使用固定 argv 并处理
   timeout/cancel，不借用 Lite ZIP runner。后续接入受控来源登记、本机执行开关、白名单、
   获批素材绑定与自动调度；验收这些生产条件后，才在现有包声明上增加相应执行能力。
4. **带证据的编辑扩展。** 定义源绑定 ASR/utterances、cue 和视觉证据映射，逐项接入现有删词、静音、
   静帧和句末补全能力。第一阶段未承接的要求保持 blocked，不能只裁剪就报成功。
5. **轮次与附件交付。** Taskboard 接通获批素材与受控任务绑定、明确 parent 选择、附件追加/token 恢复、
   绑定读回、阶段回写。只有附件读回成功才是 delivered；仅 rendered 不算 PMO 交付完成。

建议的剪辑包代码分工为 `auto_cut_slicing/vm_pmo.py`（协议 CLI）、
`auto_cut_slicing/vm_pmo_contract.py`（字段/身份/context 校验）、
`auto_cut_slicing/vm_pmo_adapter.py`（转换与结果映射），对应独立协议、CLI 和真实媒体测试。
当前适配实现集中在 `auto_cut_slicing/vm_pmo.py`，测试见 `tests/test_vm_pmo.py`；完整带证据编辑仍待扩展。

Taskboard 已完成离线 runner 与 run-input/result 校验；生产资格/调度、轮次与交付队列仍按独立子项目实施。
生产启用前须同步其根 AGENTS、README 与测试中的完成条件：旧流程唯一 ZIP 发布，
新流程 MP4 附件读回确认。不能只删除 `PACKAGE_WORKFLOW_NOT_READY` 就认为接入完成。

## 必须通过的联调样例

- 单段、有序多段以及倒序片段，输出顺序严格跟随输入；原音同步，1080p25，实测大小/时长/哈希正确。
- 同 VMID 两条记录得到两个独立成片与回执；一条失败不影响另一条。
- 输入、源、计划或 parent 哈希变化时阻断；已存在但无本轮成功证据的 MP4 不冒充新输出。
- 需要语音定位/句界补全却缺 ASR、未知操作、模糊 cue、当前轮适用的截图要求未判读，
  或本轮适用性未明确时，完整保留意见并阻断。纯范围 exact 不强制 ASR，历史背景截图不自动成为待执行意见。
- 无音轨源仅在 profile 明确允许时交付无音轨；应有音轨却缺失必须失败。
- 输出越界、源/输出同路径、未知字段/版本、截断回执、非零退出、超时和取消均不登记成功。
- 上传或阶段回写失败可恢复，复用本次成片和已知 token；旧轮次不覆盖新轮次状态。
- 真实测试表与获批素材另做端到端验收；离线 fixture 通过不等于生产事件或 PMO 上传已通过。

## 可转交给 Taskboard 开发方的结论

剪辑包已经具备本地视频的保留/拼接、精确删词、静音、静帧和语义边界能力；旧入口仍是 `python -m auto_cut_slicing run request.json --result result.json`。
独立 VM/PMO 离线入口仅接受单条纯确切时间范围意见；超出能力的意见会阻断。Taskboard 宿主与真实包的离线合成媒体联调已通过，下一步是扩展带证据的意见、审核轮次以及受控生产编排。
请以 1080p25 输出、静帧覆盖到动画恢复、PMO 成片命名字段为准同步宿主契约；
Taskboard 继续负责素材、轮次、执行资格和附件交付。离线 schema/fixture 已冻结并通过双方联调；
真实调用须等受控生产资格、审批下载绑定、所需编辑能力与审核轮次、附件读回及阶段回写在测试表验收后启用。
