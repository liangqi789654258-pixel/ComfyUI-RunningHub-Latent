"""
云端工作流结构解析 —— 极简版「零配置」的地基。

## 为什么需要这个模块

极简版要做到「只填密钥和工作流 ID 就能出图」，就必须在不让用户填 nodeId 的前提下，
自己找出该往哪些节点注入提示词和种子。办法是先从云端把工作流结构拉下来，再按语义找。

## 两条关键的查找策略

1. **提示词：不认节点类型，只认占位符文本**
   扫描工作流里**所有**字符串字段，凡是内容中出现 `{positive}` / `{negative}`
   就替换它。这样无论云端是单文本节点、双 CLIPTextEncode、还是把提示词塞在某个
   第三方节点的自定义字段里，都能命中。比「按 class_type == CLIPTextEncode 找」
   健壮得多——后者在遇到 SDXL / 第三方编码节点时就会漏。

   替换是**子串替换**而非整值替换，所以云端写成
   `masterpiece, {positive}, best quality` 也能正确保留前后缀。

   注意这里**不做任何拓扑猜测**：曾经有一套「沿采样器 positive/negative 连线
   向上游回溯找文本字段」的兜底，已推倒——图像大小设置和提示词应用每条工作流
   都不一样，猜出来的落点一旦错了就是静默失效。没有占位符 = 没有自动落点，
   由调用方报错并指引用户手写映射。

2. **种子：优先认采样器类型，找不到再认字段名**
   先找 class_type 里带 Sampler 的节点；一个都没有时，退化为「任何带 seed
   字段的节点」。两种都找不到就在 report 里明说，而不是悄悄跳过。

所有函数都是纯函数，不联网、不依赖 ComfyUI，可以直接离线测试。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# 占位符标记：允许大小写变体（{Positive} / {POSITIVE} 都认）
_TOKEN_PATTERNS = {
    "positive": re.compile(r"\{\s*positive\s*\}", re.IGNORECASE),
    "negative": re.compile(r"\{\s*negative\s*\}", re.IGNORECASE),
}

_SEED_FIELDS = ("seed", "noise_seed", "rand_seed")
_SAMPLER_HINTS = ("sampler",)

# 「看起来承载提示词」的字段名候选（各插件包命名不统一）。
# 只用于给用户推荐落点、以及生成探查模板，**不用于自动决定落点**。
_TEXT_FIELD_ORDER = (
    "text", "string", "value", "prompt", "str",
    "text_a", "text_b", "text_g", "text_l", "wildcard",
)

# 节点直属的「执行态」字段：ComfyUI 前端格式用 mode（0 执行 / 2 mute / 4 bypass），
# 部分导出格式直接用 bypass / muted 之类的布尔字段。这些是平台侧的通用字段，
# 不是某个工作流的私有字段，归一化时一并收进 meta，省得下游再去翻原始响应。
_NODE_STATE_KEYS = (
    "mode",
    "bypass", "bypassed", "is_bypassed", "isBypassed",
    "mute", "muted", "is_muted", "isMuted",
    "disabled", "is_disabled",
)


def normalize(data: Any) -> Dict[str, Dict[str, Any]]:
    """把各种形态的工作流响应归一化成：

        {nodeId(str): {"class_type": str, "inputs": {...}, "meta": {...}}}

    `meta` = 平台返回的 `_meta`（用户自定义节点标题就在 `_meta.title`）再合并
    节点直属的执行态字段（mode / bypass / mute …）。以前这一层被整个丢掉，
    等于把平台白送的语义线索扔了，S2 的 Snapshot 又必须要有它。

    兼容的输入形态（都是实际遇到过的）：
      * ComfyUI prompt 格式：{"3": {"class_type": "KSampler", "inputs": {...}}}
      * {"prompt": "{...json 字符串...}"} 或 {"prompt": {...}}
      * {"nodes": [{"id": 3, "type": "KSampler", "widgets_values": [...]}]}
      * {"workflow": {...}} 外层再包一层
    """
    if not data:
        return {}

    if isinstance(data, str):
        # 有些版本把整个工作流塞在一个 JSON 字符串里
        import json

        try:
            data = json.loads(data)
        except Exception:
            return {}

    if not isinstance(data, dict):
        return {}

    # 逐层拆包，直到找到真正的工作流主体
    for wrapper in ("prompt", "workflow", "data", "graph", "api", "json"):
        inner = data.get(wrapper)
        if isinstance(inner, (dict, str)) and wrapper in data:
            # 只有当前层看起来不像工作流主体时才下探
            if not _looks_like_prompt(data):
                nested = normalize(inner)
                if nested:
                    return nested

    # 形态 A：{nodeId: {...}}
    if _looks_like_prompt(data):
        out: Dict[str, Dict[str, Any]] = {}
        for key, meta in data.items():
            if not isinstance(meta, dict):
                continue
            class_type = str(meta.get("class_type") or meta.get("type") or "")
            inputs = meta.get("inputs")
            if not isinstance(inputs, dict):
                inputs = {}
            out[str(key)] = {
                "class_type": class_type,
                "inputs": inputs,
                "meta": _collect_meta(meta),
            }
        return out

    # 形态 B：{"nodes": [...]}
    nodes = data.get("nodes")
    if isinstance(nodes, list):
        out = {}
        for node in nodes:
            if not isinstance(node, dict):
                continue
            node_id = node.get("id", node.get("nodeId", node.get("node_id")))
            if node_id is None:
                continue
            class_type = str(node.get("type") or node.get("class_type") or "")
            inputs = node.get("inputs")
            if not isinstance(inputs, dict):
                inputs = {}
            # 部分导出格式把参数放在 widgets_values 里，字段名需要从
            # widgets 描述里取，这里做不到可靠映射，因此只保留能识别的 inputs
            out[str(node_id)] = {
                "class_type": class_type,
                "inputs": inputs,
                "meta": _collect_meta(node),
            }
        return out

    return {}


def _collect_meta(node: Dict[str, Any]) -> Dict[str, Any]:
    """汇总节点的元信息：平台 `_meta` + 节点直属的执行态字段。"""
    raw = node.get("_meta")
    meta: Dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    for key in _NODE_STATE_KEYS:
        if key in node and key not in meta:
            meta[key] = node[key]
    return meta


def _looks_like_prompt(data: Dict[str, Any]) -> bool:
    """判断 dict 是否已经是 {nodeId: {class_type, inputs}} 形态。"""
    for value in data.values():
        if isinstance(value, dict) and ("class_type" in value or "inputs" in value):
            return True
    return False


def iter_string_fields(
    structure: Dict[str, Dict[str, Any]]
) -> List[Tuple[str, str, str]]:
    """列出工作流里所有「字符串类型的输入字段」。

    返回 [(nodeId, fieldName, 原值)]，已剔除连线引用（list 形态的值）。
    """
    slots: List[Tuple[str, str, str]] = []
    for node_id, meta in sorted(structure.items(), key=lambda kv: _node_sort_key(kv[0])):
        inputs = meta.get("inputs") or {}
        if not isinstance(inputs, dict):
            continue
        for field, value in inputs.items():
            if isinstance(value, str):
                slots.append((node_id, str(field), value))
    return slots


def _node_sort_key(node_id: str) -> Tuple[int, str]:
    """数字 ID 按数值排，非数字按字典序，保证输出稳定可复现。"""
    try:
        return (0, f"{int(node_id):012d}")
    except (TypeError, ValueError):
        return (1, str(node_id))


def _is_link(value: Any) -> bool:
    """判断字段值是否为连线引用（形如 [源节点Id, 输出槽序号]）。"""
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], (str, int))
    )


def _node_label(structure: Dict[str, Dict[str, Any]], node_id: str) -> str:
    """把节点渲染成「能认出来是哪个」的标签。

    带上用户在画布上起的自定标题（_meta.title，normalize 已保留）——
    「正面提示词」「Positive Prompt」这类标题是判断落点最直接的线索，
    比只看 class_type 强得多。只用于呈现，不用于任何自动判断。
    """
    meta = structure.get(str(node_id)) or {}
    class_type = str(meta.get("class_type") or "?")
    title = str((meta.get("meta") or {}).get("title") or "")
    label = f"nodeId={node_id} [{class_type}]"
    if title and title != class_type:
        label += f" 「{title}」"
    return label


def _literal_text_slots(
    structure: Dict[str, Dict[str, Any]]
) -> List[Tuple[str, str]]:
    """列出「可能承载提示词」的**字面量**字段，供诊断给用户推荐落点。

    优先取字段名像文本的（text / value / string …），一个都没有时才退回
    「任意字符串字段」。只用于**建议**，不用于自动决定落点。
    """
    preferred: List[Tuple[str, str]] = []
    fallback: List[Tuple[str, str]] = []
    for node_id, field, _value in iter_string_fields(structure):
        if str(field).lower() in _TEXT_FIELD_ORDER:
            preferred.append((node_id, field))
        else:
            fallback.append((node_id, field))
    if preferred:
        # 字段名越像「提示词」排越前：text 优先于 text_a / wildcard。
        # 只是排序，不筛掉任何一个候选——筛就是替用户猜，不筛才是把信息摆全。
        order = {name: i for i, name in enumerate(_TEXT_FIELD_ORDER)}
        preferred.sort(
            key=lambda nf: (order.get(str(nf[1]).lower(), 99), _node_sort_key(nf[0]))
        )
        return preferred
    return fallback


def _multi_field_note(slots: List[Tuple[str, str]]) -> str:
    """同一节点有多个文本字段时，明确告诉用户「机器无法判断该注入哪一个」。"""
    grouped: Dict[str, List[str]] = {}
    for node_id, field in slots:
        grouped.setdefault(node_id, []).append(field)
    for node_id in sorted(grouped, key=_node_sort_key):
        fields = grouped[node_id]
        if len(fields) > 1:
            return (
                f"（注意：nodeId={node_id} 有多个文本字段（{'、'.join(sorted(fields))}），"
                "无法判断该注入哪一个，请按语义自行选择。）"
            )
    return ""


def suggest_text_targets(
    structure: Dict[str, Dict[str, Any]]
) -> List[Tuple[str, str]]:
    """公开入口：列出云端所有可写入的字面量文本字段。

    报错文案要靠它生成**能直接照抄**的映射片段——早先文案里硬编码
    `"fieldName": "text"`，而真实字段名可能是 `prompt` / `value` / `string`，
    用户照抄过去又被 803 打回，等于报错本身在误导人。
    """
    return _literal_text_slots(structure)


def prompt_link_diagnosis(structure: Dict[str, Dict[str, Any]], token: str) -> str:
    """说明「为什么没有自动落点」，并给出可直接照抄的手写映射修法。

    契约变更（原「自动匹配提示词」已推倒）：本插件不再沿连线猜测提示词的落点。
    原因是图像大小设置和提示词的接法每条工作流都不一样，猜出来的落点一旦错了
    就是**静默失效**——用户以为生效了，其实根本没送上去，比报错更糟。

    因此这里不再解释「猜测为什么失败」，而是回答用户真正要问的三件事：
    云端结构里有没有可写的地方、该往哪写、怎么写。返回字符串（调用方会把它
    拼进 warning / 报错文案），签名与返回类型保持不变。
    """
    if not structure:
        return (
            "云端工作流结构读取失败（getJsonApiFormat 没有返回可用节点），"
            "无从推荐落点。请确认工作流 ID 正确且已保存，"
            "再用「RH 工作流参数探查」拉一次清单后手写参数映射。"
        )

    slots = _literal_text_slots(structure)
    if not slots:
        return (
            f"云端结构共读到 {len(structure)} 个节点，但没有可写入的字面量文本字段"
            "（文本字段都已被上游连线接管）。本节点不做拓扑猜测，"
            "请在参数映射里手工指定 nodeId / fieldName。"
        )

    shown = "、".join(f"{_node_label(structure, n)}.{f}" for n, f in slots[:6])
    tail = "" if len(slots) <= 6 else f"（另有 {len(slots) - 6} 个未列出）"
    return (
        f"云端结构共读到 {len(structure)} 个节点，但没有一处 {{{token}}} 占位符，"
        f"本节点不会沿连线猜测落点。请在参数映射里手工指定：{shown}{tail}。"
        f"{_multi_field_note(slots)}"
        "（以上是云端所有可写入的文本字段候选；请按节点标题和语义自行判断"
        "哪一个是提示词落点，选错会覆盖不该覆盖的内容，"
        "例如被多个分支共用的 lora 触发词节点。）"
        f'复制后把 nodeId 换成你自己的：{{"nodeId": "<ID>", "fieldName": "text", '
        f'"fieldValue": "{{{token}}}"}}；'
        "也可以直接在云端文本节点的文本框里写上 "
        f"{{{token}}} 标记，本节点会自动替换。"
    )


def build_prompt_injections(
    structure: Dict[str, Dict[str, Any]], positive: str, negative: str
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """按占位符生成提示词注入项（**只认占位符，不做任何拓扑猜测**）。

    返回 (注入列表, {"positive": 命中次数, "negative": 命中次数})。

    调用方必须检查命中次数：为 0 意味着云端工作流里根本没有占位符，
    用户填的提示词不会生效——这种情况必须由调用方**报错**，绝不能静默跳过。

    契约：{positive} / {negative} 是工作流作者写进云端文本里的**显式声明**，
    本函数只做子串替换。曾经存在的「沿采样器连线兜底回溯」已被推倒——
    每条工作流的提示词接法都不一样（有的接字符串拼接、有的接通配符、
    有的接透传节点），按拓扑猜出来的落点错了就是静默失效。
    """
    injections: List[Dict[str, Any]] = []
    stats = {"positive": 0, "negative": 0}
    values = {"positive": positive or "", "negative": negative or ""}

    for node_id, field, text in iter_string_fields(structure):
        new_text = text
        for token, pattern in _TOKEN_PATTERNS.items():
            found = pattern.findall(new_text)
            if found:
                stats[token] += len(found)
                new_text = pattern.sub(values[token], new_text)
        if new_text != text:
            injections.append(
                {"nodeId": node_id, "fieldName": field, "fieldValue": new_text}
            )

    return injections, stats


def placeholder_hits(structure: Dict[str, Dict[str, Any]]) -> Dict[str, int]:
    """统计云端工作流里 {positive}/{negative} 标记的出现次数（与是否注入无关）。

    用来识别一个很容易被忽略的静默场景：云端写了占位符，但本地提示词是
    空字符串（比如上游文本处理节点没输出内容、或输出口接错），此时云端
    提示词会被替换成空内容，画面完全不对，却没有任何报错。
    """
    counts = {"positive": 0, "negative": 0}
    for _node_id, _field, text in iter_string_fields(structure):
        for token, pattern in _TOKEN_PATTERNS.items():
            counts[token] += len(pattern.findall(text))
    return counts


def find_seed_targets(structure: Dict[str, Dict[str, Any]]) -> List[Tuple[str, str]]:
    """找出应该注入随机种子的 (nodeId, fieldName)。

    优先采样器类节点；找不到采样器时退化为任何带 seed 字段的节点。
    """
    sampler_slots: List[Tuple[str, str]] = []
    fallback_slots: List[Tuple[str, str]] = []

    for node_id, meta in sorted(structure.items(), key=lambda kv: _node_sort_key(kv[0])):
        class_type = str(meta.get("class_type", "")).lower()
        inputs = meta.get("inputs") or {}
        if not isinstance(inputs, dict):
            continue
        for field in _SEED_FIELDS:
            if field in inputs:
                slot = (node_id, field)
                if any(hint in class_type for hint in _SAMPLER_HINTS):
                    sampler_slots.append(slot)
                else:
                    fallback_slots.append(slot)
                break

    return sampler_slots or fallback_slots


def _diagnose_injection(
    structure: Dict[str, Dict[str, Any]], item: Dict[str, Any]
) -> Optional[str]:
    """校验单条映射，返回中文问题；合法则返回 None。"""
    node_id = str(item.get("nodeId", ""))
    field = str(item.get("fieldName", ""))
    meta = structure.get(node_id)
    if meta is None:
        return (
            f"nodeId「{node_id}」在云端工作流里不存在"
            "（换工作流后最容易踩这个坑，可用「RH 工作流参数探查」重新拉清单）"
        )
    inputs = meta.get("inputs") or {}
    if not isinstance(inputs, dict) or field not in inputs:
        available = ", ".join(sorted(inputs.keys())) if inputs else "（该节点无可注入字段）"
        return f"nodeId「{node_id}」没有字段「{field}」，该节点可用字段：{available}"
    # 不变量 A：不得向连线字段注入。字段值是 [源节点, 槽位] 说明该输入已被
    # 上游连线接管，平台不建议修改，写进去不报错也不会生效——最坏的静默失效。
    if _is_link(inputs.get(field)):
        src = str(inputs[field][0]) if inputs[field] else "?"
        return (
            f"nodeId「{node_id}」的字段「{field}」已被上游连线接管"
            f"（当前值是连线引用，来源 nodeId={src}），"
            "平台不建议修改连线字段，注入不会生效。"
        )
    return None


def diagnose_injections(
    structure: Dict[str, Dict[str, Any]], injections: List[Dict[str, Any]]
) -> List[Optional[str]]:
    """逐条校验，返回与 `injections` **一一对应**的问题列表（None = 该条合法）。

    一一对应是给「803 逐条定位」用的：平台整包拒绝时，调用方需要知道
    **第几条**错了，而不是拿到一堆无从下手的字符串。
    结构不可得时全部返回 None——无法校验就不下结论。
    """
    if not structure:
        return [None] * len(injections)
    return [_diagnose_injection(structure, item) for item in injections]


def validate_injections(
    structure: Dict[str, Dict[str, Any]], injections: List[Dict[str, Any]]
) -> List[str]:
    """校验用户手写的参数映射，返回中文问题列表（空列表 = 全部合法）。

    结构不可得时返回空列表——此时无法校验，不能拿不确定的信息去吓唬用户。
    """
    if not structure:
        return []
    return [p for p in diagnose_injections(structure, injections) if p is not None]


def split_injection_problems(
    structure: Dict[str, Dict[str, Any]], injections: List[Dict[str, Any]]
) -> Tuple[List[str], List[str]]:
    """把映射问题分成（致命、连线警告）两组。

    致命 = nodeId / fieldName 在云端结构里不存在，提交必被 803 整包拒绝，
    应该在本地直接报错，不花云端钱试错；连线警告 = 注入不生效但平台不拦，
    只值得提醒不值得阻断。
    """
    if not structure:
        return [], []
    fatal: List[str] = []
    linked: List[str] = []
    for problem in diagnose_injections(structure, injections):
        if problem is None:
            continue
        if "已被上游连线接管" in problem:
            linked.append(problem)
        else:
            fatal.append(problem)
    return fatal, linked


def describe(structure: Dict[str, Dict[str, Any]], limit: int = 60) -> str:
    """把工作流结构渲染成人能读的清单（供探查节点输出）。"""
    lines: List[str] = []
    for node_id, meta in sorted(structure.items(), key=lambda kv: _node_sort_key(kv[0])):
        class_type = meta.get("class_type") or "?"
        inputs = meta.get("inputs") or {}
        if not isinstance(inputs, dict):
            inputs = {}
        # 连线引用形如 [nodeId, outputIndex]，不是可注入的 widget，剔除
        fields = {k: v for k, v in inputs.items() if not _is_link(v)}
        if not fields:
            continue
        # 用户自定义的节点标题（_meta.title）比 class_type 更能说明这个节点是干什么的，
        # 探查清单里带上它，用户才知道该往哪注入；默认标题等于类型名时就不重复显示。
        node_meta = meta.get("meta")
        title = str(node_meta.get("title") or "") if isinstance(node_meta, dict) else ""
        if title and title != class_type:
            lines.append(f"● nodeId={node_id}  [{class_type}]  「{title}」")
        else:
            lines.append(f"● nodeId={node_id}  [{class_type}]")
        for name in sorted(fields.keys()):
            shown = str(fields[name])
            if len(shown) > limit:
                shown = shown[: limit - 3] + "..."
            lines.append(f"    fieldName = {name}   当前值 = {shown}")
    return "\n".join(lines)


def mapping_templates(
    structure: Dict[str, Dict[str, Any]], value_limit: int = 40
) -> str:
    """为每个节点的**字面量字段**生成可直接复制的参数映射片段。

    为什么要再出一份、而不是只给 describe 的字段清单：用户抄 nodeId / fieldName
    再手打 JSON 的过程本身就是出错来源（引号、逗号、字段名打错 → 平台 803 整包拒绝）。
    这里直接给出能粘进映射框的完整片段，用户只改 fieldValue 即可。

    连线字段（`[]` 形态）**不给模板**：平台不建议修改连线字段，给了就等于
    诱导用户写出必然失效的映射。因此输出里必须把两者明确区分开。
    """
    import json

    lines: List[str] = []
    for node_id, meta in sorted(structure.items(), key=lambda kv: _node_sort_key(kv[0])):
        inputs = meta.get("inputs")
        if not isinstance(inputs, dict) or not inputs:
            continue
        literal = {k: v for k, v in inputs.items() if not _is_link(v)}
        linked = [k for k, v in inputs.items() if _is_link(v)]
        if not literal and not linked:
            continue

        class_type = meta.get("class_type") or "?"
        node_meta = meta.get("meta")
        title = str(node_meta.get("title") or "") if isinstance(node_meta, dict) else ""
        if title and title != class_type:
            lines.append(f"● nodeId={node_id}  [{class_type}]  「{title}」")
        else:
            lines.append(f"● nodeId={node_id}  [{class_type}]")

        for name in sorted(literal.keys()):
            value: Any = literal[name]
            if isinstance(value, str) and len(value) > value_limit:
                value = value[:value_limit] + "…"
            snippet = json.dumps(
                {"nodeId": node_id, "fieldName": name, "fieldValue": value},
                ensure_ascii=False,
            )
            lines.append(f"    {snippet}")
        for name in sorted(linked):
            src = str(inputs[name][0]) if inputs[name] else "?"
            lines.append(
                f"    × {name} 是连线字段（由 nodeId={src} 提供）—— 不能注入，不提供模板"
            )
    return "\n".join(lines)


__all__ = [
    "normalize",
    "build_prompt_injections",
    "find_seed_targets",
    "validate_injections",
    "split_injection_problems",
    "diagnose_injections",
    "iter_string_fields",
    "describe",
    "mapping_templates",
    "prompt_link_diagnosis",
    "placeholder_hits",
]
