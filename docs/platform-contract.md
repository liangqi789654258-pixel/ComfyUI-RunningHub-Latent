# S1 · RunningHub 平台契约盘点

> 子任务：S1（平台契约盘点，纯调研交付）
> 状态：**已完成（文档取证）**，含 3 项需真机确认项（已给出分层验证方案与脚本）
> 日期：2026-10-05 ｜ 适用插件版本：v1.3.1+
> 关联：[`injection-architecture.md`](injection-architecture.md)

**本文不含任何针对单个工作流的硬编码结论。** 所有结论均来自 RunningHub 官方文档原文或
对平台不变量的推导；凡属未验证项均已显式标注，并给出验证方法。

**证据等级约定：**

| 等级 | 含义 |
|---|---|
| **A** | 官方文档原文（可直接引用，可信） |
| **B** | 多源一致的第三方实现/社区实践（参考，需真机复核） |
| **C** | 未在文档中找到，属**未知**，必须真机验证 |

---

## 一、结论速览

| # | 待查事实 | 结论 | 等级 | 对架构的影响 |
|---|---|---|---|---|
| **1** | `getJsonApiFormat` 是否返回全量节点与 inputs | **是**。返回 `data.prompt`（JSON **字符串**），内容为完整 API-format：`{nodeId: {class_type, inputs, _meta}}` | **A** | Discovery 层可建；`_meta.title` 是宝贵的语义线索 |
| **2** | 是否存在平台参数 schema 接口 | **无**独立 schema 接口。但 API-format 自带 `_meta.title`（节点标题），官方示例脚本的做法也是"拉取后人工挑选" | **A** | 通用性**不能**依赖平台声明，只能走"发现 + 显式绑定" |
| **3** | 注入不存在的 nodeId/fieldName 会怎样 | **硬失败**：`803 APIKEY_INVALID_NODE_INFO`（"节点 ID 或字段名与工作流定义不一致"） | **A** | 存在**零成本负向验证通道**，静默失效可被根除 |
| **4** | 注入**存在但被连线接管**的字段会怎样 | **未知**。官方仅表述"不建议修改"，未说明行为 | **C** | 架构最大不确定项；决定 R4 是否必须守住不变量 A |
| **5** | create 响应能否回显已应用的注入 | **能部分回显**：响应含 `promptTips`（ComfyUI 校验结果 JSON 串，含 `node_errors` / `outputs_to_execute`）；另有 `addMetadata`（默认 true）会把提示词写入图片元数据 | **A** | S7 验证层有**平台原生通道**，无需额外成本 |
| **6** | 有无低成本 dry-run | 无显式 dry-run。但 `workflow` 参数（自定义完整工作流 JSON，**覆盖 workflowId**）+ 803 硬失败 + `promptTips` 三者组合，可构造近似 dry-run | **A**（参数存在）/ **C**（可用性） | 可能改变整体架构，见 §4 |

---

## 二、逐条取证

### 事实 1 · `getJsonApiFormat` 返回全量 API-format —— 等级 A

官方《获取工作流Json》（<https://www.runninghub.cn/runninghub-api-doc-cn/api-425749014>）：

- 请求：`POST https://www.runninghub.cn/api/openapi/getJsonApiFormat`，
  头 `Host: www.runninghub.cn` + `Authorization: Bearer [KEY]`，body `{"apiKey": "...", "workflowId": "..."}`
- 响应：`{code, msg, data: {prompt: "<JSON 字符串>"}}`

官方响应原文（节选）：

```json
"data": {
  "prompt": "{\"3\":{\"class_type\":\"KSampler\",\"inputs\":{\"scheduler\":\"karras\",
    \"negative\":[\"7\",0],\"denoise\":1,\"latent_image\":[\"5\",0],\"seed\":669816362794144,
    \"cfg\":8,\"sampler_name\":\"dpmpp_2m\",\"model\":[\"4\",0],\"positive\":[\"6\",0],
    \"steps\":20},\"_meta\":{\"title\":\"KSampler\"}}, ... }"
}
```

