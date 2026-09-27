# Changelog

本项目所有值得记录的变更都会写在这里，格式参考 [Keep a Changelog](https://keepachangelog.com/)。

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
