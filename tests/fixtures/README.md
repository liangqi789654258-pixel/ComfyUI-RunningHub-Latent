# S0 · 提示词注入失效的可复现 fixture

> 子任务：S0（把根因固化为离线可执行的复现 + 基线快照 + 回归测试）
> 适用插件版本：v1.3.1+ ｜ 关联：[`docs/injection-architecture.md`](../../docs/injection-architecture.md)、[`docs/platform-contract.md`](../../docs/platform-contract.md)

**这一层不联网、不调用 RunningHub API、不消耗任何额度。**
它做的事只有一件：把「提示词注入在一大类工作流形态上静默失效」这件事，
变成任何人都能在本地一键复现、一键比对的**标尺**。后续 S3–S9 的全部重构
都以这里的行为基线为准——改好了要看得见，改坏了要当场红。

---

## 一、目录

```
tests/fixtures/
├── README.md                 # 本文
├── workflows/<case_id>.json  # 形态 fixture：模拟平台 getJsonApiFormat 的真实响应
└── baselines/<case_id>.json  # 当前实现在该 fixture 上的基线快照（进 git）

tools/capture_baseline.py     # 产出 / 比对基线
tests/test_s0_fixtures.py     # 回放 + 逐字段比对的回归测试
```

---

## 二、fixture 文件格式（`workflows/<case_id>.json`）

```jsonc
{
  "schema_version": 1,                 // 固定为 1；改结构时必须升版本
  "case_id": "c2-passthrough-no-text", // 全局唯一，必须与文件名一致
  "form": "passthrough",               // 形态分类（见第五节清单）
  "description": "…",                  // 说明它代表**哪一类**工作流形态（不是某一条工作流）
  "tags": ["passthrough", "no-placeholder"],
  "api_response": {                    // 模拟 getJsonApiFormat 的完整响应
    "code": 0,
    "data": { "prompt": "{…JSON 字符串…}" }
  },
  "truth": {                           // 人工判断的落点（不参与自动比对，供人核对与后续验收）
    "positive": [{"node": "6", "field": "text"}],
    "negative": [{"node": "7", "field": "text"}],
    "seed":     [{"node": "10", "field": "noise_seed"}],
    "note": "真正可写、且在实际执行路径上的落点"
  },
  "expect_current": {                  // 当前实现在这个形态上的**应有**表现
    "status": "regression_open",       // 四档之一，见 2.2
    "violation": "不变量 B",            // 仅 injected_suspect 必填：点名违反的那条规则
    "why": "机制层面的原因（不允许只写现象）"
  }
}
```

### 2.1 必须模拟平台真实形态

| 约定 | 要求 | 依据 |
|---|---|---|
| `api_response.data.prompt` | **JSON 字符串**（不是对象） | 平台 `getJsonApiFormat` 的真实返回形态（`platform-contract.md` 事实 1） |
| 每个节点的 `_meta` | 必须带 `{"title": "…"}` | 平台保留用户自定义节点标题，S2 的 Snapshot 会消费它 |
| 连线 | 一律 `["<源nodeId>", <出槽序号>]` 二元素数组 | ComfyUI API-format 标准形态；`[]` 即「被连线接管」 |
| 未执行路径上的节点 | 节点直属 `"mode": 4`（bypass）或 `"mode": 2`（mute） | ComfyUI 执行态语义，用于守护不变量 B |
| 纯前端字段 | 可保留（如 `control_after_generate`），但**不得**作为落点 | 平台：API-format 里找不到的 fieldName 才是前端字段 |

> 不要构造「只在本地 `normalize()` 下才成立」的畸形结构。fixture 是平台数据的替身，
> 不是喂给某个函数的参数。

### 2.2 `expect_current.status` 的四个取值

| 取值 | 含义 | 回归测试如何判定 |
|---|---|---|
| `already_ok` | 当前实现确实产生了该端口的注入，**且落点成立** | 注入项里必须出现正/负哨兵值 |
| `injected_suspect` | **产生了注入，但注入在本项目的某个不变量或禁止项下不成立**，需要在后续阶段（S5/S6）修正后才能算真正通过 | 有注入 **且** `violation` 点名一条已知规则 **且** 该规则在 `why` 里被写出来 |
| `regression_open` | 当前实现 0 命中（失效），且不得抛异常 | 命中数 0、注入为空、回放不崩 |
| `ambiguous_reject` | 结构本身无法唯一判定，正确行为是拒绝猜测 | 命中 0、注入为空，且诊断说明「多个候选」 |

