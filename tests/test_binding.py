"""
S3 · R1 显式绑定（Binding）+ 漂移检测（Drift Guard）离线自测

不依赖 API Key、不联网，也不 import torch —— 与 test_snapshot.py 同样注册
`rhplugin.core` 包壳来绕开 `core/__init__.py` 对 torch 的间接依赖：

    python tests/test_binding.py

覆盖：
  * 9 类 S0 fixture 上，显式绑定能否精准命中人工标注的目标槽位
  * 非法绑定拦截：link 字段 / bypass 节点 / 不存在的 nodeId / 不存在的字段名 / 未知形态
  * 漂移检测：节点删除、字段 literal→link、exec 变更要报；只改默认值、重排、新增节点不报
  * 不静默：有值无绑定、同槽位冲突、部分注入禁止
  * lockfile 落盘往返与版本校验
  * 禁止项源码自查：模块内不得出现任何自动定位 / 推断痕迹
"""

import json
import os
import sys
import tempfile
import types
from typing import Any, Dict, List, Tuple

ROOT = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.abspath(os.path.join(ROOT, ".."))

_pkg = types.ModuleType("rhplugin")
_pkg.__path__ = [PLUGIN]
sys.modules["rhplugin"] = _pkg
_core_pkg = types.ModuleType("rhplugin.core")
_core_pkg.__path__ = [os.path.join(PLUGIN, "core")]
sys.modules["rhplugin.core"] = _core_pkg

from rhplugin.core import binding as bind  # noqa: E402
from rhplugin.core import snapshot as snap  # noqa: E402

PASSED: List[str] = []
FAILED: List[str] = []

FIXTURES_DIR = os.path.join(ROOT, "fixtures", "workflows")
BASELINES_DIR = os.path.join(ROOT, "fixtures", "baselines")

# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def check(name: str, cond: bool, extra: str = "") -> None:
    if cond:
        PASSED.append(name)
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name} {extra}")


def load_fixtures() -> List[Tuple[str, Dict[str, Any], snap.WorkflowSnapshot]]:
    """[(case_id, fixture, snapshot)]，按 case_id 稳定排序。"""
    out = []
    for name in sorted(os.listdir(FIXTURES_DIR)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(FIXTURES_DIR, name), encoding="utf-8") as fh:
            fx = json.load(fh)
        out.append((fx["case_id"], fx, snap.build_snapshot(fx["api_response"])))
    return out


def truth_bindings(fx: Dict[str, Any]) -> List[bind.Binding]:
    """把 fixture 里人工标注的 truth 落点转成绑定清单。"""
    out = []
    for role in ("positive", "negative", "seed"):
        for item in fx.get("truth", {}).get(role, []):
            out.append(bind.Binding(
                node_id=str(item["node"]),
                field_name=item["field"],
                port=role,
            ))
    return out


FIELDS = load_fixtures()

SENTINEL_POS = "<<S3_POS>>"
SENTINEL_NEG = "<<S3_NEG>>"
SENTINEL_SEED = 20261005


def sample_values() -> Dict[str, Any]:
    return {"positive": SENTINEL_POS, "negative": SENTINEL_NEG, "seed": SENTINEL_SEED}


# ---------------------------------------------------------------------------
print("\n=== 1. fixture 完整性：人工标注的落点必须真的可注入 ===")
# ---------------------------------------------------------------------------

check("fixture 数量不少于 9", len(FIELDS) >= 9, f"实际 {len(FIELDS)}")

for case_id, fx, s in FIELDS:
    injectable = set(s.injectable_slots())
    bad = [ (str(i["node"]), i["field"])
            for role in ("positive", "negative", "seed")
            for i in fx.get("truth", {}).get(role, [])
            if (str(i["node"]), i["field"]) not in injectable ]
    check(f"{case_id}：truth 落点全部满足不变量 A+B", not bad, f"不可注入 {bad}")


# ---------------------------------------------------------------------------
print("\n=== 2. R1 显式绑定：9 类形态全部精准命中目标槽位 ===")
# ---------------------------------------------------------------------------

