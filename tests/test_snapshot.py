"""
S2 · 发现层快照（Snapshot）离线自测

不依赖 API Key、不联网，也不 import torch —— 只注册 `rhplugin.core` 的包壳并
直接加载 `core/snapshot.py` 与 `core/workflow.py`，因此在任何 Python 环境下都能跑：

    python tests/test_snapshot.py

覆盖：
  * 归一化保留 `_meta.title` 与节点执行态
  * 字段形态判定（字面量 / 连线 / 未知）
  * 连线边抽取与悬空连线告警
  * 节点执行态判定（mode / bypass / mute / 无证据）
  * 指纹稳定性（键序无关、值变化不影响拓扑指纹）
  * diff 检出与「会导致绑定失效」标记
  * JSON 落盘往返
"""

import json
import os
import sys
import types
from typing import Any, List, Tuple

ROOT = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.abspath(os.path.join(ROOT, ".."))

# 与 test_core.py 相同的虚拟包手法：插件目录名含连字符，不能直接做包名。
# 这里额外手工注册 rhplugin.core 并指向 core/ 目录，从而绕开 core/__init__.py
# 对 torch 的间接依赖（latent 模块），让本文件可以脱离 ComfyUI 环境单独运行。
_pkg = types.ModuleType("rhplugin")
_pkg.__path__ = [PLUGIN]
sys.modules["rhplugin"] = _pkg
_core_pkg = types.ModuleType("rhplugin.core")
_core_pkg.__path__ = [os.path.join(PLUGIN, "core")]
sys.modules["rhplugin.core"] = _core_pkg

from rhplugin.core import snapshot as snap  # noqa: E402
from rhplugin.core import workflow as wf_mod  # noqa: E402

PASSED = []
FAILED = []


def check(name, cond, extra=""):
    if cond:
        PASSED.append(name)
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name} {extra}")


# ---------------------------------------------------------------------------
# 一份通用形态的 API-format 工作流（平台 getJsonApiFormat 的真实返回形态）
# ---------------------------------------------------------------------------
API_PROMPT = {
    "3": {
        "class_type": "KSampler",
        "inputs": {
            "seed": 669816362794144,
            "steps": 20,
            "cfg": 8,
            "denoise": 1.0,
            "positive": ["6", 0],
            "negative": ["7", 0],
            "latent_image": ["5", 0],
        },
        "_meta": {"title": "采样器"},
    },
    "5": {
        "class_type": "EmptyLatentImage",
        "inputs": {"width": 512, "height": 512, "batch_size": 1},
        "_meta": {"title": "空 latent"},
    },
    "6": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "{positive}"},
        "_meta": {"title": "正面提示词"},
    },
    "7": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "{negative}"},
        "_meta": {"title": "负面提示词"},
    },
    "9": {
        "class_type": "SaveImage",
        "inputs": {"images": ["3", 0]},
        "_meta": {"title": "保存图片"},
    },
}

# 平台实际返回：data.prompt 是一整个 JSON 字符串
PLATFORM_BODY = {"code": 0, "msg": "success", "data": {"prompt": json.dumps(API_PROMPT)}}


print("=== 1. 归一化必须保住平台白送的元信息 ===")
norm = wf_mod.normalize(API_PROMPT)
check("class_type 与 inputs 键未变（兼容既有调用方）",
      set(norm["6"]) >= {"class_type", "inputs"})
check("_meta.title 被保留", norm["6"]["meta"].get("title") == "正面提示词")
check("无 _meta 的节点也有空 meta", norm["3"]["meta"].get("title") == "采样器")
check("探查清单里带上用户自定义标题",
      "正面提示词" in wf_mod.describe(norm), wf_mod.describe(norm)[:120])

print("\n=== 2. 从平台原始响应构建快照 ===")
snap_a = snap.build_snapshot(PLATFORM_BODY, workflow_id="12345")
check("解析出全部 5 个节点", len(snap_a.nodes) == 5, str(sorted(snap_a.nodes)))
check("节点标题取自 _meta.title", snap_a.node("6").title == "正面提示词")
check("节点类型正确", snap_a.node("3").class_type == "KSampler")
check("直接喂 API-format 与平台包壳得到同一份快照",
      snap.build_snapshot(API_PROMPT, workflow_id="12345").fingerprint()
      == snap_a.fingerprint())

