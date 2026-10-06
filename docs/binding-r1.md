# R1 显式绑定与漂移检测（`core/binding.py`）

> 阶段：S3 ｜ 状态：已实现 ｜ 自测：`tests/test_binding.py`（131 项）
> 上游依赖：S2 `core/snapshot.py`、S1 `docs/platform-contract.md`
> 下游：S5 结构求解会用这里的 `Binding` 数据结构作为输出格式

---

## 一、为什么需要这一层

S2 的 Snapshot 回答了「这个世界里都有什么」：哪些字段是字面量、哪些节点会被执行、整张图的指纹是什么。
但它回答不了「用户的 `positive` 该送到哪个节点」——**这个映射关系只能由人指定**。

历史上我们试图用启发式回答这个问题（沿采样器的 `positive` 连线找上游、要求上游自带 `text` 字段、
找不到就当作「要么没有」），结果在 S0 固化的 9 类形态里：**2 类完全静默失效、1 类拒绝猜测、
4 类虽有注入但打错了地方**（详见 `tests/fixtures/README.md` 的四档状态）。

根因很直白：**邻接、命名、存在，都是某一种工作流的形状约定，不是平台不变量。**
任何靠形状猜的实现，都会在透传节点、分支旁路、多源汇聚、多采样器这些第三方工作流的
常态形态上集体失效。

R1 显式绑定不解决这个问题——它**绕开**这个问题：既然猜不准，那就让用户明说。
代价是要人工配置一次，收益是**100% 确定、可审计、可复核**。

---

## 二、核心概念

```
SemanticPort（positive / seed / …）  用户自取的语义键
        ↓  Binding（显式指定，不推断）
StructuralSlot（nodeId + fieldName） 平台唯一理解的地址
        ↓  build_injections()
nodeInfoList 项                      提交给 RunningHub 的形态
```

| 概念 | 类型 | 说明 |
|---|---|---|
| `Binding` | dataclass | 一条绑定：`node_id` + `field_name` + `port`（+ 备注 `note`、建立时的 `topology` 指纹） |
| `BindingSet` | dataclass | 某工作流下的全量绑定，可落盘为 lockfile 给团队共享 |
| `BindingProblem` | dataclass | 一处校验问题，带 `level`（`error` 阻断 / `warn` 提示）与机器可读的 `code` |
| `DriftReport` | dataclass | 漂移检测报告：哪些绑定失效了、为什么 |

**SemanticPort 只是字符串键。** 模块不解释端口名的含义，源码里不存在任何以端口名为条件的
分支判断。`PORT_POSITIVE` 等常量仅是文档级约定，写成 `"ografia"` 也一样工作。

---

## 三、用法

### 3.1 挑一个槽位绑上去

```python
from core.snapshot import build_snapshot
from core.binding import Binding, BindingSet, lock_bindings, build_injections

snapshot = build_snapshot(api_response)          # getJsonApiFormat 的原始响应

# 1) 先把工作流里所有可注入槽位列出来，人工挑
print(snapshot.injectable_slots())
# [('6', 'text'), ('7', 'text'), ('10', 'noise_seed'), ...]

# 2) 建立显式绑定
bindings = [
    Binding(node_id="6",  field_name="text",       port="positive"),
    Binding(node_id="7",  field_name="text",       port="negative"),
    Binding(node_id="10", field_name="noise_seed", port="seed"),
]

# 3) 固化成 lockfile（会记录当时的拓扑指纹作为漂移基准）
bs = lock_bindings(snapshot, bindings, workflow_id="12345")

# 4) 构造提交项
injections, problems = build_injections(
    snapshot, bs, {"positive": "a cat", "negative": "blurry", "seed": 42}
)
# injections = [{"nodeId": "6", "fieldName": "text", "fieldValue": "a cat"}, ...]
```

一个端口允许绑定**多个槽位**（多源汇聚时需要全部覆盖），它们会收到同一个值。

### 3.2 查看可注入槽位清单

```python
from core.binding import format_slot_catalog, export_injectable_slots

print(format_slot_catalog(snapshot))
# 可注入槽位 18 个（已排除连线字段与不执行节点）：
#   ● nodeId=6  fieldName=text   [CLIPTextEncode 「正面提示词」]   当前值 = a nice view
#   ● nodeId=10 fieldName=noise_seed   [KSamplerAdvanced]   当前值 = 12345

rows = export_injectable_slots(snapshot)   # 结构化版本，含 title / classType / execState
```

清单里的每一项都已满足不变量 A + B，可以直接填进 lockfile，不必再自行判断。

### 3.3 drift 检测（建议在每次运行时做）

```python
from core.binding import detect_drift, check_fingerprint

# 快检：只比拓扑指纹，开销极小，可每次都跑
for p in check_fingerprint(current_snapshot, bs):
    print(p.describe())

# 精查：需要上一份快照做 diff
report = detect_drift(old_snapshot, current_snapshot, bs)
if report.has_drift:
    print(report.describe())
    print("失效端口：", report.broken_ports())
```

---

## 四、校验规则

`validate_binding()` 按「越靠前越致命」的顺序判定，命中即返回：

| 顺序 | `code` | 触发条件 | 级别 | 依据 |
|---|---|---|---|---|
| 1 | `NODE_MISSING` | 快照里没有这个 nodeId | error | 平台会返回 **803 硬失败**，先知电源先知道 |
| 2 | `NODE_NOT_EXECUTING` | 节点是 bypass / muted | error | **不变量 B**：打了也不会执行 |
| 3 | `FIELD_MISSING` | 节点没有这个字段名 | error | 平台会返回 **803 硬失败** |
| 4 | `FIELD_LINKED` | 字段被 `[]` 连线接管 | error | **不变量 A**：平台明确不建议修改 |
| 5 | `FIELD_UNKNOWN_KIND` | 值的形态既不是字面量也不是连线 | error | C 级未知项一律按最严格处理 |
| 6 | `EXEC_EVIDENCE_ASSUMED` | 结构里没有 `mode`/`bypass`/`mute` 证据 | **warn** | 「会执行」只是默认假设，不是平台明示 |

