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


def normalize(data: Any) -> Dict[str, Dict[str, Any]]:
    """把各种形态的工作流响应归一化成：

        {nodeId(str): {"class_type": str, "inputs": {字段名: 值}}}

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
            out[str(key)] = {"class_type": class_type, "inputs": inputs}
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
            out[str(node_id)] = {"class_type": class_type, "inputs": inputs}
        return out

    return {}


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


def build_prompt_injections(
    structure: Dict[str, Dict[str, Any]], positive: str, negative: str
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """按占位符生成提示词注入项。

    返回 (注入列表, {"positive": 命中次数, "negative": 命中次数})。

    调用方必须检查命中次数：为 0 意味着云端工作流里根本没有占位符，
    用户填的提示词不会生效——这种情况必须显式警告，绝不能静默跳过。
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


def validate_injections(
    structure: Dict[str, Dict[str, Any]], injections: List[Dict[str, Any]]
) -> List[str]:
    """校验用户手写的参数映射，返回中文问题列表（空列表 = 全部合法）。

    结构不可得时返回空列表——此时无法校验，不能拿不确定的信息去吓唬用户。
    """
    if not structure:
        return []

    problems: List[str] = []
    for item in injections:
        node_id = str(item.get("nodeId", ""))
        field = str(item.get("fieldName", ""))
        meta = structure.get(node_id)
        if meta is None:
            problems.append(
                f"nodeId「{node_id}」在云端工作流里不存在"
                "（换工作流后最容易踩这个坑，可用「RH 工作流参数探查」重新拉清单）"
            )
            continue
        inputs = meta.get("inputs") or {}
        if not isinstance(inputs, dict) or field not in inputs:
            available = ", ".join(sorted(inputs.keys())) if inputs else "（该节点无可注入字段）"
            problems.append(
                f"nodeId「{node_id}」没有字段「{field}」，"
                f"该节点可用字段：{available}"
            )
    return problems


def describe(structure: Dict[str, Dict[str, Any]], limit: int = 60) -> str:
    """把工作流结构渲染成人能读的清单（供探查节点输出）。"""
    lines: List[str] = []
    for node_id, meta in sorted(structure.items(), key=lambda kv: _node_sort_key(kv[0])):
        class_type = meta.get("class_type") or "?"
        inputs = meta.get("inputs") or {}
        if not isinstance(inputs, dict):
            inputs = {}
        # 连线引用形如 [nodeId, outputIndex]，不是可注入的 widget，剔除
        fields = {
            k: v
            for k, v in inputs.items()
            if not (isinstance(v, list) and len(v) == 2 and isinstance(v[0], (str, int)))
        }
        if not fields:
            continue
        lines.append(f"● nodeId={node_id}  [{class_type}]")
        for name in sorted(fields.keys()):
            shown = str(fields[name])
            if len(shown) > limit:
                shown = shown[: limit - 3] + "..."
            lines.append(f"    fieldName = {name}   当前值 = {shown}")
    return "\n".join(lines)


__all__ = [
    "normalize",
    "build_prompt_injections",
    "find_seed_targets",
    "validate_injections",
    "iter_string_fields",
    "describe",
]