print("\n=== 3. 字段形态：字面量 / 连线 / 未知 ===")
check("标量为字面量", snap_a.slot("3", "seed").is_literal)
check("字面量保留原值", snap_a.slot("3", "steps").value == 20)
check("[] 形态判定为连线", snap_a.slot("3", "positive").is_link)
check("连线源节点正确", snap_a.slot("3", "positive").link_src == "6")
check("连线输出槽正确", snap_a.slot("3", "positive").link_slot == 0)
check("连线可还原成 API-format 写法", snap_a.slot("3", "negative").link_ref() == ["7", 0])
check("字面量字段才会被列为可注入", ("3", "seed") in snap_a.injectable_slots())
check("连线字段不在可注入清单里", ("3", "positive") not in snap_a.injectable_slots())
check("文本占位符字段是可注入字面量", ("6", "text") in snap_a.injectable_slots())
check("可注入清单不含任何连线字段",
      all(snap_a.slot(n, f).is_literal for n, f in snap_a.injectable_slots()))

unk_struct = {
    "1": {"class_type": "X", "inputs": {"opts": {"a": 1}, "empty": []}},
    "2": {"class_type": "Y", "inputs": {"n": None}},
}
snap_unk = snap.build_snapshot(unk_struct)
check("复合结构判定为 unknown", snap_unk.slot("1", "opts").value_kind == snap.VALUE_UNKNOWN)
check("空列表判定为 unknown", snap_unk.slot("1", "empty").value_kind == snap.VALUE_UNKNOWN)
check("unknown 形态不算可注入", ("1", "opts") not in snap_unk.injectable_slots()
      and ("1", "empty") not in snap_unk.injectable_slots())
check("None 不是连线，按字面量处理（可注入）", snap_unk.slot("2", "n").is_literal)

print("\n=== 4. 连线边列表 ===")
pairs = {(e.src, e.dst, e.dst_field) for e in snap_a.edges}
check("抽出全部 4 条连线", len(snap_a.edges) == 4, str(snap_a.edges))
check("采样器 positive 来自节点 6", ("6", "3", "positive") in pairs)
check("采样器 negative 来自节点 7", ("7", "3", "negative") in pairs)
check("采样器 latent 来自节点 5", ("5", "3", "latent_image") in pairs)
check("保存节点接在采样器之后", ("3", "9", "images") in pairs)
check("可以按节点查入边", [e.dst_field for e in snap_a.links_into("3")] ==
      ["latent_image", "negative", "positive"], str(snap_a.links_into("3")))
check("可以按节点查出处", [e.dst for e in snap_a.links_from("3")] == ["9"])

dangling = snap.build_snapshot({"2": {"class_type": "A", "inputs": {"x": ["99", 0]}}})
check("连线指向不存在的节点时给出告警",
      any("99" in w for w in dangling.warnings), str(dangling.warnings))

print("\n=== 5. 节点执行态（不变量 B：不在不执行的节点上注入） ===")
exec_struct = {
    "1": {"class_type": "A", "inputs": {"a": 1}, "mode": 0},
    "2": {"class_type": "B", "inputs": {"a": 1}, "mode": 4, "_meta": {"title": "被旁路"}},
    "3": {"class_type": "C", "inputs": {"a": 1}, "mode": 2},
    "4": {"class_type": "D", "inputs": {"a": 1}, "bypass": True},
    "5": {"class_type": "E", "inputs": {"a": 1}, "muted": True},
    "6": {"class_type": "F", "inputs": {"a": 1}},
    "7": {"class_type": "G", "inputs": {"a": 1}, "mode": 99},
}
snap_exec = snap.build_snapshot(exec_struct)
check("mode=0 视为会执行", snap_exec.node("1").is_executing)
check("mode=4 判定为 bypass", snap_exec.node("2").exec_state == snap.EXEC_BYPASSED)
check("bypass 的判定依据被记录", snap_exec.node("2").exec_source == "mode")
check("mode=2 判定为 mute", snap_exec.node("3").exec_state == snap.EXEC_MUTED)
check("bypass=True 也能识别", snap_exec.node("4").exec_state == snap.EXEC_BYPASSED)
check("muted=True 也能识别", snap_exec.node("5").exec_state == snap.EXEC_MUTED)
check("无证据时按会执行处理", snap_exec.node("6").is_executing)
check("无证据时依据记为 default（是假设而非平台明示）",
      snap_exec.node("6").exec_source == "default")
