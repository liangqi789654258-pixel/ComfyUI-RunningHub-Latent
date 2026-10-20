"""参数映射：解析 `node_overrides` + 占位符替换。

**单一代码源**：插件节点（`nodes.py`）与 RH 出图器（`desktop/app`）共用本模块。
历史上两边各写了一份，结果语义悄悄分叉（出图器不支持 dict 简写与 `value` 别名，
报错文案也少一条）。共享一份实现后，改语义只需改这里。

两个函数都是纯函数：不碰网络、不碰文件、不依赖 torch，可离线单测。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from .errors import RHUserError

# {seed} / {positive} / {negative} …，也允许用户自定义 token
_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

_JSON_HINT = (
    "请检查是否漏了引号或多了逗号，标准格式形如："
    '[{"nodeId": "3", "fieldName": "seed", "fieldValue": "123"}]'
)


def normalize_overrides(raw: Any) -> List[Dict[str, Any]]:
    """把用户填写的 overrides 解析成规范 nodeInfoList。

    宽容处理三件事（都是真实踩过的坑）：
      1. 空输入（None / "" / "[]" / "{}"）；
      2. 用户写成 value 而不是 fieldValue（官方接口只认后者）；
      3. 单个节点字段被写成 dict 简写形式 {"3": {"seed": 1}}。

    宽容是有边界的：字段名错、缺 nodeId/fieldName 一律报错，绝不猜。
    """
    if raw is None:
        return []
    if isinstance(raw, (list, dict)):
        items: Any = raw
    else:
        text = str(raw).strip()
        if not text or text in ("[]", "{}"):
            return []
        try:
            items = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RHUserError(
                f"参数映射 JSON 格式有误：第 {exc.lineno} 行第 {exc.colno} 列 —— {exc.msg}。"
                + _JSON_HINT
            ) from exc

    items_list: List[Dict[str, Any]] = []
    if isinstance(items, dict):
        for node_id, fields in items.items():
            if isinstance(fields, dict):
                for field_name, value in fields.items():
                    items_list.append(
                        {
                            "nodeId": str(node_id),
                            "fieldName": str(field_name),
                            "fieldValue": value,
                        }
                    )
            else:
                # {"3": "一段文本"} 视为该节点的默认文本字段
                items_list.append(
                    {"nodeId": str(node_id), "fieldName": "text", "fieldValue": fields}
                )
    else:
        items_list = list(items)

    result: List[Dict[str, Any]] = []
    for item in items_list:
        if not isinstance(item, dict):
            raise RHUserError(f"参数映射每一项都必须是对象，实际收到：{item!r}")
        node_id = item.get("nodeId") or item.get("node_id")
        field_name = item.get("fieldName") or item.get("field_name")
        # 官方文档强调必须是 fieldValue，这里直接帮用户纠正写法
        if "fieldValue" in item:
            field_value = item["fieldValue"]
        elif "value" in item:
            field_value = item["value"]
            print(
                "[RunningHub-Latent] 已自动把 value 改写为 fieldValue"
                f"（nodeId={node_id}, fieldName={field_name}）—— 官方接口只认 fieldValue。"
            )
        else:
            raise RHUserError(
                f"参数映射缺少 fieldValue 字段：{item}。"
                f'正确格式：{{"nodeId": "{node_id}", "fieldName": "{field_name}", '
                f'"fieldValue": "你的值"}}'
            )
        if node_id is None or field_name is None:
            raise RHUserError(f"参数映射缺少 nodeId 或 fieldName：{item}")
        result.append(
            {
                "nodeId": str(node_id),
                "fieldName": str(field_name),
                "fieldValue": field_value,
            }
        )
    return result


def apply_placeholders(
    node_info_list: List[Dict[str, Any]], values: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """把 {seed} / {positive} 之类的占位符替换成真实值。

    两种形态区别对待：
      * 整值占位符 "{seed}" → 直接替换为原值，**保留 int/float 类型**。
        若退化成字符串，云端收到的是 "42" 而不是 42，部分工作流会因类型不匹配报错。
      * 混合表达式 "seed_{seed}" → 退化为字符串插值。
    """
    resolved: List[Dict[str, Any]] = []
    for item in node_info_list:
        value = item["fieldValue"]
        if isinstance(value, str):
            whole = _PLACEHOLDER.fullmatch(value)
            if whole is not None:
                name = whole.group(1)
                _require_known(name, values)
                value = values[name]
            else:

                def _sub(match: "re.Match[str]") -> str:
                    name = match.group(1)
                    _require_known(name, values)
                    return str(values[name])

                value = _PLACEHOLDER.sub(_sub, value)
        resolved.append(
            {
                "nodeId": item["nodeId"],
                "fieldName": item["fieldName"],
                "fieldValue": value,
            }
        )
    return resolved


def _require_known(name: str, values: Dict[str, Any]) -> None:
    if name not in values:
        raise RHUserError(
            f"参数映射里引用了未知占位符 {{{name}}}。"
            f"当前可用占位符：{', '.join(sorted(values))}"
        )


def merge_injections(*groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """合并多组注入项，后者覆盖前者（按 nodeId + fieldName 去重）。

    优先级：用户手工映射 > 自动定位。按 (nodeId, fieldName) 排序保证缓存键稳定。
    """
    merged: Dict[tuple, Dict[str, Any]] = {}
    for group in groups:
        for item in group or []:
            merged[
                (str(item.get("nodeId", "")), str(item.get("fieldName", "")))
            ] = item
    return [merged[k] for k in sorted(merged.keys())]


__all__ = [
    "normalize_overrides",
    "apply_placeholders",
    "merge_injections",
]
