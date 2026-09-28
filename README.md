# ComfyUI-RunningHub-Latent

把 **RunningHub 云端当作远程采样器** 用：云端跑完采样后只回传 latent，
**VAE 解码全部在本地完成**，最终输出 ComfyUI 原生 `LATENT`。

```
本地画布  ──▶  提交任务到 RunningHub  ──▶  云端采样，产出 latent 二进制
                                              │
本地 VAE 解码 ◀── 标准 LATENT ◀── 下载 + 解析 ◀┘
```

## 这个插件解决什么问题

| 场景 | 传统做法 | 用本插件 |
|------|---------|---------|
| 本地显卡跑不动大模型 | 全套工序上云，云端出图 | 只把采样交给云端，算力大头卸出去 |
| 想换 VAE 试画质 | 受限于云端工作流固定的 VAE | 本地任意 VAE，随时换 |
| 本地不放十几 GB 底模 | 必须下载 checkpoint | 本地只需要一个 VAE |
| 重复跑同一组参数 | 每次都扣费 | 本地 latent 缓存，重复的零成本重放 |

> **关于云端出图环节**：RunningHub 平台会对最终生成的图像/视频做内容审核。
> 如果云端工作流末端接的是「保存Latent」而非「保存图像」，平台流程就停在 latent 阶段。
> 这是本插件采用该架构的技术副作用，而非本插件植入的功能——本插件没有任何
> 伪装、对抗或规避检测的代码。请务必遵守当地法律法规与 RunningHub 平台规则。

## 安装

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/<你的账号>/ComfyUI-RunningHub-Latent.git
cd ComfyUI-RunningHub-Latent
pip install -r requirements.txt
```

重启 ComfyUI，在节点列表里搜索 **RunningHub** 分类即可看到 5 个节点。

> 三个依赖（requests / safetensors / numpy）在标准 ComfyUI 环境中都已存在，
> 这一步通常只会提示「已满足」。

## 快速开始

### 第一步：准备云端工作流（最关键）

在 [runninghub.cn](https://www.runninghub.cn) 上编辑你的工作流，重点是末端：

1. **删除**末端的「保存图像」/「预览图像」节点；
2. **接上**「保存Latent」节点（ComfyUI 原生节点，英文界面叫 `SaveLatent`），输入接 KSampler 的 latent 输出；
3. 「保存Latent」节点要能被真正执行到，**不要**在它后面再接别的节点把它短路；
4. 点保存。**每次打开工作流，URL 里的 ID 可能变化**，提交前注意取最新的那个：
   ```
   https://www.runninghub.cn/workflow/1980237776367083521   ← 这串数字就是 workflow_id
   ```

> ⚠️ 做不到这一步，插件会明确报错告诉你「没有产出 latent 文件」，并把云端实际输出的
> 文件列给你看。这是最常见的配置错误。

### 第二步：在云端文本节点里写占位符（极简版必需）

打开云端工作流的**正面提示词**文本框，把内容改成：

```
{positive}
```

负面提示词框同理改成 `{negative}`。可以带前后缀，例如
`masterpiece, {positive}, best quality` —— 前后缀会被保留。

> 这一步是极简版能「零配置」的前提。忘了写的话节点**会明确警告**（画布上变黄
> + report 里写明），不会静默失效。

### 第三步：本地跑通

- **极简版**：拖 `RH 云端采样 · 极简版`，只填 `workflow_id`，把 latent 接到
  本地 `VAEDecode` / `VAEDecodeTiled` → `SaveImage`。完事。
- **专业版**：还要填 `node_overrides`（见下节）。

不知道 nodeId 就拖一个 **RH 工作流参数探查** 节点执行一下，它会把云端所有
可修改节点列出来：

```
● nodeId=3  [KSampler]
    fieldName = seed       当前值 = 0
    fieldName = steps      当前值 = 20
● nodeId=6  [CLIPTextEncode]
    fieldName = text       当前值 = 1 girl

── 自动定位检查 ──
提示词占位符：{positive} 命中 1 处，{negative} 命中 1 处
  → 极简版可以自动注入提示词。