**可确认的三点：**

1. 返回的是**全量**节点与 inputs（不是"可改参数白名单"），每个节点都给。
2. 连线以 `[源nodeId, slot]` 表示，字面量以标量表示 —— **这正是判定 `value_kind` 的依据**。
3. **每个节点带 `_meta.title`**。社区实现（n8n 节点 `n8n-nodes-runninghub-client`）正是解析它得到
   `meta_title: "Positive Prompt" / "Negative Prompt" / "System Prompt"`，
   说明**用户自定义的节点标题会保留在 `_meta.title` 中**。

> ⚠️ **我方现存的直接缺陷**：`core/workflow.py:normalize()` 归一化时只保留
> `class_type` 与 `inputs`，**把 `_meta` 整个丢弃了**（源码 89-96 行）。平台白送的语义线索被我们自己扔掉。
> S2 的 Snapshot 模型必须保留 `_meta`。

### 事实 2 · 无平台参数 schema 接口 —— 等级 A

- 官方接口清单中不存在"参数 schema / 可注入参数列表"类接口，只有 `getJsonApiFormat`（返回整份结构）。
- 官方《Full Workflow Integration Example》（<https://www.runninghub.cn/runninghub-api-doc-en/doc-8287472>）
  给出的**官方自家做法**是：读本地导出的 `api.json` → 打印全部可改节点 → **由使用者手填
  nodeId / fieldName / 新值** → 提交。

> 这条是对架构方向**最有力的佐证**：**平台官方自己的示例脚本都不做语义推断**。
> 因此"通用性来自暴露 + 显式绑定，而非把猜测规则写复杂"这一判断成立，
> 与 ComfyDeploy / RunComfy / ViewComfy 的做法一致。

### 事实 3 · 注入不存在的 nodeId/fieldName = 硬失败 803 —— 等级 A

官方《接口错误码说明》（<https://qpsnl2kplc.apifox.cn/doc-8417467>）原文：

> **803** `APIKEY_INVALID_NODE_INFO` **nodeInfoList 不匹配**
> 节点 ID 或字段名与工作流定义不一致。

配套说明见官方 nodeInfoList 文档（<https://www.runninghub.cn/runninghub-api-doc-en/doc-8287464>）：

> "If a fieldName cannot be found in the API-format workflow, that means it's a
> **purely front-end field** that only works in the browser — for example,
> `control_after_generate` in KSampler."

**推论（架构级）：**

- 平台在**提交阶段**就对 nodeId/fieldName 做存在性校验 → **不存在的落点不会静默通过**。
- 因此"显式绑定（R1）"这条最保守的路线，可以做到**近乎零静默失效**：
  绑定写错 → 803 报错，而不是悄悄用默认值跑完（正是本次故障的表现）。
- 这条同时为 S3 的**漂移检测**提供了现成信号：工作流改动导致 nodeId 失效 → 803。

> ❗ 注意：第三方文档（如某 rh.md 笔记）把 803 写成"排队队列已满"，与官方 apifox 表冲突。
> **以官方 apifox 错误码表为准**。我方 `core/client.py:_ERROR_HINTS[803]` 的现有文案
> （"参数注入不合法…"）与官方语义一致，无需修改。

### 事实 4 · 注入被连线接管的字段 —— 等级 C（未知，架构最大不确定项）

官方表述（同一份 nodeInfoList 文档，Note 3）：

> "In API-format workflows, a `fieldValue` wrapped in `[]` typically indicates a
> **connection (wiring) between nodes**. It is **not recommended** to modify these values."

**关键辨析：** 官方只说"不建议"，**未说明改了会怎样**。且从错误码语义看：