for case_id, fx, s in FIELDS:
    bs = bind.BindingSet(workflow_id=case_id, bindings=truth_bindings(fx))
    injections, problems = bind.build_injections(s, bs, sample_values())

    errors, _warns = bind.split_problems(problems)
    check(f"{case_id}：校验无阻断项", not errors, bind.describe_problems(errors))

    expected = {(str(i["node"]), i["field"])
                for role in ("positive", "negative", "seed")
                for i in fx.get("truth", {}).get(role, [])}
    actual = {(str(i["nodeId"]), str(i["fieldName"])) for i in injections}
    check(f"{case_id}：注入槽位集合与 truth 完全一致",
          actual == expected, f"期望 {sorted(expected)} 实际 {sorted(actual)}")
    check(f"{case_id}：注入条数等于绑定条数（无多注无漏注）",
          len(injections) == len(bs.bindings),
          f"注入 {len(injections)} 绑定 {len(bs.bindings)}")

    # 每个端口的值必须准确落到它自己的槽位上，不能串端口
    by_port: Dict[str, List[Dict[str, Any]]] = {"positive": [], "negative": [], "seed": []}
    port_of = {(str(b.node_id), str(b.field_name)): b.port for b in bs.bindings}
    for inj in injections:
        port = port_of.get((str(inj["nodeId"]), str(inj["fieldName"])), "")
        by_port.setdefault(port, []).append(inj)
    check(f"{case_id}：positive 值与其绑定槽位一一对应",
          all(i["fieldValue"] == SENTINEL_POS for i in by_port.get("positive", []))
          and len(by_port.get("positive", [])) > 0)
    check(f"{case_id}：无端口串值（seed 槽位不会出现提示词）",
          all(not isinstance(i["fieldValue"], str) or i["fieldValue"] == SENTINEL_SEED
              for i in by_port.get("seed", [])))

# 一个端口绑定多个槽位：允许，且全部收到同一个值
multi = next((x for x in FIELDS if x[0] == "c3-multi-source-merge"), None)
if multi:
    case_id, fx, s = multi
    pos_items = fx["truth"]["positive"]
    bs = bind.BindingSet(workflow_id=case_id,
                         bindings=[bind.Binding(str(i["node"]), i["field"], "positive")
                                   for i in pos_items])
    injections, problems = bind.build_injections(s, bs, {"positive": SENTINEL_POS})
    errors, _ = bind.split_problems(problems)
    check("c3：单端口多槽位全部收到同一值且不判为冲突",
          not errors and len(injections) == len(pos_items)
          and all(i["fieldValue"] == SENTINEL_POS for i in injections),
          bind.describe_problems(errors))


# ---------------------------------------------------------------------------
print("\n=== 3. 非法绑定拦截 ===")
# ---------------------------------------------------------------------------

case_ids = [c for c, _, _ in FIELDS]
_c4 = next(s for c, _, s in FIELDS if c == "c4-branch-bypass")
_c1 = next(s for c, _, s in FIELDS if c == "c1-direct-encode-placeholder")

# 3.1 bypass 节点（c4 的 nodeId 40/41 为 mode:4）
for nid in ("40", "41"):
    probs = bind.validate_binding(_c4, bind.Binding(nid, "text", "positive"))
    codes = [p.code for p in probs]
    check(f"c4：向 bypass 节点 {nid} 绑定被拒（不变量 B）",
          bind.CODE_NODE_NOT_EXECUTING in codes
          and any(p.is_error for p in probs), f"codes={codes}")

# 3.2 被连线接管的字段（node 6 的 clip）
probs = bind.validate_binding(_c1, bind.Binding("6", "clip", "positive"))
codes = [p.code for p in probs]
check("向 link 字段绑定被拒（不变量 A）",
      bind.CODE_FIELD_LINKED in codes and probs[0].is_error, f"codes={codes}")
check("link 拒绝提示里给出了上游 nodeId 作为替代建议",
      any("改绑到连线上游" in p.message or p.hint for p in probs))

# 3.3 不存在的 nodeId（对应平台 803 硬失败）
probs = bind.validate_binding(_c1, bind.Binding("9999", "text", "positive"))
codes = [p.code for p in probs]
check("不存在的 nodeId 被拒", bind.CODE_NODE_MISSING in codes, f"codes={codes}")
check("nodeId 拒绝时会列出存在的节点",
      any("平台" in p.message or "nodeId=" in p.message for p in probs))

