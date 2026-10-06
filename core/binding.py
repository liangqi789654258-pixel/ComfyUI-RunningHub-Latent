"""
S3 · R1 显式绑定（Binding）+ 绑定漂移检测（Drift Guard）

## 这一层负责什么

S2 的 Snapshot 回答了「这个世界里都有什么」：哪些字段是字面量、哪些节点会被执行、
整张图的指纹是什么。但它不知道「用户的 positive 提示词该送到哪个节点」——这个
映射关系**只能由人指定**，于是本层提供 Binding：

    SemanticPort（positive / seed / ...）  →  StructuralSlot（nodeId + fieldName）

SemanticPort 只是用户自取的字符串键；StructuralSlot 是平台唯一理解的地址。
两者之间的对应关系，本层不猜。

## 三条设计红线

1. **零推断。**
   不看 `class_type` 名字去判断谁是采样器、不沿连线回溯、不做「邻接 / 唯一 /
   距离」任何拓扑假设、找不到时**不回退到任何默认值**。`resolve_port()` 是纯查表，
   查不到就是查不到。
   这不是偷懒，是 S0 那九个形态已经证明过的事：任何靠形状猜的实现，都会在
   透传节点、分支旁路、多源汇聚、多采样器这些第三方工作流的常态形态上集体失效。

2. **不变量 A / B 在这里就是出口校验。**
   S6 会做全链路统一校验，但本层作为绑定的唯一生产者必须先立规矩——往 link 字段、
   往不会执行的节点绑定，在 `validate_binding()` 就会被拒。出口收不住，全线失守。

3. **不静默。**
   每个端口都必须有明确结局：命中并注入 / 带着中文原因报错。
   特别地，「用户想注入某个端口但该端口没绑定」这类情况**必须报错**——
   这正是 S1 之前那个 Bug 的成因（0 命中还一路绿灯跑完）。

## 漂移检测为什么放在这一层

绑定的地址是 `nodeId + fieldName`，而云端工作流随时会被作者编辑。昨天生效的映射，
今天可能指向已被删除的节点——那还算好的，平台会返回 **803 硬失败**让你知道。
更糟的情况是它指向仍然存在、但已被上游连线接管的字段：提交不报错，注入不生效。
`detect_drift()` 在提交前用连续两次快照的 diff 把这件事提前说清楚。

## C 级未知项的处置

「向被连线接管的字段注入，平台到底会怎样」目前仍是未知（S1 事实 4）。
本层不依赖任何未经验证的猜测，一律按最严格处理：link 字段不可绑定。
未知不代表可以放行，未知只代表不能赌。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .snapshot import (
    EXEC_ACTIVE,
    VALUE_LINK,
    VALUE_LITERAL,
    VALUE_UNKNOWN,
    DiffEntry,
    FieldSnapshot,
    NodeSnapshot,
    WorkflowSnapshot,
    diff_snapshots,
)

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

BINDING_SCHEMA_VERSION = 1

# 常用语义端口名——**仅是文档级约定**。
# 本模块把这些完全当作普通字符串键处理，源码里不存在任何以端口名为条件的
# 分支判断（tests/test_binding.py 的「禁止项源码自查」会逐行验证这一点）。
PORT_POSITIVE = "positive"
PORT_NEGATIVE = "negative"
PORT_SEED = "seed"
PORT_STEPS = "steps"
PORT_CFG = "cfg"
PORT_WIDTH = "width"
PORT_HEIGHT = "height"

LEVEL_ERROR = "error"   # 阻断注入
LEVEL_WARN = "warn"     # 仍可注入，但必须把依据摆给用户看

CODE_NODE_MISSING = "NODE_MISSING"
CODE_FIELD_MISSING = "FIELD_MISSING"
CODE_FIELD_LINKED = "FIELD_LINKED"
CODE_FIELD_UNKNOWN = "FIELD_UNKNOWN_KIND"
CODE_NODE_NOT_EXECUTING = "NODE_NOT_EXECUTING"
CODE_EXEC_ASSUMED = "EXEC_EVIDENCE_ASSUMED"
CODE_NO_BINDING = "NO_BINDING_FOR_PORT"
CODE_DUPLICATE_SLOT = "DUPLICATE_SLOT"
CODE_PORT_NOT_LOCKED = "PORT_NOT_IN_LOCKFILE"

_CODE_LABELS = {
    CODE_NODE_MISSING: "nodeId 不存在",
    CODE_FIELD_MISSING: "字段名不存在",
    CODE_FIELD_LINKED: "字段被连线接管",
    CODE_FIELD_UNKNOWN: "字段形态无法确认",
    CODE_NODE_NOT_EXECUTING: "节点不会被执行",
    CODE_EXEC_ASSUMED: "执行态只有默认依据",
    CODE_NO_BINDING: "该端口没有绑定",
    CODE_DUPLICATE_SLOT: "多个端口绑到同一槽位",
    CODE_PORT_NOT_LOCKED: "端口不在 lockfile 中",
}


# --------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------


@dataclass
class Binding:
    """一条显式绑定：语义端口 → 结构槽位。零推断，全部由调用方给定。"""

    node_id: str
    field_name: str
    port: str = ""
    note: str = ""
    topology: str = ""          # 建立绑定那一刻的拓扑指纹，便于单点溯源

    def slot(self) -> Tuple[str, str]:
        return (str(self.node_id), str(self.field_name))

    def identity(self) -> str:
        """人读/日志用的稳定标识。"""
        return f"{self.port or '?'} → nodeId={self.node_id} fieldName={self.field_name}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": str(self.node_id),
            "field_name": str(self.field_name),
            "port": self.port,
            "note": self.note,
            "topology": self.topology,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Binding":
        return cls(
            node_id=str(data.get("node_id", "")),
            field_name=str(data.get("field_name", "")),
            port=str(data.get("port", "")),
            note=str(data.get("note", "")),
            topology=str(data.get("topology", "")),
        )


@dataclass
class BindingProblem:
    """一处绑定问题。level 决定它是否阻断注入。"""

    level: str
    code: str
    message: str
    hint: str = ""
    node_id: str = ""
    field_name: str = ""
    port: str = ""

    @property
    def is_error(self) -> bool:
        return self.level == LEVEL_ERROR

    def describe(self) -> str:
        tag = "拒绝" if self.is_error else "提示"
        head = f"[{tag}] {_CODE_LABELS.get(self.code, self.code)}"
        body = self.message
        tail = f"（建议：{self.hint}）" if self.hint else ""
        return f"{head}：{body}{tail}"


@dataclass
class BindingSet:
    """某个工作流下的全量绑定，可落盘为 lockfile 供团队共享。"""

    workflow_id: str = ""
    bindings: List[Binding] = field(default_factory=list)
    topology: str = ""      # 固化时的拓扑指纹（漂移检测基准）
    fingerprint: str = ""   # 固化时的全量指纹（含默认值，用于判断是否"完全相同"）
    note: str = ""

    def ports(self) -> List[str]:
        out: List[str] = []
        for b in self.bindings:
            if b.port and b.port not in out:
                out.append(b.port)
        return out

    def get(self, port: str) -> List[Binding]:
        """取某个端口的全部绑定。**纯查表**，一个端口允许绑定多个槽位（多注入）。"""
        return [b for b in self.bindings if b.port == port]

    def has(self, port: str) -> bool:
        return any(b.port == port for b in self.bindings)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": BINDING_SCHEMA_VERSION,
            "workflow_id": self.workflow_id,
            "topology": self.topology,
            "fingerprint": self.fingerprint,
            "note": self.note,
            "bindings": [b.to_dict() for b in self.bindings],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BindingSet":
        version = int(data.get("schema_version", 0))
        if version != BINDING_SCHEMA_VERSION:
            raise ValueError(
                f"lockfile 版本不匹配：文件为 v{version}，当前实现为 "
                f"v{BINDING_SCHEMA_VERSION}，请重新导出绑定"
            )
        return cls(
            workflow_id=str(data.get("workflow_id", "")),
            bindings=[Binding.from_dict(b) for b in (data.get("bindings") or [])],
            topology=str(data.get("topology", "")),
            fingerprint=str(data.get("fingerprint", "")),
            note=str(data.get("note", "")),
        )

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_json(cls, text: str) -> "BindingSet":
        return cls.from_dict(json.loads(text))


def save_lockfile(binding_set: BindingSet, path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(binding_set.to_json())


def load_lockfile(path: str) -> BindingSet:
    with open(path, "r", encoding="utf-8") as fh:
        return BindingSet.from_json(fh.read())


def lock_bindings(snapshot: WorkflowSnapshot,
                  bindings: Sequence[Binding],
                  workflow_id: str = "",
                  note: str = "") -> BindingSet:
    """把一批绑定固化成 lockfile，并记录当时的指纹作为漂移基准。"""
    return BindingSet(
        workflow_id=workflow_id or snapshot.workflow_id,
        bindings=[Binding(node_id=str(b.node_id), field_name=str(b.field_name),
                          port=b.port, note=b.note,
                          topology=snapshot.topology_fingerprint())
                  for b in bindings],
        topology=snapshot.topology_fingerprint(),
        fingerprint=snapshot.fingerprint(),
        note=note,
    )


# --------------------------------------------------------------------------
# 校验：不变量 A / 不变量 B / 存在性
# --------------------------------------------------------------------------


def validate_binding(snapshot: WorkflowSnapshot, binding: Binding) -> List[BindingProblem]:
    """校验单条绑定是否成立。返回空列表 = 可以安全注入。

    判定顺序刻意按「越靠前的错误越致命」排列，一条失败就直接返回，
    避免给用户一次抛七八条噪音。
    """
    node = snapshot.node(binding.node_id)
    if node is None:
        candidates = _node_preview(snapshot)
        return [BindingProblem(
            LEVEL_ERROR, CODE_NODE_MISSING,
            f"工作流里没有 nodeId={binding.node_id}"
            f"（端口 {binding.port or '未命名'}）。"
            f"平台上存在的节点：{candidates}",
            hint="用「RH 工作流参数探查」重新导出可注入槽位清单，再挑一个 nodeId",
            node_id=str(binding.node_id), field_name=str(binding.field_name),
            port=binding.port,
        )]

    if not node.is_executing:
        return [BindingProblem(
            LEVEL_ERROR, CODE_NODE_NOT_EXECUTING,
            f"nodeId={binding.node_id} 处于「{node.exec_state}」状态，不会被实际执行，"
            f"打在它上面的注入不会生效（违反不变量 B）",
            hint="改绑到一个会执行的节点，或先把该节点的 bypass/mute 取消",
            node_id=str(binding.node_id), field_name=str(binding.field_name),
            port=binding.port,
        )]

    fs = node.fields.get(str(binding.field_name))
    if fs is None:
        available = ", ".join(sorted(node.fields)) or "（该节点没有输入字段）"
        return [BindingProblem(
            LEVEL_ERROR, CODE_FIELD_MISSING,
            f"nodeId={binding.node_id}（{node.class_type or '?'}）没有字段 "
            f"「{binding.field_name}」。该节点可用字段：{available}",
            hint="字段名要严格按 API-format 拼写，可在探查清单里复制",
            node_id=str(binding.node_id), field_name=str(binding.field_name),
            port=binding.port,
        )]

    if fs.is_link:
        src = fs.link_ref()
        return [BindingProblem(
            LEVEL_ERROR, CODE_FIELD_LINKED,
            f"nodeId={binding.node_id} 的「{binding.field_name}」已被连线接管"
            f"（上游 nodeId={fs.link_src} 输出槽 {fs.link_slot}），"
            f"平台明确不建议修改，注入后是否生效无法保证（违反不变量 A）",
            hint="改绑到连线上游那个真正持有字面量的节点"
                 + (f"，即 nodeId={src[0]}" if src else ""),
            node_id=str(binding.node_id), field_name=str(binding.field_name),
            port=binding.port,
        )]

    if fs.value_kind == VALUE_UNKNOWN:
        return [BindingProblem(
            LEVEL_ERROR, CODE_FIELD_UNKNOWN,
            f"nodeId={binding.node_id} 的「{binding.field_name}」形态无法归类"
            f"（既不是字面量也不是连线），按最严格规则不允许注入",
            hint="只在探查清单里标注为 literal 的字段上绑定",
            node_id=str(binding.node_id), field_name=str(binding.field_name),
            port=binding.port,
        )]

    problems: List[BindingProblem] = []
    if node.exec_source == "default":
        # 这不是平台明示的事实，而是「没有反证就当会执行」的假设。
        # 允许注入，但必须让用户知道依据是假设而非证据——S6 可据此要求显式确认。
        problems.append(BindingProblem(
            LEVEL_WARN, CODE_EXEC_ASSUMED,
            f"nodeId={binding.node_id} 的结构里没有 mode/bypass/mute 证据，"
            f"「会执行」是按默认假设认定的，并非平台明示",
            hint="若该节点实际被云端判定为不执行，注入会无声失效；确认后即可忽略本提示",
            node_id=str(binding.node_id), field_name=str(binding.field_name),
            port=binding.port,
        ))
    return problems


def validate_bindings(snapshot: WorkflowSnapshot,
                      bindings: Sequence[Binding]) -> List[BindingProblem]:
    """批量校验，额外检查「两个端口抢同一个槽位」这类跨绑定冲突。

    同一 (nodeId, fieldName) 被两个端口绑定时，平台侧会收到两条同址注册，
    行为未知（S0 的 c7 形态已实证过这条路径会出问题）。这里直接判错，
    不做「取第一个」或「取最后一个」的静默裁决。
    """
    problems: List[BindingProblem] = []
    owner: Dict[Tuple[str, str], str] = {}

    for b in bindings:
        problems.extend(validate_binding(snapshot, b))
        key = b.slot()
        prev = owner.get(key)
        if prev is not None and prev != b.port:
            problems.append(BindingProblem(
                LEVEL_ERROR, CODE_DUPLICATE_SLOT,
                f"端口「{prev}」与「{b.port or '未命名'}」都绑定到 "
                f"nodeId={key[0]} fieldName={key[1]}，一个槽位只能归属一个端口",
                hint="删掉其中一个绑定，或确认它们应当是两个不同的槽位",
                node_id=key[0], field_name=key[1], port=b.port,
            ))
        else:
            owner[key] = b.port or ""
    return problems


def split_problems(problems: Sequence[BindingProblem]
                   ) -> Tuple[List[BindingProblem], List[BindingProblem]]:
    """拆成 (阻断级, 提示级)。"""
    errors = [p for p in problems if p.is_error]
    warns = [p for p in problems if not p.is_error]
    return errors, warns


def describe_problems(problems: Sequence[BindingProblem]) -> str:
    if not problems:
        return "全部绑定校验通过"
    lines: List[str] = []
    for p in problems:
        lines.append("  · " + p.describe())
    errors, warns = split_problems(problems)
    lines.append(f"共 {len(errors)} 处阻断 / {len(warns)} 处提示")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# R1 显式绑定解析 + 注入构造
# --------------------------------------------------------------------------


def resolve_port(binding_set: BindingSet, port: str) -> List[Binding]:
    """R1 解析器：按端口名取绑定。**纯查表**，没有任何推断。

    查不到就返回空列表，由调用方决定是否报错——本函数不做任何降级。
    """
    return binding_set.get(port)


def build_injections(
    snapshot: WorkflowSnapshot,
    binding_set: BindingSet,
    values: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[BindingProblem]]:
    """构造提交给平台的 `nodeInfoList` 项。

    返回 `(注入列表, 问题列表)`。**只要出现阻断级问题，注入列表必为空**——
    宁可整单失败，也不做「部分注入」这种最容易变成静默失效的妥协。

    `values` 是 `{端口名: 值}`。未提供值的端口不产生注入，也不算错误
    （用户可能只想覆盖 positive、保留云端原有的 negative）。
    但反过来——提供了值却没有绑定的端口**必须报错**。
    """
    problems: List[BindingProblem] = []

    # 方向一：用户要传值，却没有任何绑定 → 这正是历史上那个静默失效 Bug
    for port in values:
        if not binding_set.has(port):
            problems.append(BindingProblem(
                LEVEL_ERROR, CODE_NO_BINDING,
                f"端口「{port}」有值要注入，但当前工作流没有它的绑定，"
                f"本次不会向云端提交该参数",
                hint="先为该工作流建立显式绑定（手写映射或从探查清单里挑）",
                port=str(port),
            ))

    # 方向二：逐条绑定做出口校验 + 跨绑定冲突检查
    active: List[Binding] = []
    for b in binding_set.bindings:
        if b.port in values:
            active.append(b)
    problems.extend(validate_bindings(snapshot, active))

    errors, _warns = split_problems(problems)
    if errors:
        return [], problems

    injections: List[Dict[str, Any]] = [
        {
            "nodeId": str(b.node_id),
            "fieldName": str(b.field_name),
            "fieldValue": values[b.port],
        }
        for b in active
    ]
    return injections, problems


# --------------------------------------------------------------------------
# 漂移检测
# --------------------------------------------------------------------------


@dataclass
class BindingDrift:
    """某条绑定失效了，附带导致它失效的具体变更。"""

    binding: Binding
    reasons: List[DiffEntry] = field(default_factory=list)

    @property
    def port(self) -> str:
        return self.binding.port

    def describe(self) -> str:
        head = f"端口「{self.binding.port or '未命名'}」的绑定已失效：" \
               f"nodeId={self.binding.node_id} fieldName={self.binding.field_name}"
        for entry in self.reasons:
            head += "\n      - " + entry.describe()
        return head


@dataclass
class DriftReport:
    """漂移检测报告。"""

    workflow_id: str = ""
    entries: List[DiffEntry] = field(default_factory=list)
    broken: List[BindingDrift] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def has_drift(self) -> bool:
        return bool(self.broken)

    def broken_ports(self) -> List[str]:
        return [d.port for d in self.broken]

    def describe(self) -> str:
        from .snapshot import summarize_diff

        if not self.has_drift:
            base = "未检测到绑定漂移，全部绑定仍然成立"
            if self.entries:
                base += "（期间工作流有变化，但不影响已绑定的槽位）"
            if self.notes:
                base += "\n  " + "\n  ".join(self.notes)
            return base
        lines = [f"检测到 {len(self.broken)} 条绑定失效："]
        lines.extend("  · " + d.describe() for d in self.broken)
        lines.append(summarize_diff(self.entries))
        if self.notes:
            lines.extend("  · " + n for n in self.notes)
        lines.append("绑定失效后继续提交，可能命中 803 硬失败，或静默落到云端默认值")
        return "\n".join(lines)


def detect_drift(old: WorkflowSnapshot,
                 new: WorkflowSnapshot,
                 binding_set: BindingSet) -> DriftReport:
    """比对两次快照，找出因结构变更而失效的绑定。

    **只关心会让特定绑定失效的变化。** 全图 `has_breaking_change` 太粗——
    作者在别处删了个节点不影响你的绑定，改了某个默认值更不该逼你重新配置。
    所以这里逐绑定筛选：只看 DiffEntry 中同时满足
    「与这条绑定的 nodeId 相关」且「被标记为 breaking」的那些。

    不触发漂移的典型情况（刻意放行的噪音）：
      * 只改某个字段的默认值 → 绑定地址没变，字段仍是可注入字面量；
      * 节点顺序重排 → 不影响 nodeId；
      * 新增了别的节点 → 老槽位照样成立。
    """
    entries = diff_snapshots(old, new)
    report = DriftReport(workflow_id=binding_set.workflow_id or new.workflow_id,
                         entries=entries)

    if not entries:
        report.notes.append("两次快照完全一致")
        return report

    for b in binding_set.bindings:
        reasons = [e for e in entries if e.breaking and e.node_id == str(b.node_id)]
        if reasons:
            report.broken.append(BindingDrift(binding=b, reasons=reasons))

    if binding_set.topology and binding_set.topology == new.topology_fingerprint():
        report.notes.append(
            "lockfile 记录的拓扑指纹与当前一致：本次改动未触及结构，"
            "可视为安全变更"
        )
    elif binding_set.topology and not report.broken:
        report.notes.append(
            "拓扑指纹已变化，但没有变动任何已绑定的槽位——仍建议复核 diff"
        )
    return report


def check_fingerprint(snapshot: WorkflowSnapshot,
                      binding_set: BindingSet) -> List[BindingProblem]:
    """提交前的快速体检：当前结构相对 lockfile 是否还是当初那张图。

    不做 diff，只比指纹，因此开销极小、可以在每次运行时都做。
    指纹不一致时**不阻断**（走得下去也可能没问题），但一定提示。
    """
    if not binding_set.topology:
        return []
    current = snapshot.topology_fingerprint()
    if current == binding_set.topology:
        return []
    return [BindingProblem(
        LEVEL_WARN, CODE_PORT_NOT_LOCKED,
        f"云端工作流结构自绑定固化后已发生变化"
        f"（拓扑指纹 {binding_set.topology[:12]}… → {current[:12]}…），"
        f"已绑定的 nodeId/fieldName 可能不再成立",
        hint="跑一次 detect_drift() 确认是否影响已有绑定",
    )]


# --------------------------------------------------------------------------
# 槽位清单导出：给用户看，由他挑一个填进 lockfile
# --------------------------------------------------------------------------


def export_injectable_slots(snapshot: WorkflowSnapshot) -> List[Dict[str, Any]]:
    """导出全部可注入槽位（已满足不变量 A + B），供人工挑选建立绑定。

    每条记录包含：nodeId、fieldName、字段当前值、节点类型、节点标题、字段形态。
    这是给用户看清单用的，**不是**自动定位——本模块不替任何人做选择。
    """
    rows: List[Dict[str, Any]] = []
    for node_id, field_name in snapshot.injectable_slots():
        node = snapshot.node(node_id)
        fs = snapshot.slot(node_id, field_name)
        if node is None or fs is None:
            continue
        rows.append({
            "nodeId": str(node_id),
            "fieldName": str(field_name),
            "fieldValue": fs.value,
            "valueKind": fs.value_kind,
            "classType": node.class_type,
            "title": node.title,
            "execState": node.exec_state,
        })
    return rows


def format_slot_catalog(snapshot: WorkflowSnapshot, value_limit: int = 48) -> str:
    """把可注入槽位渲染成可直接阅读的中文清单。"""
    rows = export_injectable_slots(snapshot)
    if not rows:
        return "该工作流没有满足不变量 A + B 的可注入槽位" \
               "（所有字段要么被连线接管，要么所在节点不会执行）"

    lines = [f"可注入槽位 {len(rows)} 个（已排除连线字段与不执行节点）："]
    for row in rows:
        shown = str(row["fieldValue"])
        if len(shown) > value_limit:
            shown = shown[: value_limit - 3] + "..."
        label = f"nodeId={row['nodeId']}  fieldName={row['fieldName']}"
        kind = f"[{row['classType'] or '?'}"
        if row["title"]:
            kind += f" 「{row['title']}」"
        kind += "]"
        lines.append(f"  ● {label}   {kind}   当前值 = {shown}")
    lines.append(
        "把挑选好的 nodeId + fieldName 写进 lockfile，即为一条 R1 显式绑定。"
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 内部实现
# --------------------------------------------------------------------------


def _node_preview(snapshot: WorkflowSnapshot, limit: int = 8) -> str:
    """列几个存在的 nodeId 给用户，避免他面对一个"不存在"干瞪眼。"""
    ids = sorted((str(n) for n in snapshot.nodes), key=_sort_key)
    if not ids:
        return "（该工作流结构为空，可能是拉取失败）"
    shown = ", ".join(ids[:limit])
    if len(ids) > limit:
        shown += f" 等共 {len(ids)} 个"
    return shown


def _sort_key(node_id: str) -> Tuple[int, str]:
    """与 core.workflow._node_sort_key 同口径：数字 ID 按数值，其余按字典序。"""
    try:
        return (0, f"{int(node_id):012d}")
    except (TypeError, ValueError):
        return (1, str(node_id))


__all__ = [
    "BINDING_SCHEMA_VERSION",
    "Binding",
    "BindingProblem",
    "BindingSet",
    "BindingDrift",
    "DriftReport",
    "LEVEL_ERROR",
    "LEVEL_WARN",
    "validate_binding",
    "validate_bindings",
    "split_problems",
    "describe_problems",
    "resolve_port",
    "build_injections",
    "detect_drift",
    "check_fingerprint",
    "lock_bindings",
    "save_lockfile",
    "load_lockfile",
    "export_injectable_slots",
    "format_slot_catalog",
]