- 该字段**存在于**工作流定义中 → 不满足 803 的触发条件（"与工作流定义不一致"）；
- 因此有三种可能，文档无法区分：
  - (a) 平台接受并在服务端覆盖连线（注入生效，但语义上覆盖上游）；
  - (b) 平台接受但运行时被上游值覆盖（**静默失效** —— 最坏情况）；
  - (c) 校验失败，报 `433 VALIDATE_PROMPT_FAILED`（"工作流校验未通过 节点参数或连接逻辑错误"）
      或 `1101 Node info error`。

这三种情况对应的架构选择完全不同，因此**必须真机验证**（见 §5 的 L2）。
在此之前，**不变量 A（只向字面量字段注入）应继续作为强制约束**，因为它对三种情况都安全。

### 事实 5 · create 响应可回显校验结果 —— 等级 A

官方《发起ComfyUI任务2-高级》（<https://qpsnl2kplc.apifox.cn/api-435315561>）成功响应原文：

```json
{
  "code": 0,
  "msg": "success",
  "data": {
    "netWssUrl": null,
    "taskId": "1910246754753896450",
    "clientId": "e825290b08ca2015b8f62f0bbdb5f5f6",
    "taskStatus": "QUEUED",
    "promptTips": "{\"result\": true, \"error\": null, \"outputs_to_execute\": [\"9\"], \"node_errors\": {}}"
  }
}
```

官方字段说明：

| 字段 | 说明（官方原文） |
|---|---|
| `promptTips` | **ComfyUI 校验信息（字符串格式的 JSON），可用于识别配置异常节点** |
| `taskStatus` | 初始状态：`QUEUED` / `RUNNING` / `FAILED` |
| `clientId` | 平台内部标识，用于排错 |

官方《使用建议》原文：

> ⚠️ 如果返回 `promptTips` 含有错误信息，请根据 `nodeId` 精确排查问题

**这是本次调研最有价值的发现之一：** `promptTips` 提供 `node_errors` 与 `outputs_to_execute`
——即**提交瞬间**就能知道"图是否被校验通过、哪些节点有问题、哪些输出节点会被执行"。

> ⚠️ **我方完全未使用**：`core/client.py:create_task()` 只取 `data["taskId"]`，
> `promptTips` / `taskStatus` 被丢弃。这是 S7 验证层**现成的、零成本的**平台原生通道。

**第二条回显通道 —— `addMetadata`（可选参数，默认 `true`）：**

> `addMetadata` boolean 默认 true：**是否在图片中写入元信息（如提示词）**

即平台会把实际生效的提示词写进输出图片（PNG 元数据）。下载图片后读取元数据，
即可**事后验证**注入是否真正生效 —— 这正是 S7 需要的"受控验证手段"。

### 事实 6 · dry-run 与 `workflow` 参数 —— 参数存在（A）/ 可用性（C）

同一份高级接口文档列出的可选参数：

| 参数 | 默认 | 官方说明 |
|---|---|---|
| `addMetadata` | true | 是否在图片中写入元信息（如提示词） |
| `webhookUrl` | 无 | 任务完成后回调 URL（推荐仅开发人员使用） |
| **`workflow`** | 无 | **自定义完整工作流（JSON 字符串），如指定则忽略 `workflowId`** |
| `instanceType` | 无 | 指定实例类型（`default` 24G / `plus` 48G / `ultra` 84G） |
| `usePersonalQueue` | false | 独占型 Key 是否入队（上限 1000，超限 814） |
| `retainSeconds` | 无 | 实例保留时长（10~180s，仅企业共享 Key） |
| `accessPassword` | 无 | 工作流开启加密访问时的访问密码 |

**`workflow` 参数是第二个高价值发现。** 它意味着调用方可以：

1. `getJsonApiFormat` 拉取完整 API-format；
2. 在本地**任意改写整张图**（包括改连线、增删节点，不再局限于替换字面量）；
3. 把改写后的 JSON 作为 `workflow` 提交，覆盖 `workflowId`。