# 3.4 不存在的字段名
probs = bind.validate_binding(_c1, bind.Binding("6", "nope_field", "positive"))
codes = [p.code for p in probs]
check("不存在的字段名被拒", bind.CODE_FIELD_MISSING in codes, f"codes={codes}")
check("字段名拒绝时会列出该节点可用字段",
      any("可用字段" in p.message for p in probs))

# 3.5 形态无法归类的字段（复合结构 → unknown）
_weird = {"prompt": json.dumps({
    "1": {"class_type": "X", "inputs": {"weird": {"a": 1}}},
})}
_weird_snap = snap.build_snapshot(_weird)
probs = bind.validate_binding(_weird_snap, bind.Binding("1", "weird", "positive"))
codes = [p.code for p in probs]
check("unknown 形态字段被拒（按最严格处理）",
      bind.CODE_FIELD_UNKNOWN in codes and probs[0].is_error, f"codes={codes}")

# 3.6 非法绑定必须让整单注入为空
bs_bad = bind.BindingSet(bindings=[
    bind.Binding("6", "text", "positive"),
    bind.Binding("40", "text", "negative"),   # bypass，非法
])
injections, problems = bind.build_injections(_c4, bs_bad, sample_values())
errors, _ = bind.split_problems(problems)
check("存在非法绑定时整单注入为空（不做部分注入）",
      injections == [] and len(errors) >= 1, f"注入 {len(injections)} 错误 {len(errors)}")

# 3.7 执行态只有默认依据时：允许但必须提示
_s = next(s for c, _, s in FIELDS if c == "c1-direct-encode-placeholder")
_node6 = _s.node("6")
probs = bind.validate_binding(_s, bind.Binding("6", "text", "positive"))
has_warn = any(p.code == bind.CODE_EXEC_ASSUMED and not p.is_error for p in probs)
check("exec_source=default 时给出 WARN 而非放行无声",
      (_node6.exec_source == "default" and has_warn) or _node6.exec_source != "default",
      f"exec_source={_node6.exec_source} warns={[p.code for p in probs]}")


# ---------------------------------------------------------------------------
print("\n=== 4. 漂移检测 ===")
# ---------------------------------------------------------------------------


def mutate(case_id: str, fn) -> snap.WorkflowSnapshot:
    """在 fixture 的 prompt 结构上做一次改动，返回新快照。"""
    fx = next(f for c, f, _ in FIELDS if c == case_id)
    data = json.loads(fx["api_response"]["data"]["prompt"])
    fn(data)
    return snap.build_snapshot({"code": 0, "data": {"prompt": json.dumps(data)}})


_fx1 = next(f for c, f, _ in FIELDS if c == "c1-direct-encode-placeholder")
_s_old = snap.build_snapshot(_fx1["api_response"])
_bs1 = bind.lock_bindings(_s_old, truth_bindings(_fx1), workflow_id="c1")

# 4.1 删除被绑定的节点 → 漂移
def _del(data):
    del data["6"]
new = mutate("c1-direct-encode-placeholder", _del)
rep = bind.detect_drift(_s_old, new, _bs1)
check("删除已绑定节点 → 标记该绑定失效",
      rep.has_drift and any(d.binding.node_id == "6" for d in rep.broken),
      f"broken={rep.broken_ports()}")
check("漂移报告含可读的中文描述", "失效" in rep.describe())

# 4.2 字段 literal → link → 漂移
def _to_link(data):
    data["6"]["inputs"]["text"] = ["7", 0]
new = mutate("c1-direct-encode-placeholder", _to_link)
rep = bind.detect_drift(_s_old, new, _bs1)
check("字段 literal→link → 标记该绑定失效",
      rep.has_drift and any(d.binding.field_name == "text" for d in rep.broken),
      f"broken={[(d.binding.node_id, d.binding.field_name) for d in rep.broken]}")

# 4.3 节点 active → bypassed → 漂移
def _to_bypass(data):
    data["6"]["mode"] = 4
