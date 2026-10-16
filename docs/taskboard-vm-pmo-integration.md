# Taskboard VM / PMO 接入状态

更新时间：2026-10-15。Taskboard 与 `auto-cut-slicing` 的 VM/PMO v2 生产路径已接通，并通过合成媒体离线联调。真实测试记录上的审批、PMO 附件读回和 NAS 归档尚未验收；离线结果不代表这些外部效果已通过。

跨系统协议见 [VM / PMO v1](vm-pmo-offline-contract-v1.md) 和 [VM / PMO v2](vm-pmo-offline-contract-v2.md)。Taskboard 的运行与真实测试表步骤见 [VM / PMO 离线联调](../../feishu-codex-Taskboard-main/docs/vm-pmo-offline-integration.md)。

## 工作流

包模板身份保持 `vm_pmo_review_video` v1。当前 Auto-Cut manifest 声明执行 contract v2 和固定 driver `vm_pmo_review_video_v2`；Taskboard 从受信包工作区复核真实 manifest、摘要与本机 Python runtime。普通 ZIP runner 不接受 VM 包，VM/PMO 使用独立的 Bridge 登记接口、编排器、结果校验器与 MP4 交付 worker。

| 阶段 | 输入与执行 | 成功条件 |
| --- | --- | --- |
| `initial` | 受信状态边沿登记；使用正常 VM 审批队列和通过采集回执验证的源视频；v1 保留区间输入保持兼容。 | MP4 通过独立媒体校验，追加到配置附件字段并读回确认，随后回写该阶段完成。 |
| `review` | 仅在 v2 driver 和完整审核字段启用后登记；源是上一轮在 Taskboard 附件字段读回确认的 MP4，不发起新的 VM 下载申请。每次任务冻结 parent run、artifact、delivery、SHA、字节数、时长和意见快照。 | 与初稿相同，MP4 附件读回确认后才完成；下一轮只能继承此处产生的已确认交付指针。 |
| `finalArchive` | 独立、默认关闭的状态边沿；认证 Bridge 请求不接收本机路径。worker 只读取最新确认交付指针。 | 重新验证 SHA 后在配置目标无覆盖发布；同名同 SHA 幂等，同名异 SHA 显示冲突。 |

初稿与审核阶段都校验实时 Base 字段、单选项、配置版本、包 manifest 和受控来源。审核启用需要审核意见文本字段、不同触发选项及开始/完成回写。归档触发必须与启用的编辑触发不同。保存/启用不会补跑既有状态；模拟事件、普通任务、复制标记或标签都不能获得登记、执行、上传、回写或归档资格。

## 编辑证据

v1 只接受 `initial`、无 parent 的 `vm_original` 时间轴和明确范围，继续供初稿兼容使用。v2 支持 `initial` 或 parent-bound `review`，范围、源哈希绑定词级删词/静音、完整词 cue，以及带源哈希证据的静帧操作。语音编辑调用包本机的 ASR 配置；Taskboard 输入、子进程参数和普通日志不携带 ASR 凭据。

删词、静音和语音 cue 的计划须绑定当前源 SHA、唯一词级对齐和准确词边界；重复词定位、ASR 不可用、对齐错源或保护词冲突时，整个运行在 render 前 `blocked`，保留原始意见且不创建成片。审核的时间坐标只属于冻结 parent MP4，不回退到原始 VM 视频。重启与重试复用相同 parent、轮次、意见、输出路径和对齐文件，不另申请素材或重新分配轮次。

## 交付与恢复

Taskboard 独立核对输入、计划、回执、parent 身份和实际 MP4 的摘要、大小、时长、H.264/AAC、1920×1080、25 fps 与完整解码。产物上传 token 不等于交付：worker 重读附件字段、保留已存在附件，只在目标 token 不存在时追加，并读回同一 token；成功后才更新最新已交付指针。阶段回写失败可单独恢复，不重新渲染。附件服务不提供 CAS，读取与写入之间仍存在并发编辑风险，真实验收应避免多人同时修改目标附件。

初稿审批等待不会占用剪辑槽。自动执行仍要求本机开关、可信 Bridge 登记、活动自动配置及启用包全部有效；取消开关只影响尚未开始的预约，不会回滚已启动运行，重新开启也不补跑历史事件。Taskboard 与 Bridge 只监听 `127.0.0.1`。

## 离线验收

在 Taskboard 仓库运行：

```powershell
node scripts/check-vm-pmo-offline.mjs --package-root 'E:\codex\Auto-cut-slicing-src' --python 'C:\path\to\python.exe'
```

脚本使用合成视频与确定性合成词时间戳，验证 v1 范围剪辑、v2 词级删除、parent 连续两轮、歧义与错源哈希阻断、最终媒体校验及输入/计划/回执篡改拒绝。它只在 `.runtime/vm-pmo-offline` 创建本机测试产物，不启动 Bridge、不请求审批、不访问飞书、不上传或回写，也不触碰归档目标。

真实验收另用指定测试记录：先通过正常审批完成初稿并确认附件读回，再进入审核触发选项验证 parent SHA、无新审批申请、语义修改、下一稿附件追加与阶段回写；最后单独验证归档同名幂等和异 SHA 冲突。每次必须检查 Bridge 的真实事件来源、Taskboard 任务/run、附件读回及归档 worker 回执。不得用复制文件、手改数据库、模拟事件或附件顺序代替业务凭据。