若该参数对普通 Key 可用，架构上会多出一条**比参数注入更强的路径**（见 §4 架构候选 X）。
但文档未说明权限限制与校验严格程度，**必须真机验证**。

**近似 dry-run 的构造方式**（无需平台提供）：
因为 803 在提交阶段硬失败且不产生 taskId，可用"故意注入不存在的 nodeId"探测
平台对 nodeInfoList 的校验时机与强度 —— 零成本、无副作用。

---

## 三、附带确认的其他平台事实

| 项 | 结论 | 用途 |
|---|---|---|
| **810 `WORKFLOW_NOT_SAVED_OR_NOT_RUNNING`** | "请在平台**保存**并**手动运行**一次该工作流" | 工作流必须先手动跑通一次才能 API 调用 —— 应写入 FAQ 与报错提示 |
| **412 `TOKEN_INVALID`** | 官方：**API 路径拼写错误**；第三方常误读为"Key 无效" | 我方现有文案写的是"API Key 无效"，**与官方不符，需修正**（低优先级） |
| **802 `APIKEY_UNAUTHORIZED`** | Key 未授权/已失效 | 真正的 Key 问题应指向 802，而非 412 |
| **433 `VALIDATE_PROMPT_FAILED`** | 工作流校验未通过（节点参数或连接逻辑错误） | 事实 4 若走 (c) 分支，很可能落在此码 |
| **v2 端点** | `POST /openapi/v2/run/workflow/{workflow_id}`（Bearer 鉴权）、`POST /openapi/v2/query`、`POST /openapi/v2/media/upload/binary`；返回 `taskId` + `status` + `clientId` | 长期演进方向；v1 `/task/openapi/*` 仍是主写路径 |
| **API 会强制重置 seed** | "API calls will force a reset of the seed value. To keep the seed parameter constant, include the seed value within the nodeInfoList." | 印证我方"种子必须注入"的设计正确 |
| **COS 链接 24h 失效** | 第三方实践记录 | 结果需及时下载；我方可考虑提示 |

---

## 四、对架构的修订建议（对应 `injection-architecture.md`）

| 原方案条目 | 修订 |
|---|---|
| **R2 平台契约** | 原为弱项。现确认平台提供 `_meta.title`（用户自定义节点标题）。R2 应定义为：**基于平台返回的 `_meta.title` + `class_type` 做语义匹配**，而非基于 class_type 硬规则。这属于"读平台数据"，不构成硬编码 |
| **S2 Snapshot 模型** | 必须保留 `_meta`（现被 `normalize` 丢弃）；每条字段标注 `value_kind`（literal / link），连线边抽取为 `[srcId, slot]` |
| **S3 漂移检测** | 现成信号：803。绑定失效 → 报"绑定漂移"，而非静默 |
| **S5 结构求解** | 在事实 4 验证前，**不变量 A 继续强制**（只向字面量字段注入）。若验证结果为 (a)，可放宽；若为 (b)，则永久强制 |
| **S7 验证层** | **新增平台原生通道**：① 提交时解析 `promptTips.node_errors` / `outputs_to_execute`；② `addMetadata=true` 后读输出图片元数据回读提示词。二者均零额外成本 |
| **新增：架构候选 X** | 评估 `workflow` 参数（整图改写）。若可用，可绕开"连线字段不可注入"的限制，但会引入"本地改写后与云端版本漂移"的新风险。**建议在 S3 之前做一次可行性验证**（成本：1 次任务） |

---

## 五、残余未知与验证方案

仅 **2 项** 需真机验证（事实 4、`workflow` 参数可用性），均按成本分层，
脚本见 `tools/probe_platform.py`。