> **`injected_suspect` 不是「已经没问题」。** 它表示「有注入，但这个注入在某个不变量／禁止项下
> 不成立」，是**待修正**状态，S10 消费这批 fixture 时不得当成绿灯跳过。
> 只有 c1 / c6a 这类「注入本身成立」的形态才是 `already_ok`。

#### 2.2.1 `violation` 字段（仅 `injected_suspect` 必填）

取值必须落在回归测试 `KNOWN_VIOLATIONS` 清单内，且必须是 `why` 的子串
（`why` 要把它写成人话）。当前清单：

| `violation` | 指代的规则 | 当前命中此条的 case |
|---|---|---|
| `不变量 B` | 可达性：落点必须在实际会被执行的路径上，不得向 bypass/mute 节点注入 | `c4-branch-bypass` |
| `§3.5 唯一即默认采用` | 架构文档 §3.5 禁止项：不得把「上游只有一个字符串字段」当成唯一并直接采用 | `c5-derived-value` |
| `部分覆盖` | 多源汇聚只替换了带占位符的一路，未覆盖的分支没有上报 | `c3-multi-source-merge` |
| `同址重复注入` | 同一 `(nodeId, fieldName)` 在 `nodeInfoList` 里出现多条，且负向存在静默丢弃 | `c7-multi-sampler-shared-encode` |

新增形态时同步扩充清单与本节表格——**不允许**用「有注入所以标 suspect」以外的理由绕过，
也不允许 `violation` 写成清单外的自造词（那样测试会红）。

> `violation` 是 v1 内的**增量可选字段**：只有 `injected_suspect` 必须带，其余三档不带，
> 老 fixture 无需回填，因此不升 `schema_version`。

`why` 必须写**机制层面**的原因（例如「兜底路径要求连线源自带 text 字段，透传节点没有 → 在该判断处放弃」），
只写现象（「注入没生效」）视为不合格。

### 2.3 两条硬红线

1. **禁止针对单个真实工作流硬编码。** fixture 里的 nodeId 只是为了构造拓扑，
   `description` 必须说明它代表哪一类形态；绝不允许出现
   「某某官方模板的 node XXX」这类表述，真实工作流的 id/名字不得进入断言逻辑。
2. **提示词文本保持中性。** 用 `a photo of a cat`、`landscape, mountains` 之类，
   不要放真实作者的 prompt（避免版权与泄漏风险）。

---

## 三、基线快照（`baselines/<case_id>.json`）

由 `tools/capture_baseline.py` 产出：把仓库根目录注入 `sys.path`，用当前实现
（`core.workflow`）在每个 fixture 上跑一遍，喂固定哨兵值
`positive="<<POS_SENTINEL>>"` / `negative="<<NEG_SENTINEL>>"`，记录：

```jsonc
{
  "case_id": "…", "schema_version": 1,
  "node_count": 9,                     // 归一化后的节点数
  "string_field_count": 10,            // 字面量字符串字段数
  "link_field_count": 14,              // 连线（[]）字段数
  "placeholder_hits": {"positive": 0, "negative": 0},   // 云端写了几个占位符
  "hit_stats": {"positive": 0, "negative": 0},          // 实际命中（含兜底）
  "injections": [                      // 实际会提交上去的注入项
    {"nodeId": "6", "fieldName": "text", "fieldValue": "<<POS_SENTINEL>>"}
  ],
  "seed_targets": [["10", "noise_seed"]],
  "diagnosis": {"positive": "…中文说明…", "negative": "…"},
  "expect_current": { "…与 fixture 同…" }
}
```

**确定性约定**（基线必须能进 git、能 diff）：

- 字典键排序写入；注入项按 `core.workflow._node_sort_key` → 字段名 → 值排序；
- 不写入时间戳、随机值、绝对路径或任何平台相关信息；
- `injections[].fieldValue` 记录**实际提交值**——本项目的红线是
  「必须让用户看到真正送上去的是什么」，基线同样不能含糊。

### 命令

