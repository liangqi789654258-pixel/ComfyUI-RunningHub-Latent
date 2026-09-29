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

重启 ComfyUI，在节点列表里搜索 **RunningHub** 分类即可看到 8 个节点。

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

### 第二步：在云端文本节点里写占位符（要在本地改提示词才需要）

打开云端工作流的**正面提示词**文本框，把内容改成：

```
{positive}
```

负面提示词框同理改成 `{negative}`。可以带前后缀，例如
`masterpiece, {positive}, best quality` —— 前后缀会被保留。

> 这一步是「在本地改提示词」的前提。忘了写的话节点**会明确警告**（画布上变黄
> + report 里写明），不会静默失效。云端不写占位符也完全可以用——
> 只是本地输入框填了提示词也不会生效，会沿用云端默认文案。

### 第三步：本地跑通

- **极简版**：拖 `RH 云端采样 · 极简版`，只有 `api_key` 和 `workflow_id`
  两个输入框，填完把 latent 接到本地 `VAEDecode` / `VAEDecodeTiled` →
  `SaveImage`。完事。提示词直接在云端工作流里改好；每次运行自动换随机种子。
- **专业版**：同样开箱即用（自动注入提示词与种子）。只有想本地调
  步数 / CFG / 尺寸等云端参数时才需要填 `node_overrides`（见下节）。

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
| **RH 云端采样 · 极简版** | **只有 2 个输入框**（api_key、workflow_id），填完即用；种子每次自动随机 | 新手 / 云端调好直接出图 |
| **RH 云端采样 · 专业版** | 自动注入提示词与种子 + `node_overrides` 完整参数映射 | 需要本地调 step / cfg / 尺寸 / 二采 denoise |
| **RH 按任务号取回 Latent** | 用已有 taskId 重新拉取（超时续取、换会话复用） | 所有人都该知道有这个 |
| **RH 从 URL 加载 Latent** | 直接给一个 latent 下载链接加载（手动下载场景） | 手动下载场景 |
| **RH 工作流参数探查** | 拉取云端工作流结构与可改参数，并判断能否自动定位 | 第一次配某条工作流时 |
| **RH AV Latent 保存** | 替代原生「保存Latent」，能存 MiniMax H3 的音视频复合 latent | 云端跑 H3 时必用 |
| **RH AV Latent 拆分** | 把复合 latent 拆成视频 / 音频两条，分别解码 | H3 本地解码必用 |
| **RH AV Latent 合并** | 把视频 + 音频 latent 组装回复合 latent | 二采 / latent 放大 |

### 两个版本怎么选

两个版本**共用同一套自动注入**：提交前先拉一次云端工作流结构，扫描**所有字符串字段**
里的 `{positive}` / `{negative}` 并替换，同时自动找采样器的 `seed` 字段注入种子。
因为它不认节点类型、只认占位符文本，所以单文本节点、双 CLIPTextEncode、
甚至提示词藏在第三方节点里都能命中。

- **极简版**：把自动注入之外的一切都藏起来。没有提示词输入框（沿用云端文案）、
  没有种子滑块（每次自动随机）、没有 timeout（走 config.ini）。
  想要固定种子或本地改提示词，换专业版。
- **专业版**：自动注入照常生效，`positive` / `negative` 输入框可直接改提示词；
  再叠加 `node_overrides` 手工映射，**映射与自动注入冲突时以映射为准**：

```json
[
  {"nodeId": "3", "fieldName": "steps",    "fieldValue": 20},
  {"nodeId": "3", "fieldName": "cfg",      "fieldValue": 7.0}
]
```

### 共用参数

| 参数 | 说明 |
|------|------|
| `api_key` | 留空则读 config.ini / config.json |
| `workflow_id` | RunningHub 工作流 ID |

**极简版**：没有其他任何输入框。timeout / poll_interval / use_cache /
use_high_performance 全部走 config.ini（见下），不占画布。

**专业版独有**

| 参数 | 说明 |
|------|------|
| `positive` / `negative` | 正面/负面提示词，自动替换云端占位符 |
| `seed` | 每次可固定；配合「生成后控制」实现固定或随机 |
| `node_overrides` | 参数映射 JSON（可选，默认留空）。支持 `{positive}` `{negative}` `{seed}` 占位符 |
| `timeout` | 最长等待秒数。视频工作流建议调大 |
| `poll_interval` | 轮询间隔（秒），默认 5 |
| `use_cache` | 同参数命中本地缓存时跳过云端调用，**不扣费** |
| `use_high_performance` | 切到高性能实例（`instanceType: plus`），消耗更多额度 |
| `validate_mapping` | 校验映射的 nodeId / fieldName 是否真实存在，写错在 report 提示 |

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

### 视频 latent（5 维）怎么用

完全可行，而且**对中小显存是最划算的用法**：视频采样的显存开销全在云端，
本地只承担 VAE 解码。链路和图片一模一样，只有三点不同：

1. **必须加载视频 VAE**（如 `wan_2.1_vae.safetensors`）。
   如果误用图片 VAE，ComfyUI **不会报错**——它在
   `comfy/sd.py` 里对「2 维 VAE + 5 维输入」的处理是 `samples_in[:, :, 0]`，
   也就是**静默只解第一帧**。你会以为在出视频，实际只得到一张图。