| 层 | 验证内容 | 预期成本 | 判据 |
|---|---|---|---|
| **L0** | 拉 `getJsonApiFormat`，统计节点数、`_meta.title` 覆盖率、字面量/连线字段比例 | **0**（不创建任务） | 输出结构快照，供 S0/S2 建 fixture |
| **L1** | 提交含**必然不存在**的 nodeId（如 `__probe__`） | **0**（应无 taskId 返回） | 期望 `code == 803` 且**无 taskId** → 证实提交阶段硬失败、不扣费 |
| **L2** | 提交**真实存在但被连线接管**的字段（从 L0 结果里自动挑一个 `[]` 字段） | **0 或 1 次任务** | 观察：803/433/1101 → 硬失败；返回 taskId → 需 L3 确认是否真生效 |
| **L3** | 用 L0 挑出的一个**字面量**文本字段注入特征串，`addMetadata=true`，最小规模跑一次 | **1 次任务** | 下载输出图片读元数据，确认特征串在内 → 证实注入生效通道可用 |
| **L4** | `workflow` 参数：提交 L0 拉到的原样 JSON 字符串，覆盖 workflowId | **1 次任务** | 能正常出图 → 候选 X 可行 |

> **L1/L2 的"零成本"判据**：只要响应中**没有 `taskId`**，即未创建任务，不产生计费。
> 脚本会显式标注每次调用是否返回了 taskId。若平台行为不同（返回了 taskId 才报错），
> 脚本会提示"该步已产生计费"，此时应停止后续探测。

---

## 六、S1 验收对照

| S1 验收标准 | 达成情况 |
|---|---|
| ① `getJsonApiFormat` 是否返回全量节点与 inputs | ✅ 已核实（A） |
| ② 是否存在平台参数 schema 接口 | ✅ 已核实：无（A），但存在 `_meta.title` 语义线索 |
| ③ 注入不存在 nodeId/fieldName 的错误码与行为 | ✅ 已核实：803 硬失败（A） |
| ④ 注入被连线接管字段是否报错 | ⚠️ **文档无法判定（C）**，已给出 L2 验证方案与判据 |
| ⑤ create 响应能否回显已应用注入 | ✅ 已核实：`promptTips.node_errors` / `outputs_to_execute` + `addMetadata`（A） |
| ⑥ 有无低成本 dry-run | ✅ 已核实：无显式 dry-run（A）；但 `workflow` 参数（A，可用性待 L4）+ 803 探测可构造近似 dry-run |

**S1 文档取证部分完成**，剩余 L1–L4 为可选真机复核（需 API Key 与额度），
阻塞关系：**L2 的结论决定 S5 是否可放宽不变量 A**，建议优先执行 L0 + L1 + L2。

---

## 附：资料来源

| 来源 | URL | 用途 |
|---|---|---|
| 官方《关于 nodeInfoList》 | <https://www.runninghub.cn/runninghub-api-doc-cn/doc-8287336> | 不变量 A、`[]` 为连线、纯前端字段 |
| 官方《About nodeInfoList》(EN) | <https://www.runninghub.cn/runninghub-api-doc-en/doc-8287464> | 同上，英文原文 |
| 官方《接口错误码说明》 | <https://qpsnl2kplc.apifox.cn/doc-8417467> | 803 / 433 / 810 / 1101 等 |
| 官方《发起ComfyUI任务2-高级》 | <https://qpsnl2kplc.apifox.cn/api-435315561> | `promptTips`、`workflow`、`addMetadata`、可选参数 |
| 官方《获取工作流Json》 | <https://www.runninghub.cn/runninghub-api-doc-cn/api-425749014> | `data.prompt` 结构、`_meta.title` |
| 官方《Full Workflow Integration Example》 | <https://www.runninghub.cn/runninghub-api-doc-en/doc-8287472> | 官方自家做法 = 人工挑选（对标依据） |
| 官方 v2 端点示例 | <https://www.runninghub.cn/call-api/api-detail/2061745643684720641?apiType=5> | v2 参数与鉴权方式 |
| 社区 n8n 节点 | <https://github.com/zolagz/n8n-nodes-runninghub-client> | `_meta.title` 语义分类实践 |
