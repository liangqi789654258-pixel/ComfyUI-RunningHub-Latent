# 多平台适配改造方案

> 文档状态：**待确认**（确认后再进入开发实施）
> 版本：**v1.1** ｜ 日期：2026-09-29 ｜ 适用插件版本：v1.2.2
>
> **v1.1 变更**：依据贵方提供的 liblibAI 飞书文档，**修正** v1.0 中「liblib 属 B 类、能力上不支持」的判断
> —— liblibAI 确有工作流 API 且支持个人本地 ComfyUI 工作流，重新归类为 **A- 类**。
> 新增 1.5 专项评估、2.3/2.4 的语义参数注入模型、P0.5 验证门禁与 P1+ 附加工作量、7.3 最小可行性验证清单。
> **v1.0 的 1.1–1.3 节对比表仍成立，但对比的是 liblib 的「生图 API」而非「工作流 API」**，请勿据此否定其工作流能力。

---

## 一、适配性判断结论

### 结论：场景 2 —— 当前插件**仅专属适配 RunningHub**，不具备通用性

判断依据来自三个维度的实地核查（本机插件源码 + RunningHub 官方 API 文档 + 同类平台公开接口文档），不是经验推断。**协议仅在「API Key + 工作流 ID」这一表层交互模式上相似，底层无任何一致性。**

### 1.1 底层 API 协议维度

| 对比项 | RunningHub（本插件现状） | liblibAI 开放平台 | 是否通用 |
|---|---|---|---|
| 鉴权方式 | 单 API Key，放在**请求体** `apiKey` | **AccessKey + SecretKey 双密钥**，HMAC-SHA1 签名，签名参数放 **URL query**（`Signature`/`Timestamp`/`SignatureNonce`） | ✗ 完全不同 |
| 鉴权时效 | 无时效 | Timestamp 误差 >300 秒即失效；SignatureNonce 每次唯一 | ✗ |
| 端点路径 | `/task/openapi/create`、`/task/openapi/outputs`、`/api/openapi/getJsonApiFormat` | `/api/generate/webui/text2img`、`/api/generate/webui/status` | ✗ |
| 额外头部约束 | `Host` 头必须精确匹配域名 | 无此约束 | ✗ |
| 域名 | `www.runninghub.cn` | `openapi.liblibai.cloud` | ✗ |

### 1.2 接口参数规范维度（**差异最致命的一层**）

| 对比项 | RunningHub | liblibAI |
|---|---|---|
| 任务标识 | `workflowId` | `templateUuid` |
| 参数注入粒度 | **节点级**：`nodeInfoList: [{nodeId, fieldName, fieldValue}]`，可改任意节点的任意字段 | **无节点级注入**：只有扁平的 `generateParams`（`checkPointId`/`prompt`/`negativePrompt`/`steps`/`cfgScale`/`width`/`height`/`seed`/`hiResFixInfo`） |
| 注入字段名 | `fieldValue`（**不是 `value`**，官方接口最易踩的坑） | 不适用 |
| 任务 ID 字段 | `taskId` | `generateUuid` |
| 输出结构 | `data: [{fileUrl, fileType, taskCostTime, nodeId}]` | `images[].imageUrl` / `videos[].videoUrl` |
| 平台私有参数 | `addMetadata` / `webhookUrl` / `workflow` / `instanceType` / `usePersonalQueue` / `accessPassword` | `randnSource` / `restoreFaces` 等 |

### 1.3 任务流转逻辑维度

| 对比项 | RunningHub | liblibAI |
|---|---|---|
| 状态判定 | 业务状态码：`0` 成功 / `804` 运行中 / `813` 排队中 / `805` 失败 | 字符串状态 + `code=0` |
| 错误码体系 | 803 / 805 / 813 / 412 / 812 / 421 / 402 / 400 / 814（全部平台私有） | 平台私有 |
| 失败详情 | `data.failedReason.{node_name, exception_message, traceback}`（**节点级**报错，这是本插件报错可读性的来源） | 未见等价结构 |
| 响应信封 | `{code, msg, data}` | `{code, msg, data}`（**唯一恰好一致的点**） |

### 1.4 一个必须先讲清楚的前提：平台要分两类

这是整个方案的关键，直接影响工作量判断，请勿跳过。