2. **必须用 VAEDecodeTiled，并调小时间维度的分块**。
   节点 advanced 区里的 `temporal_size` / `temporal_overlap` 只在视频 VAE 上生效，
   控制「一次解多少帧」。这是 6GB 显存下最有效的旋钮：

   | 显存 | tile_size | overlap | temporal_size | temporal_overlap |
   |------|-----------|---------|---------------|------------------|
   | 4–6 GB | 320–384 | 64 | 16–32 | 8 |
   | 8–12 GB | 512 | 64 | 32–64 | 8 |
   | 16 GB+ | 640+ | 64 | 64+（或直接用 VAEDecode） | 8 |

   装不下就继续降 `temporal_size`，它比调 `tile_size` 见效快得多。
3. **解码输出是帧序列 IMAGE**，接 VHS 的 `VHS_VideoCombine` 成片即可
   （需要 ffmpeg；缺的话 `pip install imageio-ffmpeg`）。

时序衔接：`temporal_overlap` 就是用来补偿分段解码的段边界的，给到 8 基本无感。

> ⚠️ `temporal_size` 的单位是**像素帧**，不是 latent 帧。节点内部会先除以时间压缩比
> （Wan = 4），所以填 `16` 表示一次解 4 个 latent 帧、输出 16 帧像素。
> **默认值 64 在 6GB 显存上会 OOM**，务必手动调小。

建议先 33–81 帧试跑，`timeout` 给到 1800 以上（视频采样远慢于图片，默认 600 秒不够）。
完整示例见 `examples/03_video_latent.json`。

**端到端视频出片（云端工作流怎么搭、参数速查、分步验证清单、故障速查表）** 见
[`docs/video-guide.md`](docs/video-guide.md)。

**MiniMax H3 这类「一次同时生成视频和音频」的模型** 见
[`docs/h3-av-latent.md`](docs/h3-av-latent.md)。要点：H3 的 latent 不是单个张量，
而是 `NestedTensor(视频 latent, 音频 latent)`，原生「保存Latent」存不了它
（会报 `'NestedTensor' object has no attribute 'contiguous'`），需要改用
**RH AV Latent 保存** 节点；拿回本地后必须先用 **RH AV Latent 拆分** 拆成两条再
分别解码，否则音频会被静默丢弃。

## 常见问题排查

**提示词填了但没生效（最重要的一条）**

所有情况都会被明确告知（节点变黄 + report 写明），不会静默失败：

1. **云端没写占位符，映射里也没引用** → report 提示
   「没有找到 {positive} 占位符」。两种修法：去云端文本节点把内容改成
   `{positive}`（推荐）；或在 `node_overrides` 映射里用 `{positive}` 指定注入位置。
2. **nodeId 换工作流后失效** → 打开 `validate_mapping`，会直接告诉你
   「nodeId『6』在云端工作流里不存在」。
3. **极简版没找到种子字段** → report 提示沿用云端默认种子（可能重复出图）。

不确定就先跑一次 **RH 工作流参数探查**，它会打印占位符命中数和种子命中字段。

**接了文本处理节点后提示词不生效，而且不报错**

这是 ComfyUI 的一个通用机制，不是本插件的问题，但很容易踩：

> 输入框一旦被转换成输入接口并连上节点，**框里手写的文字就彻底失效了**，
> 值完全由连线决定。

排查顺序：

1. 先看 report 里的「**本次提交的正面提示词：…**」——这一行显示的是真正送进
   云端的内容。如果它不是你写的那串，说明链路接错了（常见的是接到了上游
   节点的 help / 说明类输出口，而不是它的文本输出口）。
2. 云端工作流里如果文本节点前面也接了处理节点，插件会**沿连线向上游回溯**
   找到真正能写入的字段；上游有多个文本字段且都没写占位符时会明确报警
   「无法判断该注入哪一个」，此时请在 `node_overrides` 里直接指定节点。
3. 上游输出空字符串时，插件现在会报警「提示词是空的，但云端有 N 处
   {positive} 标记」——否则云端提示词会被替换成空内容且一声不响。

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

**视频 latent 形状报错 / 只出了一张图**
确认云端输出的是 5 维 `[B, C, F, H, W]`，且本地接的是**支持视频的 VAE**。
如果没报错但只出来一张图，就是加载了图片 VAE——ComfyUI 会静默只取第一帧，
换成 `wan_2.1_vae` 这类视频 VAE 即可（详见上面的「视频 latent 怎么用」）。

**云端报 `'NestedTensor' object has no attribute 'contiguous'`**
这是 MiniMax H3 专用场景。H3 的 latent 是「视频 + 音频」的复合对象，原生
「保存Latent」处理不了，换成 **RH AV Latent 保存** 节点即可。
拿回本地后记得用 **RH AV Latent 拆分** 拆开再解码——不拆的话音频会被静默丢掉。
完整说明见 [`docs/h3-av-latent.md`](docs/h3-av-latent.md)。

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