第 6 条值得说明：多数工作流不会显式导出执行态字段，一律判 error 会让模块不可用；
但不吭声又会把假设伪装成事实。取法是**允许注入、但把依据摆给用户**。
S6 若决定据此要求用户显式确认，改这一处即可。

跨绑定冲突：

| `code` | 触发条件 |
|---|---|
| `DUPLICATE_SLOT` | 两个端口绑到同一个 `(nodeId, fieldName)` |
| `NO_BINDING_FOR_PORT` | 用户给了某个端口的值，但它没有任何绑定 |

`NO_BINDING_FOR_PORT` 是本轮最要紧的一条——历史上的静默失效正是「该注的值没注上去、
却一路绿灯跑完」。现在它必然报错。

---

## 五、漂移检测规则

判定粒度是**逐绑定**的，不看全图 `has_breaking_change`。理由：作者在别处删一个节点，
并不影响你的绑定；改了某个默认值更不该逼你重新配置。

| 变化 | 是否判为漂移 | 理由 |
|---|---|---|
| 删除了被绑定的节点 | ✅ 漂移 | 地址已不存在，提交会 803 |
| 字段从 `literal` 变 `link` | ✅ 漂移 | 违反不变量 A；且这条可能不报错，只是静默失效 |
| 节点从 `active` 变 `bypassed`/`muted` | ✅ 漂移 | 违反不变量 B，注入不会被执行 |
| 删除了被绑定的字段名 | ✅ 漂移 | 地址已不存在 |
| `class_type` 变化 | ✅ 漂移 | 节点语义可能完全不同，保守起见要求复核 |
| **只改某个字段的默认值** | ❌ 不漂移 | 绑定地址没变、字段仍是可注入字面量，变的只是用户没注入时跑什么 |
| **节点顺序重排 / 键序变化** | ❌ 不漂移 | 指纹本身与顺序无关 |
| 新增了无关节点 | ❌ 不漂移 | 老槽位照样成立 |

「只改默认值不算漂移」是有意放宽的：若这也算，用户每改一次云端提示词都要重新配置一遍绑定，
噪音远大于收益。这里复用 S2 `has_breaking_change()` 的口径。

---

## 六、lockfile 格式

```json
{
  "schema_version": 1,
  "workflow_id": "12345",
  "topology": "9f2c…（建立时的拓扑指纹）",
  "fingerprint": "c81b…（建立时的全量指纹，含默认值）",
  "note": "Wan 2.1 文生视频，作者 XYZ",
  "bindings": [
    {"node_id": "6", "field_name": "text", "port": "positive", "note": "", "topology": "9f2c…"}
  ]
}
```

- `schema_version` 不匹配时 `from_dict()` 直接抛 `ValueError`，**不做静默兼容**。
- `topology` 是漂移检测的基准；没有它时 `check_fingerprint()` 返回空，不打扰用户。
- 可直接 `save_lockfile()` / `load_lockfile()` 落盘，建议和 `.gitignore` 之外的配置一起纳管。

---

## 七、本模块明确不做的事

| 不做 | 归属 |
|---|---|
| 沿连线回溯找「真正的文本来源」 | S5（R4 结构求解） |
| 扫描占位符 / token 自动发现端口 | S4（R3 占位符泛化） |
| 根据 `class_type` 判断谁是采样器 | **永远不做**——这是 S0 已证伪的做法 |
| 多源汇聚时的「取第一个」式裁决 | S5（应上报歧义，而非裁决） |
| 触发 agent / 兜底到默认值 | 违反本项目「杜绝静默失效」红线 |

`tests/test_binding.py` 第 8 节会直接扫描 `core/binding.py` 源码，出现任何上述痕迹即判失败。

---

## 八、与 S0 基线的对照

`tests/test_binding.py` 第 9 节逐 case 拿 S0 基线做对照，构成「R1 到底解决了什么」的硬证据：

| 形态 | 现有实现（S0 基线） | R1 显式绑定 |
|---|---|---|
| c2 / c6b 透传无 `text` | 0 命中，静默用云端默认值 | 100% 生效 |
| c8 多跳歧义 | 拒绝猜测，0 命中 | 用户指定即精确注入（含多槽位） |
| c5 派生值 | 打到模板前缀 `60.prefix`（错） | 打到叶子字面量 `61.value` |
| c3 多源汇聚 | 只覆盖带占位符的一路 | 全部 truth 槽位一次覆盖 |
| c4 分支旁路 | 连 bypass 节点一起注入了 | 清单里根本不出现 bypass 节点 |
| c7 多采样器 | 同址重复注入 2 条 | 逐槽位绑定，重复互斥 slot 判错 |

---

## 九、遗留与注意事项

1. **C 级未知项仍然存在**：「向被连线接管的字段注入，平台到底会怎样」尚未真机验证。
   本模块的处置是**一律拒绝**（最严格），不依赖任何猜测。解除这一条需要跑
   `tools/probe_platform.py` 的 L2 层（会产生一次任务计费），属于后续动作。
2. **本模块尚未接入节点层**：`nodes.py` 目前仍走旧的推断路径；接入点是 S6/S8。
3. `check_fingerprint()` 用拓扑指纹，因此「只改默认值」不会打扰用户——这是设计目标，不是缺陷。