本插件的核心价值是「**云端只做采样，输出 latent，本地解码**」。要成立，目标平台必须同时满足三个能力：
① 能执行**用户自己的 ComfyUI 工作流**（不是平台封装好的固定模板）；② 支持**参数注入**（否则无法把提示词/种子送进去）；③ 能**输出 latent 文件**。

据此把市面平台分成三类：

| 类型 | 特征 | 代表 | 本插件可适配性 |
|---|---|---|---|
| **A 类：完整工作流执行器** | 执行任意 ComfyUI 工作流 + **节点级任意注入** + 可输出 latent | RunningHub、ViewComfy、RunPod/Replicate（自建） | ✅ 直接适配 |
| **A- 类：受限工作流执行器** | 能跑个人 ComfyUI 工作流，但注入是**白名单语义参数**、latent 输出待验证 | **liblibAI（见 1.5）**、吐司等 | ⚠️ 需先验证 latent 输出，再评估 |
| **B 类：封装式能力 API** | 只暴露固定生图/视频接口，参数是扁平的提示词+尺寸+种子 | 可灵、即梦类 API | ❌ 能力上不支持 |

**因此：多平台适配 ≠ 改一套命名和文案，而是「先做架构抽象（P0），再逐个平台验证能力并写适配器（P1）」。B 类平台不在本方案范围内。**

---

### 1.5 liblibAI 专项评估（2026-09-29 更新，**修正了 v1.0 的判断**）

> v1.0 曾依据「liblib 开放平台生图 API（templateUuid + generateParams）」将其判为 B 类。
> 经贵方提供飞书文档并交叉核实，**该判断有误**：liblibAI 另有**工作流 API**，明确支持个人本地 ComfyUI 工作流调用。

#### 已确认的事实

| # | 事实 | 来源 |
|---|---|---|
| 1 | 「LiblibAI 工作流：社区商用工作流和**个人本地工作流均可支持调用**」 | 飞书文档（贵方提供）正文 |
| 2 | 个人本地工作流需先发布至平台：发布-工作流 → 上传并配齐参数 → 纸飞机 → **编辑 AI 应用** → 勾选开放给用户配置的参数并命名 → **运行后发布** | 交叉验证 + 贵方补充 |
| 3 | 发布时可见性可选【自见】；**必须勾选【生成图片可出售或用于商业目的】** | 贵方补充 |
| 4 | 社区工作流需详情页显示「本工作流已提供 API 服务」及 API 参数，否则不支持 API 调用 | 交叉验证 |

#### 协议层（第三方描述，**待以飞书文档原件核实**）

| 项 | 内容 |
|---|---|
| 端点 | `POST /v1/workflow/run` |
| 鉴权 | `Authorization: Bearer {api_key}`（**比旧版 AccessKey+SecretKey 的 HMAC-SHA1 签名简单**） |
| 主体参数 | `workflow_id`（`/lbb/` 开头，如 `/lbb/flux-kontext-multi-image-v2`）、`image_url`、`params`（键值对象） |
| 返回 | `code=0` + `task_id`，之后轮询或用 HTTPS 回调（回调需校验 `X-Liblib-Signature`） |

> ⚠️ 上述端点信息来自第三方技术站点，**可信度中等**。飞书文档正文需登录态，本次仅取回开头章节。
> 请以文档原件为准，这一项列入 7.1 待确认。

#### 结论：liblibAI 归为 **A- 类**，但有两个决定性差异

**差异一（确定存在）：参数注入模型完全不同，这是本次改造最大的新增工作量**

| | RunningHub | liblibAI |
|---|---|---|
| 注入粒度 | **任意节点的任意字段**：`nodeInfoList[{nodeId, fieldName, fieldValue}]` | **仅作者发布时勾选开放的参数**，且是作者命名的语义名（如 `expand_top`、`正向提示词`） |
| 插件能否自动发现 | ✅ 能（拉工作流结构扫描所有字符串字段找 `{positive}`） | ❌ **不能**，平台不暴露工作流节点结构 |

**这直接击穿了本插件现有的自动注入机制**——极简版的"零配置自动定位提示词"在 liblib 上**无法成立**。
解决方向：适配器需增加一层「**参数名映射表**」（平台参数名 ↔ 本地语义 `positive/negative/seed/width/height/frames`），由用户在节点或配置里声明一次。这会让 P1 工作量上调（见 6.1）。