new = mutate("c1-direct-encode-placeholder", _to_bypass)
rep = bind.detect_drift(_s_old, new, _bs1)
check("已绑定节点变成 bypassed → 标记失效（不变量 B 事后校验）",
      rep.has_drift and any(d.binding.node_id == "6" for d in rep.broken))

# 4.4 只改默认值 → 不漂移
def _val(data):
    data["6"]["inputs"]["text"] = "completely different default prompt"
new = mutate("c1-direct-encode-placeholder", _val)
rep = bind.detect_drift(_s_old, new, _bs1)
check("只改默认值 → 不触发漂移（绑定地址没变）", not rep.has_drift,
      f"broken={rep.broken_ports()}")
check("只改默认值时全量指纹仍会变（说明指纹分类是对的）",
      new.fingerprint() != _s_old.fingerprint()
      and new.topology_fingerprint() == _s_old.topology_fingerprint())

# 4.5 新增无关节点 → 老绑定不漂移
def _add(data):
    data["777"] = {"class_type": "Note", "_meta": {"title": "备注"},
                   "inputs": {"text": "x"}}
new = mutate("c1-direct-encode-placeholder", _add)
rep = bind.detect_drift(_s_old, new, _bs1)
check("新增无关节点 → 老绑定不漂移", not rep.has_drift, f"broken={rep.broken_ports()}")
check("但 diff 仍能检出这次新增",
      any(e.kind == snap.DIFF_NODE_ADDED and e.node_id == "777" for e in rep.entries))

# 4.6 节点顺序重排 → 不漂移（指纹键序无关）
def _reorder(data):
    items = list(data.items())
    data.clear()
    data.update(dict(reversed(items)))
new = mutate("c1-direct-encode-placeholder", _reorder)
rep = bind.detect_drift(_s_old, new, _bs1)
check("节点重排 → 不触发漂移", not rep.has_drift and not rep.entries,
      f"entries={len(rep.entries)}")

# 4.7 完全一致 → 无差异
rep = bind.detect_drift(_s_old, _s_old, _bs1)
check("两次相同快照 → 无漂移且无差异", not rep.has_drift and not rep.entries)

# 4.8 check_fingerprint 快检
check("指纹一致时快检无告警",
      bind.check_fingerprint(_s_old, _bs1) == [])
check("只改默认值时快检不打扰用户（拓扑指纹未变）",
      bind.check_fingerprint(
          mutate("c1-direct-encode-placeholder", _val), _bs1) == [])
warns = bind.check_fingerprint(mutate("c1-direct-encode-placeholder", _del), _bs1)
check("结构性变化时快检给出 WARN 而非阻断",
      bool(warns) and all(not p.is_error for p in warns)
      and warns[0].code == bind.CODE_PORT_NOT_LOCKED, f"warns={warns}")


# ---------------------------------------------------------------------------
print("\n=== 5. 禁止静默：每条失效都必须有明确报错 ===")
# ---------------------------------------------------------------------------

# 5.1 端口有值却没有绑定 → 必须报错（这正是历史上那个 Bug 的成因）
empty = bind.BindingSet(workflow_id="c1")
injections, problems = bind.build_injections(_s_old, empty, {"positive": SENTINEL_POS})
errors, _ = bind.split_problems(problems)
check("端口有值但无绑定 → 阻断级报错",
      injections == [] and any(p.code == bind.CODE_NO_BINDING for p in errors),
      f"注入 {len(injections)} codes={[p.code for p in errors]}")

# 5.2 两个端口抢同一槽位 → 报错，不静默裁决
conflict = bind.BindingSet(bindings=[
    bind.Binding("6", "text", "positive"),
    bind.Binding("6", "text", "negative"),
])
injections, problems = bind.build_injections(_s_old, conflict, sample_values())
errors, _ = bind.split_problems(problems)
check("两个端口绑到同一槽位 → 阻断级报错（不取第一个、也不取最后一个）",
      injections == [] and any(p.code == bind.CODE_DUPLICATE_SLOT for p in errors))