check("不认识的 mode 取值不瞎猜", snap_exec.node("7").is_executing
      and snap_exec.node("7").exec_source == "default")
check("不执行的节点不进入可注入清单",
      [n for n, _f in snap_exec.injectable_slots()] == ["1", "6", "7"],
      str(snap_exec.injectable_slots()))
check("nodes 列表形态里的 mode 也不会漏",
      snap.build_snapshot(
          {"nodes": [{"id": 8, "type": "H", "inputs": {"a": 1}, "mode": 4}]}
      ).node("8").exec_state == snap.EXEC_BYPASSED)

print("\n=== 6. 指纹：可复现、可检出结构变化 ===")
again = snap.build_snapshot(PLATFORM_BODY, workflow_id="12345")
check("同一工作流两次快照指纹一致", snap_a.fingerprint() == again.fingerprint())

shuffled = json.loads(json.dumps(API_PROMPT))
shuffled = dict(reversed(list(shuffled.items())))
snap_shuffled = snap.build_snapshot(shuffled, workflow_id="12345")
check("节点顺序不同不影响指纹", snap_a.fingerprint() == snap_shuffled.fingerprint())

edited_value = json.loads(json.dumps(API_PROMPT))
edited_value["6"]["inputs"]["text"] = "换了个默认提示词"
snap_value = snap.build_snapshot(edited_value, workflow_id="12345")
check("只改默认值：全量指纹变化", snap_a.fingerprint() != snap_value.fingerprint())
check("只改默认值：拓扑指纹不变（绑定仍成立）",
      snap_a.topology_fingerprint() == snap_value.topology_fingerprint())

edited_topo = json.loads(json.dumps(API_PROMPT))
edited_topo["3"]["inputs"]["positive"] = ["8", 0]
snap_topo = snap.build_snapshot(edited_topo, workflow_id="12345")
check("改连线：拓扑指纹变化", snap_a.topology_fingerprint() != snap_topo.topology_fingerprint())

edited_id = {"10": API_PROMPT["3"], "5": API_PROMPT["5"], "6": API_PROMPT["6"],
             "7": API_PROMPT["7"], "9": API_PROMPT["9"]}
snap_id = snap.build_snapshot(edited_id, workflow_id="12345")
check("nodeId 变化：拓扑指纹变化",
      snap_a.topology_fingerprint() != snap_id.topology_fingerprint())
check("指纹是定长十六进制串", len(snap_a.fingerprint()) == 64)

print("\n=== 7. diff：结构变化必须被检出 ===")
entries = snap.diff_snapshots(snap_a, snap_a)
check("无变化时 diff 为空", entries == [])
check("无变化时摘要说明一致", "完全一致" in snap.summarize_diff(entries))

removed = json.loads(json.dumps(API_PROMPT))
del removed["7"]
diff_rm = snap.diff_snapshots(snap_a, snap.build_snapshot(removed, workflow_id="12345"))
check("删除节点被检出",
      any(e.kind == snap.DIFF_NODE_REMOVED and e.node_id == "7" for e in diff_rm))
check("删除节点标记为会导致绑定失效",
      any(e.breaking for e in diff_rm) and snap.has_breaking_change(diff_rm))

to_link = json.loads(json.dumps(API_PROMPT))
to_link["6"]["inputs"]["text"] = ["12", 0]
diff_kind = snap.diff_snapshots(snap_a, snap.build_snapshot(to_link, workflow_id="12345"))
check("字面量变连线被检出",
      any(e.kind == snap.DIFF_KIND_CHANGED and e.field == "text" for e in diff_kind))
check("字面量变连线标记为会导致绑定失效",
      any(e.breaking and e.node_id == "6" for e in diff_kind))

diff_value = snap.diff_snapshots(snap_a, snap_value)
check("默认值变化被检出",
      any(e.kind == snap.DIFF_VALUE_CHANGED and e.node_id == "6" for e in diff_value))
check("默认值变化不算绑定失效", not snap.has_breaking_change(diff_value))

diff_link = snap.diff_snapshots(snap_a, snap_topo)
check("连线改向被检出",
      any(e.kind == snap.DIFF_LINK_CHANGED and e.node_id == "3" for e in diff_link))