**差异二（未确认，且是唯一门禁）：能否输出 latent**

liblib 的工作流是发布为「AI 应用」，其输出形态大概率是图片。要让平台吐出 latent，需要：工作流末端是 ComfyUI 的 `SaveLatent` 节点，且平台把 `.latent` 文件作为输出返回。

> **这一条决定 liblib 接入的生死。** 若平台只返回图片，接入 liblib 就退化成"普通云端出图"，本插件的核心价值（云端采样 + 本地解码）荡然无存，投入应全部撤回。

建议按 7.3 的「最小可行性验证」先花 1–2 人日验证，通过再启动 P1。

---

## 二、整体架构设计

### 2.1 分层架构

现状是「节点逻辑 ↔ RunningHub 客户端」两层硬耦合。改造目标是在中间插入**平台适配层**：

```
┌─────────────────────────────────────────────┐
│ L4 节点层  nodes.py                          │  输入/输出/report/前端告警
├─────────────────────────────────────────────┤
│ L3 编排层（平台无关，已具备，基本不动）        │
│   · 占位符扫描与提示词注入（{positive} 等）    │
│   · 云端工作流结构回溯（上游连线追踪）         │
│   · 轮询等待 / 超时 / 空结果补查              │
│   · latent 解析与缩放规则                    │
│   · 本地缓存                                 │
├─────────────────────────────────────────────┤
│ L2 平台适配层（★ 新增，本次改造核心）         │
│   CloudPlatform 抽象接口 + 各平台适配器       │
│   · RunningHubAdapter（封装现有 client）      │
│   · <Future>Adapter（ViewComfy / 吐司 / …）   │
│   · Capabilities 能力声明 + 降级策略          │
├─────────────────────────────────────────────┤
│ L1 HTTP 层   requests（含重试/脱敏）          │
└─────────────────────────────────────────────┘
```

**关键：L3 编排层与 L0 latent 语义本来就是平台无关的**（latent 文件是 ComfyUI 原生 safetensors 格式，任何平台只要用 SaveLatent 产出，格式都一样）。改造真正要动的是 L2，L3 几乎不动 —— 这也是本次改造工作量可控的原因。

### 2.2 统一接口定义

```python
class CloudPlatform(ABC):
    """平台适配层统一契约。每个平台实现一个子类。"""

    name: str                    # 内部标识："runninghub" / "viewcomfy"
    display_name: str            # 面向用户："RunningHub" / "ViewComfy"
    capabilities: Capabilities   # 能力声明，见 2.4

    # ---- 任务生命周期 ----
    def submit_task(self, workflow_id: str,
                    injections: List[Injection],
                    options: SubmitOptions) -> str:
        """提交任务，返回平台原生任务 ID（字符串）。"""

    def fetch_status(self, task_id: str) -> TaskState:
        """查询一次状态，返回统一枚举，不抛业务异常。"""

    def fetch_outputs(self, task_id: str) -> List[OutputFile]:
        """任务成功后取输出文件列表。"""

    # ---- 可选能力（能力不支持时返回空而非报错）----
    def fetch_workflow_structure(self, workflow_id: str) -> dict:
        """拉取云端工作流结构，供自动定位提示词/种子字段。"""

    def upload_input(self, file_path: str) -> str:
        """上传输入文件（图生图场景），返回平台侧文件名。"""

    # ---- 错误归一 ----
    def classify_error(self, raw: Exception) -> PlatformError:
        """把平台私有错误码翻译成统一语义 + 中文提示。"""
```

### 2.3 统一数据模型（平台无关的中间表示）

| 模型 | 定义 | 说明 |
|---|---|---|
| `Injection` | 两种形态：<br>· `NodeInjection(node_id, field_name, value)`<br>· `ParamInjection(param_name, value)` | **本次改造的关键抽象**，必须同时支持两种形态：<br>· A 类平台（RunningHub）用 `NodeInjection` → 映射为 `nodeInfoList[].{nodeId,fieldName,fieldValue}`<br>· A- 类平台（liblib）用 `ParamInjection` → 映射为 `params.{作者定义的参数名}`<br>后者需要一层**语义参数名映射表**（`positive` → 平台上的参数名），由用户声明一次 |
| `TaskState` | `QUEUED / RUNNING / SUCCESS / FAILED` 枚举 | 把 804/813/805 等私有码收归枚举，L3 轮询逻辑只看枚举 |
| `OutputFile` | `(url, kind, node_id)`，`kind ∈ {image, video, latent, audio, other}` | 收编 `fileType`/`outputType`/`type` 三种字段名的差异（v1.1 已踩过这个坑） |
| `PlatformError` | `(code, message, hint)` | 保留平台原始码用于排错，同时给出统一中文提示 |

