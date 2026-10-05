# S2 · 发现层（Discovery）+ Snapshot 模型

> 子任务：S2（发现层 + Snapshot 模型）
> 状态：**已实现**（代码 `core/snapshot.py`，自测 `tests/test_snapshot.py` 101 项全绿）
> 日期：2026-10-05 ｜ 适用插件版本：v1.3.1+
> 关联：[`injection-architecture.md`](injection-architecture.md) · [`platform-contract.md`](platform-contract.md)

**本文不含任何针对单个工作流的硬编码结论。** 发现层只做「记录事实 + 判定字段形态」，
语义推断一律留给 S5 解析层。

---

## 一、为什么要有这一层

`getJsonApiFormat` 返回的是一坨原始 API-format JSON，直接拿来用有三个硬伤：

| # | 硬伤 | 后果 |
|---|---|---|
| 1 | 分不清**字面量**和**连线** | 不变量 A（只向字面量字段注入）只能靠调用方零散判断，必漏 |
| 2 | 不知道节点**会不会被执行** | 不变量 B（落点必须在执行路径上）无从判断 |
| 3 | **没有指纹、不能 diff** | 云端工作流被编辑后绑定静默失效（绑定漂移），无从检测 |

Snapshot 一次解决三点：每个字段标注 `value_kind`、每个节点标注执行态、整张图抽成
连线边列表，并给出两个指纹用于 diff 与漂移检测。

附带修掉一个现存缺陷：`core/workflow.py:normalize()` 原先只保留 `class_type` + `inputs`，
把平台返回的 `_meta`（含用户自定义节点标题 `_meta.title`）整个丢掉——
平台白送的语义线索被我们自己扔了。现在 `_meta` 与节点直属执行态字段统一收进 `meta`。

---

## 二、数据模型

```
WorkflowSnapshot
├── workflow_id: str
├── nodes: {nodeId: NodeSnapshot}
│              ├── class_type: str
│              ├── title: str            # _meta.title（用户在画布上起的标题）
│              ├── exec_state: str       # active / bypassed / muted
│              ├── exec_source: str      # mode / flag / default（判定依据）
│              ├── exec_evidence: dict   # 原始证据，例如 {"mode": 4}
│              ├── meta: dict            # 平台 _meta + 执行态字段
│              └── fields: {字段名: FieldSnapshot}
│                             ├── value_kind: literal / link / unknown
│                             ├── value: Any          # 仅字面量/未知保留原值
│                             └── link_src / link_slot
├── edges: [Edge(src, src_slot, dst, dst_field)]      # 连线边列表
└── warnings: [str]                                   # 悬空连线、空结构等诊断
```

### 2.1 字段形态 `value_kind`

判定依据只有一条平台规则（[`platform-contract.md`](platform-contract.md) 事实 1/4）：
**API-format 里 `[]` 包裹的值表示连线**。

| 形态 | `value_kind` | 可注入 |
|---|---|---|
| 标量（str / int / float / bool / None） | `literal` | ✅ |
| `[源节点, 输出槽]`（长度 1–2、首元素为 str/int） | `link` | ❌（不变量 A） |
| 其余列表、空列表、dict 等复合结构 | `unknown` | ❌（宁可不敢注入，也不猜） |

### 2.2 节点执行态 `exec_state`

平台没有统一暴露执行态，因此按**证据强度**依次判定：

| 顺序 | 依据 | 取值 | 判定结果 |
|---|---|---|---|
| 1 | `mode`（ComfyUI litegraph 常量） | `0` / `1` / `3` | `active` |
| | | `2` | `muted` |
| | | `4` | `bypassed` |
| | | 其他取值 | **不猜**，回落默认并保留证据 |
| 2 | mute 类布尔字段 | `mute` / `muted` / `is_muted` / `isMuted` / `disabled` / `is_disabled` | `muted` |
| 3 | bypass 类布尔字段 | `bypass` / `bypassed` / `is_bypassed` / `isBypassed` | `bypassed` |
| 4 | 以上都没有 | — | `active`，`exec_source = "default"` |

> `default` 不是平台明示，而是「API-format 通常已剔除被静音节点」这一假设。
> 记录 `exec_source` 就是为了让下游知道这条结论的证据等级——不能用假设冒充事实。
> 这些字段均属 ComfyUI / 平台通用字段，不是某个工作流的私有约定。

### 2.3 指纹

| 指纹 | 包含 | 用途 |
|---|---|---|
| `fingerprint()` | 节点/字段/形态/连线/执行态 **+ 字面量取值** | 判断「两份快照是否完全一致」 |
| `topology_fingerprint()` | 同上，**不含取值** | 绑定漂移检测：作者只改默认值不算漂移，改结构才算 |

