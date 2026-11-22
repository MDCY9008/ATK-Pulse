# ATK-Pulse / ATK Battery Monitor V0.8.0 开发与验收要求

修订依据：2026-10-28 用户交接文档、已接受的十项审查意见、实际源码核查，以及 2026-11-21 Cycle 1 复盘校准（V0.3）、电压-电量因果标定（V0.4）、Win11 任务栏无侵入透明直显（V0.5）、“鸢尾温室”视觉重构（V0.6.1）、净室三协议设备库 + `v2` 多设备持久化重构（V0.7.0）与极简双栏 UI / 离线脱敏众包闭环 / GPL-3.0 开源发布收口（V0.8.0）。
当前仓库根目录为开发与运行入口；保留早期 V0.1 独立历史目录只读兼容，不跨目录迁移或改写外部配置。

## 十项补充的落实要求

1. **数据证据分级**：报文校验、百分比合法性、候选电压范围分别记录。范围合理不等于校准正确；电压异常不能抹掉合法百分比。CSV 保留全部原始字段且不改动格式；V0.4 新增独立只读模块 `voltage_soc.py`（15 点实测分段 LUT + 1 步压降门控 + 无线非对称单调防抖 + 快速解锁），仅在运行时推演 1% 步进电压估算电量 `percent_est`（`< 3722 mV` 区间明确标记“低段外推，待实机低电补测”），不写入 CSV，不估算容量。
2. **状态独立**：`receiver_visibility=present/absent/unknown`，`outcome` 表示本次查询结果，`last_success` 是独立的最后有效快照。枚举抛错为 unknown，不能宣称拔出。
3. **时间与新鲜度**：保存开始、本机接收、完成时间及 UTC 偏移；设备测量时间未知，`response_freshness=unverified`。界面显示时间和年龄，失败后显示历史属性；不宣称连续实时测量。
4. **查询调度**：单工作线程、单设备互斥、一个待处理手动请求。自动查询中到来的手动请求保留；连续请求合并；手动完成以编号匹配。单次读取总预算 1000 ms，分片不超过 200 ms，原有发送后等待约 100 ms。失败后等 15、60 秒各复查一次，此后每 300 秒周期查询；接收器缺席仅每 30 秒枚举。手动请求可提前触发，不补发积压周期。
5. **生命周期与进程**：停止/恢复以 generation 和取消事件丢弃旧结果；每次按当前接口路径打开，并在查询后重新枚举核对。GUI 异步等待线程正常退出，不强杀线程。`WM_POWERBROADCAST` 保持 1 秒防抖，`WM_DEVICECHANGE` 采用 4 秒防抖以抑制 USB 插拔抖动连续打断退避。任务栏直显采用独立解耦模块 `taskbar_badge.py`（Win32 无侵入透明顶层贴片 + `UpdateLayeredWindow`），支持托盘菜单“任务栏直显”开关、主题/DPI/`TaskbarCreated` 自适应、全屏自动隐藏、Z-order 保活与异常自动降级，不修改 Explorer 窗口树。跨进程锁仅约束采用新协议锁的本应用；V0.1、旧版 V0.2、ATK HUB 不参与，不得自动终止它们。记录进程父子关系，不能将 venv 启动器当重复实例。
6. **存储、兼容和回滚**：旧 CSV（`V0.1`、`V0.2` 及 `battery_samples.v1.csv`）在 V0.7 中严格只读。新有效样本追加写 `battery_samples.v2.csv`，每条含 `schema_version=2` 与 `device_uid`/`vid_pid`/`model_id`/`protocol_family`/`link_mode`/`battery_source_mode`；不去重、平滑或删除异常值。表头不兼容或尾行截断时拒绝追加，报告存储错误。旧记录未知字段保留未知，原时区缺失仅为排序/显示暂按本地解释并明确标记；界面默认读取上限为 50,000 条（`DEFAULT_READ_LIMIT = 50000`），导出仍为全量（`limit=None`）并保留原时间戳。源码基线须附 SHA256、环境版本和恢复说明（V0.6.1 源码备份位于 `backup_v0.6.1_src\`，V0.5 位于 `backup_v0.5_src\`，V0.4 位于 `backup_v0.4_src\`，V0.3 位于 `backup_v0.3_src\`，V0.2.1 位于 `backup_v0.2.1_src\`）；恢复时不得覆盖 CSV。
7. **充电与异常**：`charging_state=unknown`；兼容对象的 `charging=None` 同义。连接字节仅保留，V0.4 中 `link_byte=1` 仅用于 `voltage_soc.py` 内部滤波器快速解锁，界面不推断充电状态。异常只描述报告值变化，不断定根因，不修正原值。提醒仅由本次有效回复触发。
8. **追溯和日志**：每轮有唯一查询编号，关联触发方式、失败分类、耗时及落盘结果。`query_events.v1.jsonl` 与 `app.log` 各约 1 MB、保留 3 个轮转备份。原报文仅 DEBUG；诊断包由用户本地复制，不自动上传。设备 ID 注明是接收器序列号散列或不稳定路径散列，不冒充鼠标硬件唯一身份。
9. **研究和图表**：相邻回升、短时下降、相近候选电压不同百分比、恢复后变化、平台期候选电压变化只作描述性标记。V0.3 基于 Cycle 1 实测校准默认阈值：连续段内回升 `rise_pct=10`、10 分钟下降 `drop_pct=10` (`short_window_s=600`)、相近候选电压比较窗口 `comparable_mv=20`、平台期 `plateau_window_s=1800` 且累计电压变化 `voltage_change_mv=20`、跨断线显著回升 `gap_rise_pct=15`、断线阈值 `chart_gap_s=450`；遇到断线边界立即重置平台期起点，不把跨断线变化混入连续段异常。`research.Thresholds` 可显式调整。图表仅在超过 450 秒、来源或已知身份变化处断线（V0.6 采用 2px 抗锯齿平滑折线 + 底部浅鸢尾渐变填充，默认隐藏密集采样圆点，鼠标悬停吸附高亮点与详情浮窗，支持 1h/24h 默认开启的 Y 轴自动缩放）。两个周期仅初步观察，不验证准确率。
10. **可复现验收**：模拟故障与实机结果分别记录。自动测试覆盖协议错误、超时、取消、竞争、线程投递、Windows 互斥、存储/诊断失败、CSV 兼容和异常分析。实机须另测活动/静置/移动后、鼠标开关、接收器拔插、快速刷新、查询中退出、Windows 睡眠恢复、长期运行及与官方驱动共存；未做的写“待实机验收”。

## 事件与数据契约

- `query_id` 是每次实际查询的唯一事务编号，不是设备回包序列号。`request_id` 是手动请求编号，在合并/恢复重试时不变；每次重试生成新的 query_id，仅发布当前 generation 的结果。
- `outcome` 包含 `success/receiver_absent/enumeration_error/interface_not_found/ambiguous_interface/device_changed/device_busy/open_error/write_error/short_write/read_error/timeout/invalid_length/invalid_report_id/invalid_command/invalid_checksum/invalid_percent/cancelled/internal_error` 等。
- `packet_valid` 与 `percent_valid` 独立；候选电压保留 `voltage_raw`，范围门限仅用于展示过滤，`voltage_validation=candidate_in_range/candidate_out_of_range/unknown`。
- 成功读取与写入失败可同时存在：`connected=true, outcome=success, storage_error!=null`。失败事件不进入采样 CSV；进入独立诊断日志。存储失败持续显示，直到下次成功落盘。
- `last_success` 仅含时间、数值、身份和查询编号，不引用此前事件链。旧历史不会在启动时被当作新成功查询。

## 已知技术边界

- HIDAPI 的枚举、open/write/close 是同步本机调用；分片超时约束的是 read，不能保证驱动或系统故障时所有 native I/O 都能被强制中断。UI 保持退出等待状态并记录；不宣称严格全事务硬超时。
- 相同设备路径上的迟到报文/固件缓存，目前无已验证的设备序列号或测量时间可鉴别。generation 与查询后枚举解决软件生命周期问题，不能证明回复物理新鲜度。
- 若 Python HID 绑定把底层枚举错误直接转成空列表且不暴露错误，则无法完全区分底层故障与未发现设备；明确错误至少保持 unknown。
- 日志轮转有大小上限，详细 DEBUG 仅短时开启。单条记录异常巨大可超过单文件目标上限；应用不会主动记录巨大原始输入。
- CSV 写入使用追加并检查已知截断；断电或进程被强制结束仍可能留下尾行，保留现场而不自动修复。基线副本和原始数据是恢复依据。

## 执行与证据规则

禁止改固件、DPI、回报率、官方配置或代理；不得覆盖 V0.1、旧历史或原配置。启动项与用户设置保持原命名空间，只有用户操作设置控件才写入。先保存基线，再修改已确认问题，不升级现有依赖。

`tests/run_acceptance.py --output <新目录>` 生成逐项结果、完整日志和计时。通过自动测试只证明所列可复现软件场景，不等同真实设备和长期验收全部通过。