### 2.4 能力声明与降级（Capabilities）

这是保证「不静默失效」的核心机制 —— 不同平台能力不齐，必须让插件**在提交前就明确告知**，而不是让用户跑完一轮扣了费才发现。

```python
@dataclass
class Capabilities:
    node_level_injection: bool = False   # 节点级任意注入（决定能否用 {positive} 占位符自动扫描）
    semantic_param_injection: bool = False  # 仅支持白名单语义参数（需参数名映射表）
    latent_output: bool = False          # 能否输出 latent（本插件生命线）
    workflow_introspection: bool = False # 能否拉工作流结构（决定极简版"零配置"能否用）
    upload_input: bool = False           # 能否上传输入图
```

**能力组合与行为**（以 liblib 为参照）：

| 能力组合 | 插件行为 |
|---|---|
| `node_level_injection=True` + `introspection=True` | 完整能力：自动扫占位符、自动定位种子（RunningHub 现状） |
| `semantic_param_injection=True` + `introspection=False` | **降级**：提示词/种子需用户声明参数名映射；未声明时明确报警，不静默沿用云端默认值 |
| `latent_output=False` | **提交前直接报错中止**，不提交任务 |

对应需求（EARS）：

- **Ubiquitous**：系统应在节点执行前校验目标平台能力，凡不支持的能力点，须以警告形式明确告知用户。
- **Unwanted**：若目标平台 `latent_output = False`，则系统应在提交任务**之前**报错并中止，不得提交任务（避免产生无法使用的结果并扣费）。
- **Unwanted**：若目标平台 `workflow_introspection = False`，则极简版的自动定位功能应降级为手动指定，并明确提示"该平台不支持自动定位，请在参数映射中指定节点"，不得静默沿用云端默认值。
- **Optional**：若目标平台支持 `upload_input`，则系统应允许上传本地图片作为云端工作流输入。

### 2.5 各平台差异点矩阵（适配器需要消化的差异）

| 差异维度 | 适配器需处理的内容 |
|---|---|
| 鉴权 | 单 Key / 双 Key 签名 / Bearer Token；放 body 还是 header 还是 query |
| 端点与路径 | 每个平台一套路径常量 |
| 参数注入协议 | 节点级注入 / workflow JSON patch / 扁平参数映射 |
| 任务 ID 与状态机 | 字段名 + 状态值到统一枚举的映射表 |
| 输出识别 | 输出列表的取法（list / dict 嵌套）+ 文件类型字段名的归一化 |
| 错误码 | 平台私有码 → 统一中文提示的映射表 |
| 平台专属参数 | 如 RunningHub 的 `instanceType`、并发队列等，放在适配器内部，不污染节点层 |

---

## 三、节点命名策略

### 3.1 不可违背的约束（硬约束，优先级高于命名美观）

> **ComfyUI 节点的「注册名」一旦改动，用户已保存的工作流里该节点会直接断线（节点变红、参数丢失）。**

因此：
- 现有 5 个注册名 **一个都不改、一个都不删**，永久保留；
- 现有输入参数名、顺序、类型 **不删不改不调序**；
- 新增参数只能追加，且优先放 `optional` 或从配置文件读取。

这一条是向后兼容的地基，详见第五章。

### 3.2 命名方案（TITLE 中立化，注册名保持不变）

| 注册名（**保持不变**） | 现 TITLE | 建议新 TITLE | 说明 |
|---|---|---|---|
| `RunningHubLatentGenerator` | RH 云端采样 · 专业版 | **云采样 · 专业版** | 主节点，去掉 RH 前缀 |
| `RunningHubLatentGeneratorSimple` | RH 云端采样 · 极简版 | **云采样 · 极简版** | |
| `RunningHubLatentFetch` | RH 按任务号取回 Latent | **云采样 · 按任务号取回** | |
| `RunningHubLatentFromUrl` | RH 从 URL 加载 Latent | **Latent 加载 · 从 URL** | 该节点本就平台无关，可直接中立化 |
| `RunningHubWorkflowInspector` | RH 工作流参数探查 | **云采样 · 参数探查** | |