muted_now = json.loads(json.dumps(API_PROMPT))
muted_now["6"]["mode"] = 2
diff_exec = snap.diff_snapshots(snap_a, snap.build_snapshot(muted_now, workflow_id="12345"))
check("节点被静音被检出",
      any(e.kind == snap.DIFF_EXEC_CHANGED and e.node_id == "6" for e in diff_exec))
check("从会执行变为不执行标记为绑定失效",
      any(e.breaking and e.kind == snap.DIFF_EXEC_CHANGED for e in diff_exec))

summary = snap.summarize_diff(diff_rm)
check("摘要是中文且可读", "删除节点" in summary and "绑定失效" in summary, summary)

print("\n=== 8. 序列化：可落盘、可回读 ===")
text = snap_a.to_json()
restored = snap.WorkflowSnapshot.from_json(text)
check("JSON 往返后指纹一致", restored.fingerprint() == snap_a.fingerprint())
check("JSON 往返后边数一致", len(restored.edges) == len(snap_a.edges))
check("JSON 往返后执行态一致",
      restored.node("6").exec_state == snap_a.node("6").exec_state)
check("落盘内容可直接被人读（含节点标题）", "正面提示词" in text)

check("可还原成既有归一化结构",
      snap_a.to_structure()["3"]["inputs"]["positive"] == ["6", 0])
check("还原后的结构与 normalize 结果字段一致",
      set(snap_a.to_structure()["3"]["inputs"]) == set(norm["3"]["inputs"]))

print("\n=== 9. 空结构与异常输入不能崩 ===")
for bad in (None, {}, "", [], {"nodes": []}):
    empty = snap.build_snapshot(bad, workflow_id="1")
    check(f"空输入 {bad!r} 返回空快照而非异常", len(empty.nodes) == 0)
check("空快照带一条可读告警", bool(snap.build_snapshot(None).warnings))

print("\n=== 10. 禁止项自查：发现层不得有任何硬编码 ===")
with open(os.path.join(PLUGIN, "core", "snapshot.py"), encoding="utf-8") as fh:
    src = fh.read().lower()
for banned in ("sampler", "cliptextencode", "ksampler", "positive\"", "seed"):
    check(f"源码未出现硬编码线索 {banned!r}", banned not in src)

print("\n=== 11. 交叉一致性：新发现层与既有 normalize 路径语义对等 ===")


def cross_check(raw: Any) -> Tuple[List[str], int, int, int]:
    """同一份原始数据分别走 normalize() 与 build_snapshot()，逐项比对。

    返回 (问题列表, 字段总数, 老路径节点数, 新路径节点数)。这是「将来把注入逻辑
    切到快照上」的唯一依据：两条路径必须给出同一份事实，否则切换就是改行为。
    """
    norm_x = wf_mod.normalize(raw)
    snap_x = snap.build_snapshot(raw)
    problems: List[str] = []

    if set(norm_x) != set(snap_x.nodes):
        problems.append(
            "节点集合不一致：" + str(sorted(set(norm_x) ^ set(snap_x.nodes)))
        )

    field_count = 0
    for node_id in sorted(norm_x):
        old = norm_x[node_id]
        new = snap_x.node(node_id)
        if new is None:
            problems.append(f"nodeId={node_id} 在新路径里丢失")
            continue
        if (old.get("class_type") or "") != new.class_type:
            problems.append(
                f"nodeId={node_id} class_type 不一致：{old.get('class_type')} vs {new.class_type}"
            )
        old_inputs = old.get("inputs")
        old_inputs = old_inputs if isinstance(old_inputs, dict) else {}
        field_count += len(old_inputs)
        if set(old_inputs) != set(new.fields):
            problems.append(
                f"nodeId={node_id} 字段集合不一致："
                + str(sorted(set(old_inputs) ^ set(new.fields)))
            )
            continue
        for name, value in old_inputs.items():
            f = new.fields[name]
            if f.is_link:
                # 连线字段在快照里只保留 [源节点, 输出槽] 引用，与原始值逐元素比对
                ok = (
                    isinstance(value, (list, tuple))
                    and len(value) >= 1
                    and str(value[0]) == f.link_src
                    and (f.link_slot is None or (len(value) > 1 and value[1] == f.link_slot))
                )
                if not ok:
                    problems.append(
                        f"nodeId={node_id} 字段「{name}」连线不一致："
                        f"{value} vs {f.link_ref()}"
                    )
            elif f.value != value:
                problems.append(
                    f"nodeId={node_id} 字段「{name}」取值不一致：{value} vs {f.value}"
                )
        if (old.get("meta") or {}) != new.meta:
            problems.append(f"nodeId={node_id} meta 不一致")
    return problems, field_count, len(norm_x), len(snap_x.nodes)


