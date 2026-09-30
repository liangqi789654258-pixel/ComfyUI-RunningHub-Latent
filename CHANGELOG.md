# Changelog

本项目所有值得记录的变更都会写在这里，格式参考 [Keep a Changelog](https://keepachangelog.com/)。

## [1.3.1] - 2026-09-30

### 文档：确认 RunningHub 不允许安装自定义节点，H3 路线改为「云端出片 + 独立音轨」

平台事实（用户确认）：RunningHub 云端不接受用户自行上传安装自定义节点，
但预装了大量主流节点。这条约束让 v1.3.0 的「路线 A（复合 latent 带回本地）」
在 RunningHub 上不可行——要修 `SaveLatent` 对 `NestedTensor` 报错，就必须换节点。

已核实核心里也没有替代方案（`nodes_minimax_h3.py` 与 latent 相关的节点
只有 `EmptyMiniMaxH3LatentAV` 一个，且无条件同时造视频+音频两个张量；
`NestedTensor.__getitem__/chunk` 返回的仍是 NestedTensor，切不散）。

因此 H3 在 RunningHub 上的正确用法改为：

    H3 采样器 →┬─ VAEDecode(H3 视频 VAE)      → VHS_VideoCombine(24fps) → mp4
               └─ VAEDecodeAudio(H3 音频 VAE) → Save Audio (Advanced)   → flac/mp3

音频必须显式接一路 `VAEDecodeAudio`（`nodes_audio.py:100` 取 `unbind()[-1]`），
否则 `VAEDecode` 只取 `unbind()[0]` 会把音频静默丢掉。帧率 24、音频 32kHz。

改动：

- `docs/h3-av-latent.md` 重写：路线 C 升为默认；路线 A/B 标注仅适用于
  允许装自定义节点的环境（本地 / 自建云端 / 其它平台）；新增 2 分钟云端自查
  （万一平台自带能存 AV latent 的节点，路线 A 可复活）
- README：节点表加平台限制说明；视频章节与 FAQ 的 H3 条目改为云端出片方案
- `extras/av_latent_nodes.py`：文件头注明 RunningHub 装不了，改用路线 C

代码逻辑未变，自测仍为 146 项。

## [1.3.0] - 2026-09-29

### 新增：音视频复合 latent 支持（MiniMax H3）

- **原生「保存Latent」存不了 H3 的 latent**：H3 的 latent 不是单个张量，而是
  `NestedTensor((视频 [B,24,T,H/16,W/16], 音频 [B,32,2,T40]))`；原生
  `SaveLatent` 里 `samples["samples"].contiguous()`（`nodes.py:552`）在
  NestedTensor 上必然抛 `'NestedTensor' object has no attribute 'contiguous'`。
  这不是接线错误，是原生节点没覆盖这种结构。
- 新增 3 个节点：
  - **RH AV Latent 保存**：替代原生 SaveLatent，把复合 latent 按
    `latent_0`（视频）/ `latent_1`（音频）写进一个 safetensors 容器。
    扩展名沿用 `.latent`，云端平台才会把它当作 latent 输出收录。
  - **RH AV Latent 拆分**：拆成视频 / 音频两条普通 latent。原生 `VAEDecode`
    遇到复合 latent 只取 `unbind()[0]`、音频被静默丢弃，所以这一步不能省。
  - **RH AV Latent 合并**：把两条 latent 组装回 NestedTensor，供需要完整
    复合 latent 的下游（如二采 / latent 放大）使用。
- `core/latent.py` 新增复合 latent 解析：识别 `latent_0/latent_1`、
  `h3_video/h3_audio` 等键名（兼容社区 `.h3latent` 包），并**固定不做
  0.18215 缩放补偿**——H3 的 scale_factor 就是 1.0，误乘 5.49 倍画面会毁掉。
- 输出分类新增 `.h3latent` / `.avlatent` 扩展名识别。
- report 的形状回显区分音视频，显示为「视频 [...] + 音频 [...]」。
- 新增 `extras/av_latent_nodes.py`：**云端用**的零依赖单文件节点包
  （保存 + 拆分），可单独作为一个 custom_nodes 包安装到云端。
- 新增示例 `examples/05_minimax_h3_av.json`（H3 本地解码链路，含音频合成）。
- 新增文档 `docs/h3-av-latent.md`：失败原因逐行分析、三条路线对比、
  云端/本地操作步骤、音频单独导出剪辑的三种做法、排障清单。

### 测试

- 自测 130 -> **146** 项，覆盖复合 latent 存取往返、不缩放、普通 latent
  不被误判、新扩展名分类、拆分/合并节点与可读报错。

## [1.2.2] - 2026-09-29

### 修复

- **文本处理节点接在提示词前面时静默失效**：ComfyUI 里 widget 一旦被上游连线
  接管，它在工作流 JSON 里就从字符串变成连线引用，往该节点注入 text 会被上游
  覆盖且无任何报错。现在会**沿连线向上游回溯**到真正可写的字符串字段再注入
  （支持多跳，兼容 text / value / string 等不同字段命名）。
  - 上游只有唯一字符串字段 -> 直接注入该字段；
  - 上游字段里有占位符 -> 认这个字段；
  - 上游有多个文本字段且都无占位符 -> **不猜**，保持 0 命中并如实报警。
- 新增 `prompt_link_diagnosis`：定位失败时给出具体到 nodeId 的原因
  （连线源缺失 / 上游多字段 / text 被接管 / 无采样器连线），不再泛泛提示。
- 新增 `placeholder_hits`：本地提示词为空但云端写了占位符时报警——
  否则云端提示词会被替换成空内容，画面对不上却一声不响。
- 专业版 report 现在会回显**实际送进云端的提示词**（前 60 字）。
  输入框一旦连上节点，框里手写的文字就不再生效，这一行是排查该类问题
  最快的一眼定位点。

### 测试

- 自测 117 -> 130 项，覆盖上游回溯、多跳链路、歧义放弃、空内容报警与预览。

## [1.2.1] - 2026-09-29

### 修复

- **提示词在部分工作流上永远不生效**：`getJsonApiFormat` 对某些工作流只返回
  注册为 API 参数的字段清单，CLIPTextEncode 的 text 值不在其中，导致占位符
  扫描恒为 0 命中（云端明明写了 `{positive}` 也报「没找到」）。
  新增**连线跟随兜底**：占位符没命中但用户填了提示词时，沿采样器的
  positive / negative 连线找到编码节点，直接覆盖其 text 字段。
  - 同一字段不会被正、负两个 token 重复注入（positive 优先认领）；
  - 连线源不在返回结构里时安全放弃并保持 0 命中（警告如实上报）；
  - 占位符主路径命中时兜底不触发。
- 「提示词未生效」警告重写：不再说「云端没写占位符」（可能冤枉用户），
  改为报告实际发生的事实（结构读到几个节点、两条路径都没命中），
  并给出在参数映射里直接指定文本节点 ID 的最可靠修法。

### 测试

- 自测 107 -> 117 项，新增覆盖兜底注入的命中、去重、安全放弃与主路径优先。

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