**分类（CATEGORY）**：现状 `CATEGORY = "RunningHub"`，决定节点在右键菜单里的位置。建议改为 **`"Cloud Latent"`**（或 `"云采样"`），符合 ComfyUI 社区用英文名做分类的习惯。

> 注：CATEGORY 变更会让用户找不到原来的菜单位置。需在 CHANGELOG 与 README 显著位置说明一次，属于可接受的一次性成本。

### 3.3 多平台模式下的辨识度处理

改造后同一节点可服务多个平台，**平台信息改由以下位置呈现**（而不是塞进节点名）：
1. 节点新增 `platform` 下拉（P1 阶段，放 optional，默认读配置）；
2. report 输出第一行写明本次调用的平台；
3. 报错信息统一前缀 `[平台名]`。

这样节点名保持中立稳定，辨识度由运行期信息承担 —— 也避免了"每接入一个平台就多一套节点"的节点爆炸问题。

### 3.4 命名相关需求（EARS）

- **Ubiquitous**：系统应保证所有既有节点注册名在改造前后完全一致，确保历史工作流可正常加载。
- **Event-driven**：当用户新建节点并选择非默认平台时，系统应在 report 中回显本次实际调用的平台名称。
- **State-driven**：在节点未显式指定平台时，系统应沿用配置文件中的 `active` 平台。

---

## 四、配置结构调整

### 4.1 目标结构（多平台密钥并存）

```ini
; ---------- 平台选择 ----------
[platform]
active = runninghub          ; 当前默认平台（节点上不指定时用这个）

; ---------- 各平台配置 ----------
[runninghub]
api_key = xxxx
base_url = https://www.runninghub.cn
default_workflow_id =
timeout = 600
poll_interval = 5
use_cache = true
use_high_performance = false

[viewcomfy]
api_key = yyyy
base_url = https://...

[tusiart]
api_key = zzzz
```

### 4.2 兼容与优先级

- **优先级**：节点输入 > 当前平台节配置 > 全局默认（与现有逻辑一致，不引入新的心智负担）。
- **旧配置兼容**：现有 `config.ini` 只有 `[runninghub]` 一节，无 `[platform]` 时，系统应默认 `active = runninghub` —— 老配置文件**无需任何修改**即可继续使用。
- 现有 `core/settings.py` 的 `_read_ini` 已支持按 section 读取与合并，改造成本很低。

### 4.3 需求（EARS）

- **Unwanted**：若配置文件中缺少 `[platform]` 节，则系统应默认使用 `runninghub`，不得报错。
- **Unwanted**：若 `active` 指定的平台不存在对应配置节，则系统应报明确错误（列出可用平台名），不得回落到其它平台静默执行。

---

## 五、向后兼容方案

现有 RunningHub 用户**零感知、零迁移**是本次改造的硬要求。具体保证：

| # | 保证项 | 落地手段 |
|---|---|---|
| 1 | 历史工作流不失效 | 5 个注册名、输入签名、输出签名全部不变 |
| 2 | 旧配置文件不用改 | 缺 `[platform]` 时默认 runninghub；旧扁平字段继续识别 |
| 3 | 报错质量不下降 | RunningHub 私有错误码（803/805/813/412/812/421/402）与 `failedReason` 节点级报错**全部保留**，适配器原样透传 |
| 4 | 行为零变更 | P0 阶段只做内部重构，不改动任何对外可观察行为；以现有 **130 项自测 + 包一致性测试全绿**作为准入门槛 |
| 5 | 关键语义不丢 | latent 缩放规则（带 `latent_format_version_0` 不缩放）、5 维视频 latent、占位符注入与 0 命中告警，全部保持 |

**P0 阶段的验收方式**：改造前后，用同一条工作流、同一组参数各跑一次，输出的 latent 张量应**逐字节一致**（可比对缓存键/张量 hash）。

---

## 六、改造工作量评估与优先级

### 6.1 优先级分级