# 5.3 绑定存在但用户没给值 → 不注入，也不报错（合理的部分覆盖）
partial = bind.BindingSet(bindings=[bind.Binding("6", "text", "positive"),
                                    bind.Binding("7", "text", "negative")])
injections, problems = bind.build_injections(_s_old, partial, {"positive": SENTINEL_POS})
check("未提供值的端口不产生注入且不算错误",
      len(injections) == 1 and not bind.split_problems(problems)[0],
      f"注入 {len(injections)}")

# 5.4 报错信息必须落到具体 nodeId / fieldName，不能是空泛提示
probs = bind.validate_binding(_s_old, bind.Binding("6", "nope", "positive"))
check("报错文案含具体字段名", all("nope" in p.message for p in probs))
probs = bind.validate_binding(_s_old, bind.Binding("8888", "text", "positive"))
check("报错文案含具体 nodeId", all("8888" in p.message for p in probs))


# ---------------------------------------------------------------------------
print("\n=== 6. lockfile 落盘与版本 ===")
# ---------------------------------------------------------------------------

_bs = bind.lock_bindings(_s_old, truth_bindings(_fx1), workflow_id="wf-1", note="测试")
check("lock_bindings 记录了拓扑指纹", bool(_bs.topology) and bool(_bs.fingerprint))
check("每条绑定自带建立时的拓扑指纹",
      all(b.topology == _bs.topology for b in _bs.bindings))

round_trip = bind.BindingSet.from_dict(_bs.to_dict())
check("to_dict / from_dict 往返一致", round_trip.to_dict() == _bs.to_dict())
check("to_json / from_json 往返一致",
      bind.BindingSet.from_json(_bs.to_json()).to_dict() == _bs.to_dict())

with tempfile.TemporaryDirectory() as td:
    path = os.path.join(td, "wf1.binding.json")
    bind.save_lockfile(_bs, path)
    loaded = bind.load_lockfile(path)
    check("文件落盘往返一致", loaded.to_dict() == _bs.to_dict())
    check("lockfile 内容为可读 JSON（含端口名）",
          "positive" in open(path, encoding="utf-8").read())

bad = json.loads(_bs.to_json())
bad["schema_version"] = 99
try:
    bind.BindingSet.from_dict(bad)
    check("版本不匹配时抛错", False, "没有抛异常")
except ValueError as exc:
    check("版本不匹配时抛错（不做静默兼容）", "版本不匹配" in str(exc))


# ---------------------------------------------------------------------------
print("\n=== 7. 槽位清单导出 ===")
# ---------------------------------------------------------------------------

rows = bind.export_injectable_slots(_c4)
check("导出清单非空", len(rows) > 0)
check("导出项含 nodeId / fieldName / 标题 / 执行态",
      all({"nodeId", "fieldName", "title", "execState"} <= set(r) for r in rows))
check("导出清单不含 bypass 节点（守不变量 B）",
      all(r["nodeId"] not in ("40", "41") for r in rows))
check("导出清单不含连线字段（守不变量 A）",
      all(r["valueKind"] == "literal" for r in rows))
catalog = bind.format_slot_catalog(_c4)
check("清单含标题可直接阅读", "nodeId=" in catalog and "fieldName=" in catalog)
check("清单数目与 injectable_slots 一致",
      catalog.count("●") == len(rows))

_empty_snap = snap.build_snapshot(None)
check("空结构导出清单为空且有可读说明",
      bind.export_injectable_slots(_empty_snap) == []
      and "没有" in bind.format_slot_catalog(_empty_snap))


# ---------------------------------------------------------------------------
print("\n=== 8. 禁止项源码自查：模块内不得有任何推断痕迹 ===")
# ---------------------------------------------------------------------------

with open(os.path.join(PLUGIN, "core", "binding.py"), encoding="utf-8") as fh:
    src = fh.read()
lowered = src.lower()

for token in ("ksampler", "sampler", "cliptextencode", "textencode"):
    check(f"源码未出现 class_type 线索 '{token}'", token not in lowered)
for token in ("upstream", "links_into", "links_from", "walk_", "infer",
              "guess", "heuristic", "fuzzy", "widget"):
    check(f"源码未出现推断/遍历线索 '{token}'", token not in lowered)