随机种子：命中 1 个可注入字段（nodeId=3.seed）
```

它会直接告诉你这条工作流能不能被极简版自动注入。

### 不想每次都粘贴 API Key？

把 `config.example.ini` 复制成 `config.ini` 并填写（旧的 `config.json` 继续兼容）：

```ini
[runninghub]
api_key = 你的 RunningHub API Key
default_workflow_id = 1980237776367083521
base_url = https://www.runninghub.cn
timeout = 600
poll_interval = 5
```

优先级：**节点显式输入 > config.ini > config.json > 内置默认值**。
临时换 Key 试跑不会被配置绑架。两个配置文件都已被 `.gitignore` 排除。

## 节点一览

| 节点 | 用途 | 适合谁 |
|------|------|--------|
| **RH 云端采样 · 极简版** | 零配置。自动定位提示词与种子，其余全部用云端默认值 | 新手 / 云端已调好只想换提示词 |
| **RH 云端采样 · 专业版** | 额外支持 `node_overrides` 完整参数映射 | 需要本地调 step / cfg / 尺寸 / 二采 denoise |
| **RH 按任务号取回 Latent** | 用已有 taskId 重新拉取（超时续取、换会话复用） | 所有人都该知道有这个 |
| **RH 从 URL 加载 Latent** | 直接给一个 latent 下载链接加载（手动下载场景） | 手动下载场景 |
| **RH 工作流参数探查** | 拉取云端工作流结构与可改参数，并判断能否自动定位 | 第一次配某条工作流时 |

### 两个版本怎么选

**极简版**会在提交前先拉一次云端工作流结构，扫描**所有字符串字段**里的
`{positive}` / `{negative}` 并替换，同时自动找采样器的 `seed` 字段注入种子。
因为它不认节点类型、只认占位符文本，所以单文本节点、双 CLIPTextEncode、
甚至提示词藏在第三方节点里都能命中。

**专业版**则把控制权完全交给你：

```json
[
  {"nodeId": "6", "fieldName": "text",     "fieldValue": "{positive}"},
  {"nodeId": "7", "fieldName": "text",     "fieldValue": "{negative}"},
  {"nodeId": "3", "fieldName": "seed",     "fieldValue": "{seed}"},
  {"nodeId": "3", "fieldName": "steps",    "fieldValue": 20},
  {"nodeId": "3", "fieldName": "cfg",      "fieldValue": 7.0}
]
```

### 共用参数

| 参数 | 说明 |
|------|------|
| `api_key` | 留空则读 config.ini / config.json |
| `workflow_id` | RunningHub 工作流 ID |
| `positive` / `negative` | 正面/负面提示词。极简版自动注入；专业版需配合占位符 |
| `seed` | `-1` 表示每次随机；其余值在 ≤0xFFFFFFFFFFFFFFFF 范围内 |
| `timeout` | 最长等待秒数，60–3600。视频工作流建议调大 |
| `poll_interval` | 轮询间隔（秒），默认 5 |
| `use_cache` | 同参数命中本地缓存时跳过云端调用，**不扣费** |
| `use_high_performance` | 切到高性能实例（`instanceType: plus`），消耗更多额度 |

**极简版独有**

| 参数 | 说明 |
|------|------|
| `on_missing_placeholder` | 云端没找到占位符时：警告继续（默认）/ 报错中断 |

**专业版独有**

| 参数 | 说明 |
|------|------|
| `node_overrides` | 参数映射 JSON，支持 `{positive}` `{negative}` `{seed}` 占位符 |
| `validate_mapping` | 开启后会把 nodeId / fieldName 拿去和云端结构核对，写错就明确提示 |

### 参数注入的三种写法

标准写法：
```json
[{"nodeId": "3", "fieldName": "seed", "fieldValue": 123}]
```

字典简写（插件会自动展开）：
```json
{"3": {"seed": 123, "steps": 20}, "6": "一段提示词"}
```

写成 `value` 也没关系——插件会自动纠正为官方要求的 `fieldValue` 并打印提示。
这是 RunningHub API 最容易踩的坑。

## 低显存配置建议（6G / RTX 3060 级别）

本插件输出的是 latent，本身几乎不占显存；**显存压力集中在下游 VAE 解码**。
推荐用 ComfyUI 原生 **VAEDecodeTiled** 替代 VAEDecode：

| 显存 | tile_size | overlap | 说明 |
|------|-----------|---------|------|
| 4–6 GB | 384–512 | 32–64 | 必须分块，否则长视频/4K 必崩 |
| 8–12 GB | 512–1024 | 64 | 平衡画质与显存 |
| 16 GB+ | 不适用（用 VAEDecode） | — | 全速解码，无额外限制 |

视频 latent（5 维）建议先把帧数控制在合理范围，再按帧分批解码：长视频一次性
走完整 VAE 会在中间态产生巨大的 5D 张量，分块只能减轻空间维度，减轻不了帧维度。

## 常见问题排查

**提示词填了但没生效（最重要的一条）**

分三种情况，全部都会被明确告知，不会静默失败：

1. **极简版，云端没写占位符** → 节点变黄 + report 提示
   「未找到 {positive} 占位符」。去云端文本节点把内容改成 `{positive}` 即可。
2. **专业版，映射里没写 `{positive}`** → report 提示
   「参数映射里没有出现 {positive} 占位符」，并给出正确的 JSON 片段。
3. **nodeId 换工作流后失效** → 打开 `validate_mapping`，会直接告诉你
   「nodeId『6』在云端工作流里不存在」。

不确定就先跑一次 **RH 工作流参数探查**，它会打印占位符命中数和种子命中字段。

**`云端任务已完成，但没有产出 latent 文件`**
云端工作流末端没接「保存Latent」，或该节点被短路了。按上面的第一步检查。
报错里会列出云端实际输出了哪些文件，照着排查即可。

**`参数注入不合法`（code 803）**
nodeId / fieldName 与云端不一致，或者用了 `value` 而非 `fieldValue`。
用「RH 工作流参数探查」节点核对。

**`鉴权失败`（code 412 / 812）**
API Key 无效、过期，或该 Key 类型无权调用工作流 API。工作流 API 支持
消费级-会员 / 企业级-共享 / 企业级-独占三种 Key。

**`并发任务数已达上限`（code 421）**
消费级会员通常同时只能跑 1 个任务。等当前任务结束再提交。

**出图是一片噪点**
几乎一定是 **VAE 不匹配**。云端用什么底模生成的 latent，本地就必须用对应家族的
VAE 解码（SDXL 的 latent 用 SD1.5 的 VAE 解码必崩）。参考云端工作流里用的 VAE。

**视频 latent 形状报错**
确认云端输出的是 5 维 `[B, C, F, H, W]`，且本地接的是**支持视频的 VAE**。
原生 VAEDecode 对 5 维输入需要视频类 VAE（如 Wan 系列）。

**生成到一半卡住**
主节点是阻塞式的，会占住队列直到完成或超时。`timeout` 给足（视频给到 3600），
超时后 taskId 会在 report 里给出，用「RH 按任务号取回」续取，不必重新付费。

## 项目结构

```
ComfyUI-RunningHub-Latent/
├── __init__.py            节点注册 + WEB_DIRECTORY
├── nodes.py               5 个节点的 UI 定义与业务流程
├── core/
│   ├── client.py          RunningHub OpenAPI 封装（重试 / 错误映射 / 脱敏）
│   ├── latent.py          latent 解析，对齐原生 LoadLatent
│   ├── workflow.py        工作流结构解析 + 提示词全局替换 + 映射校验
│   ├── cache.py           按参数哈希的本地 latent 缓存
│   └── settings.py        config.ini / config.json 读取
├── web/
│   └── rh_warning.js      前端扩展：警告时给节点上色、拉高提示词输入框
├── tests/
│   ├── test_core.py       核心逻辑冒烟测试（104 项，不需要 API Key）
│   └── test_package.py    交付物一致性检查（节点定义 vs 示例工作流）
├── examples/              4 份示例工作流（极简 / 专业 / 视频 / 二采）
├── config.example.ini
└── config.example.json
```

## 运行自测

不需要 RunningHub 账号，也不产生任何费用：

```bash
cd ComfyUI-RunningHub-Latent
python tests/test_core.py      # 核心逻辑 104 项断言
python tests/test_package.py   # 节点定义与示例工作流是否一致
```

`test_core.py` 用真实的 safetensors 文件验证 latent 解析的新旧格式与缩放行为，
用 mock session 走完「排队 → 运行 → 成功 / 失败 / 空结果补查」的完整状态机，
并覆盖提示词替换、种子定位、映射校验、config.ini 解析等全部新增逻辑。

## 设计说明

**为什么没有基于 ComfyUI_NetDist 改造**

立项文档原本要求 fork `ComfyUI_NetDist` 做二次改造，实测下来这个方案不成立：

1. NetDist 是「ComfyUI 实例 ↔ ComfyUI 实例」的局域网协议（`/prompt` + `/view`），
   与 RunningHub 的 HTTP OpenAPI 毫无交集，改造点几乎覆盖全部代码；
2. 真正可复用的只有 latent 文件解析，本质是 ComfyUI 原生的 `LoadLatent` 行为，
   总共约 20 行，直接照数据格式实现更干净；
3. 更关键的是 **NetDist 采用 Apache-2.0**，直接复制其代码后改用 MIT 发布会产生
   协议冲突。本项目因此采用独立实现，仅在 LICENSE 中致谢其设计思路。

**latent 解析的关键细节**

ComfyUI 原生 SaveLatent 的产物结构如下（`latent_format_version_0` 是一个**空张量**，
而不是 safetensors 的 metadata）：

```python
{"latent_tensor": tensor, "latent_format_version_0": torch.tensor([])}
```

缺少这个标记的老文件存的是被缩放过的值，加载时要乘回 `1/0.18215`；带标记的新文件
已经是 VAE 原始空间，**绝不能再乘**——误乘会让画面直接崩成噪声。本插件严格遵循这条
规则，不做任何「贴心」的自动推断。

> 顺带一提：`safetensors.torch.load_file()` 并不会返回 metadata，所以这个标记必须
> 作为张量存在于容器中才能被 `"..." not in latent` 检测到。这也是为什么不能用 metadata。

## 参考文档

- RunningHub OpenAPI 官方文档：<https://www.runninghub.cn/runninghub-api-doc-cn>
- 工作流 API 完整集成示例：<https://www.runninghub.cn/runninghub-api-doc-en/doc-8287472>
- ComfyUI 原生 SaveLatent / LoadLatent：`nodes.py`

## 合规与免责

- 本项目仅用于把云端的算力接入本地 ComfyUI 工作流，**不包含、也不会实现任何**
  用于伪造身份、伪装流量或对抗检测的功能；
- **请遵守所在地法律法规以及 RunningHub 平台的服务条款**。任何内容层面的违法违规行为
  由使用者自行承担全部责任；
- API Key 属于个人凭证，请妥善保管，切勿提交到公开仓库（本项目已用 `.gitignore` 排除）；
- 本项目与 RunningHub 官方无从属关系，仅为社区第三方工具。

## License

MIT License，详见 [LICENSE](./LICENSE)。