| 优先级 | 内容 | 工作量 | 依赖 | 说明 |
|---|---|---|---|---|
| **P0** | 架构抽象：抽出 `CloudPlatform` 接口 + RunningHub 适配器 + 统一数据模型 + Capabilities + `[platform]` 配置 | **3–5 人日** | 无 | **纯重构，对外行为零变更**，风险最低、价值最高（后续所有平台接入都靠它） |
| **P0.5** ★新增 | **latent 输出可行性验证**（最小工作流实测，见 7.3） | **1–2 人日 / 平台** | 无 | **门禁项**：验证不通过则该平台直接出局，P1 不启动。成本极低、避免大额返工 |
| **P1** | 接入第 2 个 A 类平台：适配器 + 真机联调 + 暴露 `platform` 可选输入 | **5–8 人日 / 每平台** | P0 + P0.5 通过 | 联调时间波动大，取决于平台文档质量与额度 |
| **P1+** ★新增 | **A- 类平台附加成本**：语义参数名映射表（配置结构 + UI + 校验 + 文档） | **+2–3 人日 / 平台** | P1 | 仅 liblib 这类"白名单参数"平台需要；A 类平台无此项 |
| **P2** | 命名与文案中立化：TITLE、CATEGORY、报错文案、README/示例工作流 | **1–2 人日** | P0 | 纯粹的面子工程，但对开源项目的观感很重要 |
| **P3** | B 类平台（可灵/即梦类）评估 | 调研 **1 人日** | — | 结论大概率是"不支持"，产出是一份结论而非代码 |

**总计（以 liblib 为第 2 平台测算）：P0(3–5) + P0.5(1–2) + P1(5–8) + P1+(2–3) + P2(1–2) ≈ 12–20 人日。**
若第 2 平台选的是 A 类（如 ViewComfy），则省掉 P1+，回到 9–15 人日。

### 6.2 关键路径与建议节奏

```
平台能力调研（2–3 天，可与 P0 并行）
        ↓
   P0 架构抽象 ──→ P0.5 latent 门禁 ──→ P1 第 2 平台 ──→ P2 中立化
   （3–5 人日）      （1–2 人日）        （5–8 人日）      （1–2 人日）
                          │
                     不通过 → 该平台出局，换下一个候选
        ↓
   发布 v2.0
```

**建议**：P0 可以与"平台能力调研"并行启动 —— P0 不依赖选定哪个平台，先把它做完，后续接平台就是"填适配器"的机械劳动。

### 6.3 优先级判断依据

- P0 先行：它把"接一个新平台"的成本从"改全套代码"降到"写一个类"，是杠杆点；且零行为变更，风险可控。
- P2 放在 P1 之后：如果先改了名字、结果第 2 平台接不进来，等于白改一次名字还让用户困惑。**先用 P1 证明多平台真的成立，再改名字对外宣传**，顺序不能反。

---

## 七、风险与待确认问题

### 7.1 待我方/贵方确认的问题（**这些不确认，P1 无法启动**）

1. **目标平台是哪些？** 请给出明确的优先级列表（本方案中的 ViewComfy / 吐司 仅为示例，非建议）。
2. **【已部分澄清】liblibAI 工作流支持**：已确认支持个人本地工作流（需发布）。仍待确认的是：
   - **能否输出 latent 文件**（决定性，见 7.3）；
   - 飞书文档中工作流 API 的**确切端点、鉴权方式、params 规范**（本次仅取回文档开头，正文需登录态）—— 请提供该章节原文或截图；
   - 平台是否暴露工作流节点结构（决定能否自动定位提示词）。
3. **各目标平台的能力调研由谁做？** 建议按下方清单逐平台核实。
4. **改造期间的额度成本**：P1 联调需要真实提交任务（任务失败也扣费），需预留测试额度。

### 7.2 平台能力调研清单（每个候选平台逐条确认）

| # | 需确认的问题 | 不通过则 |
|---|---|---|
| 1 | 能否执行**用户自己的 ComfyUI 工作流**（而非平台固定模板）？ | 直接判定 B 类，无法适配 |
| 2 | 是否支持**节点级参数注入**（指定 nodeId + 字段名 + 值）？ | 提示词/种子注入失效，核心价值打折 |
| 3 | 能否**输出 latent 文件**（`.latent` / safetensors）？ | **插件核心价值不成立** |
| 4 | 鉴权方式（单 Key / 双 Key 签名 / Token）与时效要求？ | 影响适配器复杂度 |
| 5 | 任务状态机字段、结果列表结构、失败详情字段？ | 影响轮询与报错可读性 |

