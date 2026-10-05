"""
S2 · 发现层（Discovery）：把云端工作流结构固化成**结构化快照（Snapshot）**。

## 为什么要有这一层

`getJsonApiFormat` 返回的是一坨原始 API-format JSON，直接拿来用有三个硬伤：

1. **分不清字面量和连线。** 平台官方明确：API-format 里 `[]` 包裹的 `fieldValue`
   表示该字段被连线接管。本项目不变量 A 规定只向字面量字段注入；没有显式的
   `value_kind` 标注，这条红线就只能靠每个调用方各自零散判断，必漏。
2. **不知道节点会不会被执行。** 不变量 B 要求落点在实际执行路径上，而 bypass /
   mute 节点上的注入不会被执行。执行态散落在 `mode` / `bypass` / `muted` 等
   平台字段上，原始 JSON 里看不出来。
3. **无法 diff。** 云端工作流随时会被作者编辑，nodeId 和拓扑一变，昨天还生效的
   绑定今天就静默失效——这正是「绑定漂移」，没有指纹就无从检测。

Snapshot 一次解决这三点：每个字段标注 `value_kind`、每个节点标注执行态、整张图
抽成连线边列表，并给出两个指纹用于 diff 与漂移检测。

## 本层的边界

* **不推断语义。** 不出现任何 class_type 名称判断、任何具体节点 ID、任何「某工作流
  的某某节点」分支。本层只做「记录事实 + 判定字段形态」，语义求解是 S5 解析层的事。
* **不做注入校验。** 存在性 / 字面量性 / 可达性的拦截由 S6 校验器统一负责；本层只
  提供 `injectable_slots()` 这类事实查询，不替校验器做决定。
* **纯离线。** 不联网、不依赖 ComfyUI，可直接对任意结构数据做单元测试。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import workflow as _workflow

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

# 字段形态（value_kind）
VALUE_LITERAL = "literal"   # 字面量：可注入（不变量 A）
VALUE_LINK = "link"         # 被连线接管：平台明确不建议修改（不变量 A）
VALUE_UNKNOWN = "unknown"   # 既不是字面量也不是连线（复合结构/空列表）：不注入

# 节点执行态（不变量 B）
EXEC_ACTIVE = "active"      # 会被执行
EXEC_BYPASSED = "bypassed"  # 被旁路：节点不参与计算
EXEC_MUTED = "muted"        # 被静音：节点整体不执行

# ComfyUI litegraph 的 mode 常量语义（平台/ComfyUI 通用字段，非某工作流约定）
_MODE_ACTIVE = (0, 1, 3)
_MODE_MUTED = (2,)
_MODE_BYPASSED = (4,)

_MUTE_FLAGS = ("mute", "muted", "is_muted", "isMuted", "disabled", "is_disabled")
_BYPASS_FLAGS = ("bypass", "bypassed", "is_bypassed", "isBypassed")
_FALSE_WORDS = ("", "false", "0", "no", "off", "none", "null")

# diff 条目类型
DIFF_NODE_ADDED = "node_added"
DIFF_NODE_REMOVED = "node_removed"
DIFF_CLASS_CHANGED = "class_type_changed"
DIFF_TITLE_CHANGED = "title_changed"
DIFF_EXEC_CHANGED = "exec_state_changed"
DIFF_FIELD_ADDED = "field_added"
DIFF_FIELD_REMOVED = "field_removed"
DIFF_KIND_CHANGED = "value_kind_changed"
DIFF_LINK_CHANGED = "link_retargeted"
DIFF_VALUE_CHANGED = "value_changed"


# --------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------


@dataclass
class FieldSnapshot:
    """一个输入字段的事实记录。"""

    node_id: str
    name: str
    value_kind: str = VALUE_UNKNOWN
    value: Any = None                    # 仅字面量/未知形态保留原值
    link_src: Optional[str] = None       # 连线源 nodeId
    link_slot: Optional[int] = None      # 连线源的输出槽序号

    @property
    def is_literal(self) -> bool:
        return self.value_kind == VALUE_LITERAL

    @property
    def is_link(self) -> bool:
        return self.value_kind == VALUE_LINK

    @property
    def is_injectable(self) -> bool:
        """不变量 A：只有字面量字段是可注入候选。"""
        return self.is_literal

    def link_ref(self) -> Optional[List[Any]]:
        """还原成 API-format 的连线写法 `[源节点, 输出槽]`；非连线返回 None。"""
        if not self.is_link or self.link_src is None:
            return None
        return [self.link_src, self.link_slot if self.link_slot is not None else 0]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "name": self.name,
            "value_kind": self.value_kind,
            "value": self.value,
            "link_src": self.link_src,
            "link_slot": self.link_slot,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FieldSnapshot":
        return cls(
            node_id=str(data.get("node_id", "")),
            name=str(data.get("name", "")),
            value_kind=str(data.get("value_kind", VALUE_UNKNOWN)),
            value=data.get("value"),
            link_src=data.get("link_src"),
            link_slot=data.get("link_slot"),
        )


@dataclass
class NodeSnapshot:
    """一个节点的事实记录。"""

    node_id: str
    class_type: str = ""
    title: str = ""                      # 用户在画布上起的标题（_meta.title）
    exec_state: str = EXEC_ACTIVE        # 是否会被执行（不变量 B）
    exec_source: str = "default"         # 判定依据：mode / flag / default
    exec_evidence: Dict[str, Any] = field(default_factory=dict)
    meta: Dict[str, Any] = field(default_factory=dict)
    fields: Dict[str, FieldSnapshot] = field(default_factory=dict)

    @property
    def is_executing(self) -> bool:
        return self.exec_state == EXEC_ACTIVE

    def literal_fields(self) -> Dict[str, FieldSnapshot]:
        return {n: f for n, f in self.fields.items() if f.is_literal}

    def link_fields(self) -> Dict[str, FieldSnapshot]:
        return {n: f for n, f in self.fields.items() if f.is_link}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "class_type": self.class_type,
            "title": self.title,
            "exec_state": self.exec_state,
            "exec_source": self.exec_source,
            "exec_evidence": dict(self.exec_evidence),
            "meta": dict(self.meta),
            "fields": {n: f.to_dict() for n, f in self.fields.items()},
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "NodeSnapshot":
        fields_data = data.get("fields") or {}
        return cls(
            node_id=str(data.get("node_id", "")),
            class_type=str(data.get("class_type", "")),
            title=str(data.get("title", "")),
            exec_state=str(data.get("exec_state", EXEC_ACTIVE)),
            exec_source=str(data.get("exec_source", "default")),
            exec_evidence=dict(data.get("exec_evidence") or {}),
            meta=dict(data.get("meta") or {}),
            fields={
                str(n): FieldSnapshot.from_dict(f) for n, f in fields_data.items()
            },
        )


@dataclass
class Edge:
    """一条连线：src 节点的第 src_slot 个输出 → dst 节点的 dst_field 输入。"""

    src: str
    src_slot: Optional[int]
    dst: str
    dst_field: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "src": self.src,
            "src_slot": self.src_slot,
            "dst": self.dst,
            "dst_field": self.dst_field,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Edge":
        return cls(
            src=str(data.get("src", "")),
            src_slot=data.get("src_slot"),
            dst=str(data.get("dst", "")),
            dst_field=str(data.get("dst_field", "")),
        )


@dataclass
class WorkflowSnapshot:
    """一份完整的工作流结构快照：可缓存、可 diff、可落盘（JSON）。"""

    workflow_id: str = ""
    nodes: Dict[str, NodeSnapshot] = field(default_factory=dict)
    edges: List[Edge] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    # ---------------- 查询 ----------------

    def node(self, node_id: str) -> Optional[NodeSnapshot]:
        return self.nodes.get(str(node_id))

    def slot(self, node_id: str, field_name: str) -> Optional[FieldSnapshot]:
        node = self.node(node_id)
        return node.fields.get(str(field_name)) if node else None

    def injectable_slots(self) -> List[Tuple[str, str]]:
        """满足不变量 A + 不变量 B 的可注入槽位，返回 [(nodeId, fieldName)]。

        即：字段是字面量（`[]` 之外）且所属节点会被执行。这两条任一不满足时，
        注入要么违反平台规则，要么打了也白打。
        """
        slots: List[Tuple[str, str]] = []
        for node_id in _sorted_ids(self.nodes):
            node = self.nodes[node_id]
            if not node.is_executing:
                continue
            for name in sorted(node.fields):
                if node.fields[name].is_literal:
                    slots.append((node_id, name))
        return slots

    def links_into(self, node_id: str) -> List[Edge]:
        """指向该节点的连线（它的输入被谁接管）。"""
        key = str(node_id)
        return [e for e in self.edges if e.dst == key]

    def links_from(self, node_id: str) -> List[Edge]:
        """从该节点发出的连线（谁用了它的输出）。"""
        key = str(node_id)
        return [e for e in self.edges if e.src == key]

    # ---------------- 指纹 ----------------

    def fingerprint(self) -> str:
        """全量指纹：含字面量取值。用于判断「两次快照是否完全一致」。"""
        return _sha256(_canonical(_payload(self, with_values=True)))

    def topology_fingerprint(self) -> str:
        """拓扑指纹：不含字面量取值。

        只关心「绑定是否还成立」——nodeId、字段名、字段形态、连线目标、执行态。
        作者只改了某个提示词默认值时不算漂移，改了结构才算。
        """
        return _sha256(_canonical(_payload(self, with_values=False)))

    # ---------------- 序列化 ----------------

    def to_structure(self) -> Dict[str, Dict[str, Any]]:
        """还原成 `core.workflow` 的归一化结构，供既有函数直接消费。"""
        out: Dict[str, Dict[str, Any]] = {}
        for node_id, node in self.nodes.items():
            inputs: Dict[str, Any] = {}
            for name, f in node.fields.items():
                inputs[name] = f.link_ref() if f.is_link else f.value
            out[node_id] = {
                "class_type": node.class_type,
                "inputs": inputs,
                "meta": dict(node.meta),
            }
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "fingerprint": self.fingerprint(),
            "topology_fingerprint": self.topology_fingerprint(),
            "nodes": {n: node.to_dict() for n, node in self.nodes.items()},
            "edges": [e.to_dict() for e in self.edges],
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkflowSnapshot":
        nodes_data = data.get("nodes") or {}
        return cls(
            workflow_id=str(data.get("workflow_id", "")),
            nodes={
                str(n): NodeSnapshot.from_dict(node) for n, node in nodes_data.items()
            },
            edges=[Edge.from_dict(e) for e in (data.get("edges") or [])],
            warnings=list(data.get("warnings") or []),
        )

    def to_json(self, indent: Optional[int] = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, default=str)

    @classmethod
    def from_json(cls, text: str) -> "WorkflowSnapshot":
        return cls.from_dict(json.loads(text))


# --------------------------------------------------------------------------
# 构建
# --------------------------------------------------------------------------


def build_snapshot(raw: Any, workflow_id: str = "") -> WorkflowSnapshot:
    """从 `getJsonApiFormat` 的原始响应（或其任意中间形态）构建快照。

    拿不到任何节点时返回空快照 + 一条 warning，而不是抛异常——调用方需要据此
    给出「结构读取失败」的明确提示，不能静默降级。
    """
    return snapshot_from_structure(_workflow.normalize(raw), workflow_id=workflow_id)


def snapshot_from_structure(
    structure: Dict[str, Dict[str, Any]], workflow_id: str = ""
) -> WorkflowSnapshot:
    """从已归一化的结构（{nodeId: {class_type, inputs, meta}}）构建快照。"""
    nodes: Dict[str, NodeSnapshot] = {}

    for node_id in _sorted_ids(structure):
        raw_node = structure[node_id]
        if not isinstance(raw_node, dict):
            continue
        meta = raw_node.get("meta")
        meta = dict(meta) if isinstance(meta, dict) else {}
        inputs = raw_node.get("inputs")
        inputs = inputs if isinstance(inputs, dict) else {}

        exec_state, exec_source, exec_evidence = _exec_state_of(meta)
        fields: Dict[str, FieldSnapshot] = {}
        for name in sorted(inputs):
            kind, value, src, slot = _classify_value(inputs[name])
            fields[str(name)] = FieldSnapshot(
                node_id=str(node_id),
                name=str(name),
                value_kind=kind,
                value=value,
                link_src=src,
                link_slot=slot,
            )
        nodes[str(node_id)] = NodeSnapshot(
            node_id=str(node_id),
            class_type=str(raw_node.get("class_type") or ""),
            title=str(meta.get("title") or ""),
            exec_state=exec_state,
            exec_source=exec_source,
            exec_evidence=exec_evidence,
            meta=meta,
            fields=fields,
        )

    snapshot = WorkflowSnapshot(workflow_id=str(workflow_id or ""), nodes=nodes)

    for node_id in _sorted_ids(nodes):
        node = nodes[node_id]
        for name in sorted(node.fields):
            f = node.fields[name]
            if not f.is_link or f.link_src is None:
                continue
            snapshot.edges.append(
                Edge(src=f.link_src, src_slot=f.link_slot, dst=node_id, dst_field=name)
            )
            if f.link_src not in nodes:
                snapshot.warnings.append(
                    f"节点 {node_id} 的字段「{name}」连线指向 nodeId={f.link_src}，"
                    "但该节点不在云端返回的结构里"
                )

    if not nodes:
        snapshot.warnings.append(
            "云端工作流结构读取失败：没有解析出任何节点（getJsonApiFormat 返回为空或形态未知）"
        )
    return snapshot


# --------------------------------------------------------------------------
# diff
# --------------------------------------------------------------------------


@dataclass
class DiffEntry:
    """两次快照之间的一处差异。"""

    kind: str
    node_id: str = ""
    field: str = ""
    old: Any = None
    new: Any = None
    breaking: bool = False   # 是否会让已固化的绑定失效（漂移信号）

    def describe(self) -> str:
        where = f"nodeId={self.node_id}" + (f" 字段「{self.field}」" if self.field else "")
        flag = "【会导致绑定失效】" if self.breaking else ""
        return f"{flag}{_KIND_LABELS.get(self.kind, self.kind)}：{where}（{self.old} → {self.new}）"


_KIND_LABELS = {
    DIFF_NODE_ADDED: "新增节点",
    DIFF_NODE_REMOVED: "删除节点",
    DIFF_CLASS_CHANGED: "节点类型变化",
    DIFF_TITLE_CHANGED: "节点标题变化",
    DIFF_EXEC_CHANGED: "节点执行态变化",
    DIFF_FIELD_ADDED: "新增字段",
    DIFF_FIELD_REMOVED: "删除字段",
    DIFF_KIND_CHANGED: "字段形态变化",
    DIFF_LINK_CHANGED: "连线目标变化",
    DIFF_VALUE_CHANGED: "默认值变化",
}


def diff_snapshots(old: WorkflowSnapshot, new: WorkflowSnapshot) -> List[DiffEntry]:
    """逐节点、逐字段比对两份快照，返回差异清单（无差异返回空列表）。"""
    entries: List[DiffEntry] = []
    old_ids, new_ids = set(old.nodes), set(new.nodes)

    for node_id in _sorted_ids(new_ids - old_ids):
        entries.append(
            DiffEntry(DIFF_NODE_ADDED, node_id, new=new.nodes[node_id].class_type)
        )
    for node_id in _sorted_ids(old_ids - new_ids):
        entries.append(
            DiffEntry(
                DIFF_NODE_REMOVED,
                node_id,
                old=old.nodes[node_id].class_type,
                breaking=True,
            )
        )

    for node_id in _sorted_ids(old_ids & new_ids):
        o, n = old.nodes[node_id], new.nodes[node_id]
        if o.class_type != n.class_type:
            entries.append(
                DiffEntry(DIFF_CLASS_CHANGED, node_id, old=o.class_type,
                          new=n.class_type, breaking=True)
            )
        if o.title != n.title:
            entries.append(DiffEntry(DIFF_TITLE_CHANGED, node_id, old=o.title, new=n.title))
        if o.exec_state != n.exec_state:
            entries.append(
                DiffEntry(
                    DIFF_EXEC_CHANGED,
                    node_id,
                    old=o.exec_state,
                    new=n.exec_state,
                    # 从「会执行」变成「不执行」：打在这上面的注入不再生效
                    breaking=o.is_executing and not n.is_executing,
                )
            )

        o_fields, n_fields = set(o.fields), set(n.fields)
        for name in sorted(n_fields - o_fields):
            entries.append(DiffEntry(DIFF_FIELD_ADDED, node_id, name, new=n.fields[name].value_kind))
        for name in sorted(o_fields - n_fields):
            entries.append(
                DiffEntry(DIFF_FIELD_REMOVED, node_id, name,
                          old=o.fields[name].value_kind, breaking=True)
            )
        for name in sorted(o_fields & n_fields):
            of, nf = o.fields[name], n.fields[name]
            if of.value_kind != nf.value_kind:
                entries.append(
                    DiffEntry(
                        DIFF_KIND_CHANGED,
                        node_id,
                        name,
                        old=of.value_kind,
                        new=nf.value_kind,
                        # 字面量变成连线/未知：按不变量 A 不再可注入
                        breaking=of.is_literal and not nf.is_literal,
                    )
                )
                continue
            if of.is_link and (of.link_src, of.link_slot) != (nf.link_src, nf.link_slot):
                entries.append(
                    DiffEntry(
                        DIFF_LINK_CHANGED,
                        node_id,
                        name,
                        old=of.link_ref(),
                        new=nf.link_ref(),
                    )
                )
                continue
            if of.value != nf.value:
                entries.append(
                    DiffEntry(DIFF_VALUE_CHANGED, node_id, name, old=of.value, new=nf.value)
                )

    return entries


def has_breaking_change(entries: List[DiffEntry]) -> bool:
    return any(e.breaking for e in entries)


def summarize_diff(entries: List[DiffEntry]) -> str:
    """把差异渲染成中文清单，直接进 report / 探查节点输出。"""
    if not entries:
        return "两份快照完全一致，无结构变化"
    lines = [f"共 {len(entries)} 处变化："]
    lines.extend("  · " + e.describe() for e in entries)
    if has_breaking_change(entries):
        lines.append(
            "存在会导致绑定失效的变化，已固化的 nodeId/fieldName 映射需要重新解析"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 内部实现
# --------------------------------------------------------------------------


def _sorted_ids(ids: Any) -> List[str]:
    """节点 ID 的稳定排序（数字 ID 按数值，其余按字典序），保证输出可复现。"""
    return sorted((str(i) for i in ids), key=_workflow._node_sort_key)


def _classify_value(value: Any) -> Tuple[str, Any, Optional[str], Optional[int]]:
    """判定字段形态，返回 (value_kind, 原值, 连线源, 输出槽)。

    判定依据只有一条平台规则：API-format 里 `[]` 包裹的值表示连线。因此
    `[源节点, 槽位]` 形态即连线，其余标量即字面量，看不懂的一律归为 unknown
    （宁可不敢注入，也不要猜错）。
    """
    if isinstance(value, (list, tuple)):
        first = value[0] if value else None
        if (
            0 < len(value) <= 2
            and isinstance(first, (str, int))
            and not isinstance(first, bool)
        ):
            src = str(first)
            slot: Optional[int] = None
            if len(value) > 1 and isinstance(value[1], int) and not isinstance(value[1], bool):
                slot = int(value[1])
            return VALUE_LINK, None, src, slot
        return VALUE_UNKNOWN, value, None, None
    if isinstance(value, dict):
        return VALUE_UNKNOWN, value, None, None
    return VALUE_LITERAL, value, None, None


def _exec_state_of(meta: Dict[str, Any]) -> Tuple[str, str, Dict[str, Any]]:
    """判定节点执行态，返回 (状态, 判定依据, 原始证据)。

    平台没有统一暴露执行态，因此按「证据强度」依次看：ComfyUI 的 `mode` 常量、
    再到 bypass / mute 类布尔字段。都没有时按「会执行」处理——API-format 通常
    已剔除被静音的节点，但依据记为 default，让下游知道这是假设而非平台明示。
    """
    if not isinstance(meta, dict) or not meta:
        return EXEC_ACTIVE, "default", {}

    if "mode" in meta:
        mode = meta["mode"]
        state = _mode_to_state(mode)
        if state is not None:
            return state, "mode", {"mode": mode}
        # 出现了 mode 但取值不认识：保留证据，不猜状态
        return EXEC_ACTIVE, "default", {"mode": mode}

    for key in _MUTE_FLAGS:
        if key in meta and _truthy(meta[key]):
            return EXEC_MUTED, "flag", {key: meta[key]}
    for key in _BYPASS_FLAGS:
        if key in meta and _truthy(meta[key]):
            return EXEC_BYPASSED, "flag", {key: meta[key]}
    return EXEC_ACTIVE, "default", {}


def _mode_to_state(mode: Any) -> Optional[str]:
    if isinstance(mode, bool):
        return None
    if isinstance(mode, int):
        if mode in _MODE_MUTED:
            return EXEC_MUTED
        if mode in _MODE_BYPASSED:
            return EXEC_BYPASSED
        if mode in _MODE_ACTIVE:
            return EXEC_ACTIVE
        return None
    if isinstance(mode, str):
        text = mode.strip().lower()
        if text.isdigit():
            return _mode_to_state(int(text))
        if text in ("bypass", "bypassed"):
            return EXEC_BYPASSED
        if text in ("mute", "muted", "never", "disabled"):
            return EXEC_MUTED
        if text in ("always", "active", "on_event", "on_trigger", "ontrigger"):
            return EXEC_ACTIVE
    return None


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in _FALSE_WORDS
    return bool(value)


def _payload(snapshot: WorkflowSnapshot, with_values: bool) -> Dict[str, Any]:
    """指纹输入：只含结构事实，键序固定，因此同一份结构永远得到同一个指纹。"""
    nodes: Dict[str, Any] = {}
    for node_id in _sorted_ids(snapshot.nodes):
        node = snapshot.nodes[node_id]
        fields: Dict[str, Any] = {}
        for name in sorted(node.fields):
            f = node.fields[name]
            if f.is_link:
                fields[name] = [f.value_kind, f.link_src, f.link_slot]
            else:
                fields[name] = [f.value_kind, f.value if with_values else None]
        nodes[node_id] = {
            "class_type": node.class_type,
            "title": node.title,
            "exec_state": node.exec_state,
            "fields": fields,
        }
    return {"workflow_id": snapshot.workflow_id, "nodes": nodes}


def _canonical(payload: Dict[str, Any]) -> str:
    return json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = [
    "VALUE_LITERAL",
    "VALUE_LINK",
    "VALUE_UNKNOWN",
    "EXEC_ACTIVE",
    "EXEC_BYPASSED",
    "EXEC_MUTED",
    "FieldSnapshot",
    "NodeSnapshot",
    "Edge",
    "WorkflowSnapshot",
    "DiffEntry",
    "build_snapshot",
    "snapshot_from_structure",
    "diff_snapshots",
    "has_breaking_change",
    "summarize_diff",
]