两者均为 sha256，对键序不敏感（内部按稳定序 + `sort_keys` 序列化），
因此**同一工作流两次拉取必然得到同一指纹**。

---

## 三、用法

```python
from core.snapshot import build_snapshot, diff_snapshots, summarize_diff

snap = build_snapshot(client._fetch_workflow_raw(workflow_id), workflow_id=workflow_id)

snap.node("6").title                 # '正面提示词'（_meta.title）
snap.slot("3", "seed").is_literal    # True  -> 可注入
snap.slot("3", "positive").is_link   # True  -> 被连线接管，不注入
snap.injectable_slots()              # 满足不变量 A + B 的全部槽位 [(nodeId, field)]
snap.edges                           # 连线边列表
snap.to_json()                       # 落盘 / 存档

entries = diff_snapshots(old_snap, new_snap)
if has_breaking_change(entries):
    print(summarize_diff(entries))   # 中文差异清单 + 「会导致绑定失效」标记
```

客户端侧：`RunningHubClient.get_workflow_snapshot(workflow_id)` 已完成接入，
复用 `_fetch_workflow_raw` 并带独立进程内缓存（TTL 沿用 `structure_ttl`），
与原有 `get_workflow_structure()` 互不干扰。

---

## 四、与后续子任务的接口约定

| 子任务 | 依赖本层的哪一部分 |
|---|---|
| **S3 绑定 / 漂移检测** | `topology_fingerprint()` 做漂移比对；`diff_snapshots()` 的 `breaking` 标记即「绑定失效」信号；`injectable_slots()` 用于固化绑定时校验槽位合法 |
| **S4 占位符泛化（R3）** | 扫 `literal` 字段的文本值即可，不必再自己判断连线 |
| **S5 结构求解（R4）** | 直接消费 `edges` 做图遍历；`exec_state` 用于剪掉不执行分支；`_meta.title` 可作为 R2 语义匹配素材（读平台数据，不构成硬编码） |
| **S6 校验器** | 存在性 = `slot()` 非 None；字面量性 = `value_kind == literal`；可达性 = `node.is_executing`。三条统一在 S6 拦截 |
| **S7 验证层 / 回执** | 快照可存档为「提交时刻的结构证据」，与 `promptTips` 对照 |

---

## 五、本层明确不做的事

* **不推断语义**：不出现任何 `class_type` 名称判断、任何具体节点 ID、任何「相邻即可」的
  拓扑假设（`tests/test_snapshot.py` 第 10 节对此做了源码级自查，违反即测试失败）。
* **不做注入校验**：只提供事实查询，`injectable_slots()` 是查询而非裁决，
  拦截与报错文案由 S6 统一负责。
* **不联网**：`build_snapshot()` 是纯函数，输入任意结构数据即可离线测试。

---

## 六、S2 验收对照

| S2 验收标准 | 达成情况 |
|---|---|
| 节点清单 | ✅ `WorkflowSnapshot.nodes`（含 `class_type` / `title` / `meta`） |
| 每字段 `value_kind` | ✅ `literal` / `link` / `unknown`，见 §2.1 |
| 节点执行态 | ✅ `active` / `bypassed` / `muted` + 判定依据与证据，见 §2.2 |
| `[]` 连线边列表 | ✅ `WorkflowSnapshot.edges`，悬空连线另有 warning |
| 同一工作流两次快照一致 | ✅ 指纹对键序不敏感，两次构建指纹相同（自测第 6 节） |
| 结构变化可被 diff 检出 | ✅ `diff_snapshots()` 覆盖节点增删/类型/标题/执行态/字段增删/形态/连线/取值，并标出会导致绑定失效的变化（自测第 7 节） |

自测：`python tests/test_snapshot.py`（101 项，不需要 torch、API Key 与网络）。

其中第 11 节是**交叉一致性验证**：同一份原始数据分别走既有 `normalize()` 与新快照，
逐节点比对 class_type / 字段集合 / 字段取值 / meta，证明两条路径语义对等——这是将来
S3/S5 把注入逻辑切到快照上的唯一依据。覆盖 `examples/*.json`（ComfyUI 前端格式，
参数在 `widgets_values` 里，字段数为 0）与 S0 的 `tests/fixtures/workflows/*.json`
（平台真实 API-format，字段数 23–48，含 `mode: 4` 旁路节点与多源汇聚等形态），
两者全部一致。