### 7.3 liblib「latent 输出」最小可行性验证（**P0.5 门禁，建议立即执行**）

目的：用最小成本判定 liblib 是否值得投入。预计 1–2 人日，不通过则整个 liblib 方向出局。

| 步骤 | 动作 | 判定 |
|---|---|---|
| 1 | 本地 ComfyUI 做一个最小工作流：`Checkpoint 加载 → CLIP 文本编码 → EmptyLatent → KSampler → **SaveLatent**（末端）` | — |
| 2 | 按流程发布：发布-工作流 → 配齐参数 → 纸飞机 → 编辑 AI 应用 → 勾选开放参数（提示词/种子）→ 运行后发布。**可见性选自见，务必勾选【生成图片可出售或用于商业目的】** | 发布成功 |
| 3 | 用 API 提交一次任务，拿到 `task_id` 并轮询结果 | `code=0` |
| 4 | **查看返回的 outputs 里文件 URL 的后缀与类型字段** | 🔑 **出现 `.latent` / safetensors → 通过**；只有 `.png/.jpg/.webp` → 不通过 |
| 5 | （若通过）下载该文件，用本插件的 `core/latent.py` 试解析，确认能还原成 LATENT 张量 | 形状与通道数正常 |

**判定标准**：第 4 步若只返回图片，说明平台强制图片输出 → 本插件核心价值在该平台不成立 → 撤回投入，改为只保留"云端出图"的可选降级方案（那是另一条产品线，不在本方案范围）。

> 补充：即便通过，还需确认平台是否允许 `SaveLatent` 这类节点存在于发布的工作流中（部分平台会对节点做白名单限制）。

### 7.4 风险项

| 风险 | 影响 | 应对 |
|---|---|---|
| 目标平台实为 B 类（不支持 latent） | P1 投入打水漂 | 严格执行 7.2 清单，先调研后开发；每平台先做"最小可行性验证"再投入 |
| 平台 API 不稳定/文档缺失 | 联调周期拉长 | 适配器内保留原始响应体透出（现有 `last_raw_body` 机制可复用），便于排错 |
| 改造引入回归，影响现有 RunningHub 用户 | 高（现有用户是直接受影响方） | P0 以 130 项自测全绿 + 张量逐字节比对作为准入门槛 |
| 平台计费：任务失败也扣费 | 测试成本 | 测试用例优先用小参数/低帧数任务；用缓存机制避免重复提交 |

---

## 八、验收标准

1. **P0**：现有 130 项自测 + 包一致性测试全部通过；改造前后同一工作流产出的 latent 张量逐字节一致；旧 `config.ini` 无需修改即可正常工作。
2. **P1**：新平台可完成"提交 → 轮询 → 下载 latent → 输出 LATENT → 本地 VAEDecode 出图"全链路；平台能力不支持时按 2.4 明确报警而非静默。
3. **P2**：节点 TITLE / CATEGORY 无平台专属字样；历史工作流仍能正常加载并运行。
4. **全过程**：ComfyUI 本体零改动（仅改 `custom_nodes` 下插件目录），以 `git status --porcelain` 在 ComfyUI 根目录返回空为凭证。

---

## 附：本方案涉及的关键代码位置（v1.2.2）

| 位置 | 改造阶段 | 说明 |
|---|---|---|
| `core/client.py` | P0 | 现有 RunningHub 客户端，整体包装为 `RunningHubAdapter`，对外 API 保持 |
| `core/platform.py`（新建） | P0 | `CloudPlatform` 抽象接口 + 统一数据模型 + Capabilities |
| `core/adapters/`（新建） | P0/P1 | 各平台适配器 |
| `nodes.py` | P0/P2 | L4 节点层；平台取值改为从适配层获取 |
| `core/settings.py` | P0 | 增加 `[platform] active` 读取 |
| `config.example.ini` | P0 | 多平台配置示例 |
| `README.md` / `CHANGELOG.md` | P2 | 文案中立化 |