check("源码未 import core.workflow（不得复用旧推断逻辑）",
      "from .workflow" not in src and "import workflow" not in src)
check("源码未引用 core.workflow 的旧推断函数",
      all(name not in src for name in ("_linked_prompt_targets",
                                       "_upstream_text_source",
                                       "find_seed_targets",
                                       "build_prompt_injections")))
check("resolve_port 是纯查表（不遍历 nodes）",
      "def resolve_port" in src and "nodes" not in
      src.split("def resolve_port")[1].split("def ")[0])
check("未使用 class_type 做字符串包含判断",
      " in class_type" not in src and "class_type" not in
      src.split("def validate_binding")[1].split("def ")[0].replace(
          "node.class_type or '?'", ""))


# ---------------------------------------------------------------------------
print("\n=== 9. 对照 S0 基线：R1 显式绑定能处理现有实现失效的形态 ===")
# ---------------------------------------------------------------------------

for case_id, fx, s in FIELDS:
    base_path = os.path.join(BASELINES_DIR, "%s.json" % case_id)
    if not os.path.isfile(base_path):
        check(f"{case_id}：基线存在", False, base_path)
        continue
    with open(base_path, encoding="utf-8") as fh:
        base = json.load(fh)
    status = fx["expect_current"]["status"]

    bs = bind.BindingSet(workflow_id=case_id, bindings=truth_bindings(fx))
    injections, problems = bind.build_injections(s, bs, sample_values())
    errors, _ = bind.split_problems(problems)

    if status in ("regression_open", "ambiguous_reject"):
        # 这两档在现有实现下是 0 命中 / 拒绝猜测
        check(f"{case_id}（现状 {status}）：R1 显式绑定可 100% 生效",
              not errors and len(injections) == len(bs.bindings),
              bind.describe_problems(errors))
        check(f"{case_id}：基线确认现有实现确实 0 命中",
              base["injections"] == [], f"基线注入 {len(base['injections'])} 条")
    else:
        check(f"{case_id}（现状 {status}）：R1 绑定同样成立且不更差",
              not errors and len(injections) == len(bs.bindings),
              bind.describe_problems(errors))

# c5 的对照尤其关键：推倒「沿连线找 text 字段」的兜底后，现有实现在此形态上已不再
# 猜测任何落点（基线 0 命中）；而 R1 显式绑定仍能精准打到叶子字面量 value。
# 注意：这里的「0 命中」是**库层**行为——节点层已通过 _require_prompt_targets
# 明确报错并给出可复制的映射模板，不是静默失效，与最初那个 bug 有本质区别。
if os.path.isfile(os.path.join(BASELINES_DIR, "c5-derived-value.json")):
    with open(os.path.join(BASELINES_DIR, "c5-derived-value.json"), encoding="utf-8") as fh:
        base = json.load(fh)
    old = {(str(i["nodeId"]), str(i["fieldName"])) for i in base["injections"]}
    check("c5：推倒连线兜底后现有实现 0 命中（不再错打模板前缀 prefix）",
          old == set(), f"实际 {sorted(old)}")
    _s5 = next(s for c, _, s in FIELDS if c == "c5-derived-value")
    injections, _ = bind.build_injections(_s5,
                                         bind.BindingSet(bindings=truth_bindings(
                                             next(f for c, f, _ in FIELDS
                                                  if c == "c5-derived-value"))),
                                         sample_values())
    new = {(str(i["nodeId"]), str(i["fieldName"])) for i in injections}
    check("c5：R1 显式绑定精确打到叶子字面量 value",
          {("61", "value"), ("63", "value")} <= new, f"实际 {sorted(new)}")
    check("c5：R1 不会误打到现有实现错打的模板前缀 prefix",
          not {("60", "prefix"), ("62", "prefix")} & new, f"实际 {sorted(new)}")


# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
total = len(PASSED) + len(FAILED)
print(f"断言总数 {total}，通过 {len(PASSED)}，失败 {len(FAILED)}")
if FAILED:
    print("\n失败项：")
    for name in FAILED:
        print(f"  - {name}")
    sys.exit(1)
print("全部通过")