examples_dir = os.path.join(PLUGIN, "examples")
table = []
for filename in sorted(os.listdir(examples_dir)):
    if not filename.endswith(".json"):
        continue
    with open(os.path.join(examples_dir, filename), encoding="utf-8") as fh:
        raw_example = json.load(fh)
    problems_x, fields_x, old_count, new_count = cross_check(raw_example)
    table.append((filename, old_count, new_count, fields_x, not problems_x))
    check(f"{filename}：两条路径节点数一致", old_count == new_count,
          f"{old_count} vs {new_count}")
    check(f"{filename}：class_type / 字段 / 取值 / meta 逐项一致",
          not problems_x, "; ".join(problems_x[:3]))

# examples 是 ComfyUI 前端（UI）格式，参数放在 widgets_values 里而没有 inputs 字典，
# 因此上面的「字段级」断言落在空集上是平凡成立的。真正的字段级比对要用 API-format
# 样本（平台 getJsonApiFormat 的返回形态）再跑一遍，否则这条断言没有说服力。
api_samples = (
    ("API-format 样本（连线 + _meta.title）", API_PROMPT),
    ("执行态样本（mode / bypass / mute）", exec_struct),
    ("悬空连线样本", {"2": {"class_type": "A", "inputs": {"x": ["99", 0]}}}),
)
for label, raw_sample in api_samples:
    problems_y, fields_y, old_count, new_count = cross_check(raw_sample)
    check(f"{label}：{fields_y} 个字段逐项一致", fields_y > 0 and not problems_y,
          "; ".join(problems_y[:3]))

print("\n  examples 交叉一致性结果：")
print("  文件名".ljust(34) + "老路径节点".rjust(10) + "新路径节点".rjust(10)
      + "字段数".rjust(8) + "  一致")
for filename, old_count, new_count, fields_x, ok in table:
    print(
        f"  {filename}".ljust(34)
        + str(old_count).rjust(10)
        + str(new_count).rjust(10)
        + str(fields_x).rjust(8)
        + ("  是" if ok else "  否")
    )
check("examples 全部交叉一致（任一不一致即如实失败）",
      all(row[4] for row in table))

# examples 是前端格式、字段数为 0，字段级断言在上面是平凡成立的。S0 的 fixture
# 才是平台真实形态（API-format，data.prompt 为 JSON 字符串，含 _meta.title 与连线），
# 因此同样的比对在 fixture 上再跑一遍 —— 这里字段数不为 0，断言才算真的有约束力。
fixtures_dir = os.path.join(ROOT, "fixtures", "workflows")
fixture_rows = []
if os.path.isdir(fixtures_dir):
    for filename in sorted(os.listdir(fixtures_dir)):
        if not filename.endswith(".json"):
            continue
        with open(os.path.join(fixtures_dir, filename), encoding="utf-8") as fh:
            case = json.load(fh)
        raw_case = case.get("api_response", case)
        problems_z, fields_z, old_count, new_count = cross_check(raw_case)
        fixture_rows.append((filename, old_count, new_count, fields_z, not problems_z))
        check(f"fixture {filename}：两条路径逐项一致（{fields_z} 个字段）",
              fields_z > 0 and not problems_z, "; ".join(problems_z[:3]))
    print("\n  S0 fixture（API-format）交叉一致性结果：")
    print("  文件名".ljust(40) + "老路径节点".rjust(10) + "新路径节点".rjust(10)
          + "字段数".rjust(8) + "  一致")
    for filename, old_count, new_count, fields_z, ok in fixture_rows:
        print(
            f"  {filename}".ljust(40)
            + str(old_count).rjust(10)
            + str(new_count).rjust(10)
            + str(fields_z).rjust(8)
            + ("  是" if ok else "  否")
        )
    check("S0 fixture 全部交叉一致", all(row[4] for row in fixture_rows))

# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
if FAILED:
    print("失败项：" + ", ".join(FAILED))
    sys.exit(1)
print("全部通过")