```bash
# 用当前实现重写基线（改动实现并确认是有意为之后才用）
C:/AI/dongshan/ComfyUI/.venv/Scripts/python.exe tools/capture_baseline.py --update

# CI 模式：与基线逐字段比对，不一致则打印 diff 并 exit 1（不带参数时默认即此模式）
C:/AI/dongshan/ComfyUI/.venv/Scripts/python.exe tools/capture_baseline.py --check
```

---

## 四、如何新增一个 case

1. **先想清楚形态，再想数据。** 问自己：这类工作流是**把什么假设打破了**？
   （相邻？命名？唯一？存在即生效？）`description` 就写这个答案。
2. 复制一个最接近的 `workflows/*.json`，改 `case_id` / `form` / `description` / `tags`，
   文件名必须与 `case_id` 一致。
3. 构造 `api_response`：
   - 节点数不要贪多，能表达形态即可；
   - 每个节点都写 `_meta.title`；
   - 需要「未执行分支」时用节点直属 `"mode": 4`；
   - 不想让主路径命中，就不要在任何字符串里写 `{positive}` / `{negative}`。
4. 填 `truth`：人工判断的落点（nodeId 必须真实存在于 `api_response`，否则测试会拦下），
   `note` 写清判断依据。
5. 填 `expect_current`：先跑一次 `--update` 看实际表现，再回头写 `why`（机制层原因）。
   **不要为了让测试通过而改 status**——status 描述的是真实表现。
   若实际表现是「有注入、但注入在某个不变量／禁止项下不成立」，标 `injected_suspect`
   并填 `violation`（见 2.2.1），不要图省事标成 `already_ok`。
6. 生成基线并跑测试：

```bash
C:/AI/dongshan/ComfyUI/.venv/Scripts/python.exe tools/capture_baseline.py --update
C:/AI/dongshan/ComfyUI/.venv/Scripts/python.exe tests/test_s0_fixtures.py
```

7. 把新的 fixture 与 baseline **一起提交**（缺 baseline 时测试会直接失败）。

> 回归测试 `tests/test_s0_fixtures.py` 会自动把新 fixture 纳入：
> 完整性校验 → 结构解析 → 回放比对 → 状态一致性 → 不变量 A → 禁止静默 → 确定性，
> 每个 case 至少 4 条断言（现有 case 均在 60 条以上）。

---

## 五、当前覆盖的形态

| case_id | form | 形态要点 | expect_current |
|---|---|---|---|
| `c1-direct-encode-placeholder` | `direct-encode` | 编码节点直连采样器 + 写了占位符（**正向对照**） | `already_ok` |
| `c2-passthrough-no-text` | `passthrough` | 条件链中间夹透传节点（自身无 text 字段） | `regression_open` |
| `c3-multi-source-merge` | `multi-source-merge` | 两路条件经合并节点汇聚后进采样器 | `injected_suspect`（`部分覆盖`：只覆盖有占位符的那一路） |
| `c4-branch-bypass` | `branch-bypass` | 标量参数全由分支节点驱动（`[]`）+ bypass 节点 | `injected_suspect`（`不变量 B`：命中数虚高，含不可达落点） |
| `c5-derived-value` | `derived-value` | 提示词与标量都来自加工/表达式节点（派生值） | `injected_suspect`（`§3.5 唯一即默认采用`：落到派生节点的前缀操作数） |
| `c6a-no-placeholder-direct` | `no-placeholder` | 无占位符但拓扑直白 | `already_ok` |
| `c6b-no-placeholder-passthrough` | `no-placeholder-passthrough` | 无占位符 + 透传 + 二级采样（**最坏情况**） | `regression_open` |
| `c7-multi-sampler-shared-encode` | `multi-sampler` | 两个采样器串联、共享编码节点 | `injected_suspect`（`同址重复注入` + 负向被静默丢弃） |
| `c8-multi-hop-ambiguous` | `multi-hop-ambiguous` | 上游文本处理节点多候选且无占位符 | `ambiguous_reject` |

`already_ok` 只表示「确实产生了注入」，**不代表落点正确**——c3 / c4 / c5 / c7
都是「有注入但语义可疑」的形态，已统一改标 `injected_suspect`，看每条 fixture 的
`expect_current.violation` / `why` 与 `truth.note`。
