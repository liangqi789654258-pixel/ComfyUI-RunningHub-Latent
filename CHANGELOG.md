# Changelog

本项目所有值得记录的变更都会写在这里，格式参考 [Keep a Changelog](https://keepachangelog.com/)。

## [1.2.0] - 2026-09-29

按用户实测反馈做的易用性重构。主线是「极简版做到真·零配置」和「专业版开箱即用」。

### 变更

- **极简版只剩 2 个输入框**（api_key、workflow_id）：
  - 移除了 positive / negative / seed / timeout / poll_interval / use_cache /
    use_high_performance / on_missing_placeholder 全部 widget；
  - 提示词完全沿用云端默认文案；种子每次执行自动随机（`IS_CHANGED` 强制重算）；
  - timeout / poll_interval / use_cache / use_high_performance 改为从 config.ini 读取；
  - 移除 `on_missing_placeholder` 选项，行为统一为「警告继续 + 节点变黄」。
- **专业版开箱即用**：接入与极简版同一套自动注入（占位符全局替换 + 种子），
  提示词不再需要手写映射；`node_overrides` 与自动注入冲突时以映射为准。
- **专业版输入框文案**：提示词框可见文本只显示 `positive` / `negative`；
  参数映射框默认留空，格式说明全部移入悬停提示（tooltip），不再用示例 JSON 当默认值。
- 示例工作流同步重做：极简版示例只有 2 个 widget 值；专业版示例默认映射留空；
  二采示例的映射只保留 denoise 一项（这才是映射的典型用途）。

### 兼容性说明

- 极简版输入结构变了：旧版画布里已摆好的极简版节点需要重新添加。
- 专业版注册名、输入键名与 widget 顺序均未变，已保存的工作流不受影响。

## [1.1.0] - 2026-09-28

按改造需求文档完成的一轮打磨。主线是「消灭静默失效」和「降低上手门槛」。

### 新增

- **极简版节点** `RunningHubLatentGeneratorSimple`：只填 workflow_id 即可出图。
  自动拉云端工作流结构、扫描所有字符串字段替换 `{positive}` / `{negative}`、
  自动定位采样器 seed 字段注入种子
- **提示词全局替换**（`core/workflow.py`）：不依赖 `class_type`，只认占位符文本，
  因此兼容任意结构的工作流；支持前后缀保留（`masterpiece, {positive}, best quality`）
  与大小写变体（`{Positive}`）
- **替换结果校验**：替换命中数为 0 时给出醒目警告，杜绝静默失效
- **参数映射校验**：`validate_mapping` 开启后，nodeId 不存在 / fieldName 无效
  都会在 report 里明确提示
- **API Key 全局配置**：新增 `config.ini`（`config.json` 继续兼容）
- **前端扩展** `web/rh_warning.js`：出现警告时节点边框变黄并弹 toast，
  同时拉高提示词输入框。纯 Python 无法给节点上色，这一步必须靠前端
- **参数映射校验与自动定位结果**可在「RH 工作流参数探查」中直接查看，
  它会告诉你这条工作流能不能被极简版自动注入

### 修复

- **输出类型识别失败**：真实接口返回的字段名是 `outputType`（值为 `Latent`），
  此前只读 `fileType`。现同时兼容 `fileType` / `outputType` / `type`，并统一小写比较
  （原实现其实已经做了 lowercase，真正的原因是字段名不同，不是大小写）
- **任务成功但无输出**：对象存储文件落地比状态更新慢，首次拿到空列表时
  等待 2 秒补查一次，仍为空才报错。此前会白白损失一次已付费的任务
- **响应形态兼容**：`data` 可能是 list、`{"results": [...]}`、`{"outputs": [...]}`、
  单个文件 dict，或带 `status` 字段，现已全部兼容
- **中文文件名**：下载时原样把 URL 交给 requests，不做任何手工编解码
- **排错信息增强**：识别不出 latent 时会附上云端原始返回（脱敏后）一起展示

### 变更

- 主节点显示名改为 **RH 云端采样 · 专业版**。
  注册名仍为 `RunningHubLatentGenerator` —— 改名会让用户已保存的工作流全部断线
- 三个文本框补齐 tooltip 与 placeholder，说明占位符规则
- 示例工作流改为 4 份：极简 / 专业单采样 / 视频 / 二采高清修复，均内置中文注释
- 自测从 49 项扩充到 **104 项**，新增覆盖输出类型大小写、空结果补查、
  工作流归一化、提示词替换、种子定位、映射校验、config.ini 解析

## [1.0.0] - 2026-09-27

首个可用版本。

### 新增

- **4 个节点**
  - `RunningHubLatentGenerator` 主节点：提交 → 轮询 → 下载 → 输出标准 LATENT
  - `RunningHubLatentFetch`：按 taskId 重新取回，用于超时续取与跨会话复用
  - `RunningHubLatentFromUrl`：直接加载任意 latent 下载链接
  - `RunningHubWorkflowInspector`：拉取云端工作流的可修改参数清单
- **通用的参数注入机制**：`node_overrides` 支持任意 nodeId / fieldName，
  并用 `{positive}` / `{negative}` / `{seed}` 占位符绑定到节点 widget，
  从而在不牺牲通用性的前提下保留「拖滑块调 seed」的原生体验
- **本地 latent 缓存**：按「工作流 ID + 参数」哈希，同参数重复运行不消耗云端额度
- **配置文件支持**：`config.json` 保存 API Key 与默认工作流，已加入 `.gitignore`
- **图像 / 视频双支持**：4 维图像 latent 与 5 维视频 latent 均可解析输出
- **全流程中文报错**：无效 Key、网络中断、额度不足、并发超限、云端节点级失败
  均有明确中文提示，且异常不会导致 ComfyUI 崩溃

### 实现要点

- latent 解析严格对齐 ComfyUI 原生 `LoadLatent`：带 `latent_format_version_0`
  标记的文件不做缩放补偿，老文件才乘 `1/0.18215`。该版本标记是空**张量**而非
  safetensors metadata（`load_file` 不会返回 metadata），这点已在 ComfyUI 源码中确认
- 自动纠正 `value` → `fieldValue`，避开 RunningHub API 最常见的调用错误
- 多处输出 latent 时沿 batch 维合并；形状不一致时取第一个并给出警告
- 所有异常消息对 API Key 做打码处理，杜绝密钥泄漏到 ComfyUI 前端

### 说明

立项文档建议 fork `ComfyUI_NetDist` 改造，最终未采用，原因见 README「设计说明」：
两者通信协议不匹配、可复用部分仅约 20 行，且 NetDist 的 Apache-2.0 与 MIT 发布存在冲突。
