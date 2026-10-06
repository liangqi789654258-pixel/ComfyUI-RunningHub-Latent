"""
ComfyUI 节点定义。

## 两个核心版本怎么选

* **极简版**（RH 云端采样 · 极简版）
  只有 api_key 和 workflow_id 两个输入框：一切参数都用云端的，本地只负责
  触发采样和解码。种子每次执行自动随机，其余走 config.ini。
  要改云端任何参数请换「专业版」，极简版没有参数映射入口。

* **专业版**（RH 云端采样 · 专业版）
  在极简版同一套自动注入之外，额外暴露 nodeInfoList 参数映射，
  可以精确改写云端任意节点的任意字段（步数 / CFG / 尺寸等）。
  手工映射与自动注入冲突时，以手工映射为准。

## 参数注入的两条通道

RunningHub 的参数注入接口要求必须给出 nodeId：

    nodeInfoList = [{"nodeId": "6", "fieldName": "text", "fieldValue": "1 girl"}]

1. **占位符替换（自动）**：先用 getJsonApiFormat 把云端工作流结构拉下来，然后在
   **所有字符串字段**里搜索 `{positive}` / `{negative}` 标记并替换，再找采样器的
   seed 字段注入种子。因为不依赖具体的 class_type，能兼容任意结构的工作流。
   种子语义明确，多采样器统一注入同一个 seed 是我们要的行为，因此保留。

2. **参数映射（手动，`node_overrides`）**：只有「专业版」有这个框；
   极简版按契约不提供映射入口。

曾经存在一条「沿采样器 positive/negative 连线向上游回溯找文本字段」的兜底通道，
**已推倒**：图像大小设置和提示词的接法每条工作流都不一样，猜出来的落点一旦错了
就是静默失效（用户以为生效了，其实没送上去）。现在没有占位符 = 没有自动落点。

**红线**：用户填了提示词却一个占位符都没匹配到、映射里也没指定落点时，
必须**报错**中止，绝不允许静默跑完。
"""

from __future__ import annotations

import json
import os
import random
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

import torch

try:  # 保存 latent 用到；缺失时只影响「AV Latent 保存」节点
    import safetensors.torch
except Exception:  # pragma: no cover
    safetensors = None

from .core import cache as cache_mod
from .core import client as client_mod
from .core import latent as latent_mod
from .core import settings as settings_mod
from .core import workflow as wf_mod

try:  # ComfyUI 提供；离线单测环境下允许缺失
    import folder_paths
except Exception:  # pragma: no cover
    folder_paths = None

CATEGORY = "RunningHub"

# 输出分类用的关键字。真实接口里类型字段名并不统一（fileType / outputType / type），
# 大小写也不统一（见过 "Latent"），所以这里统一小写后做子串匹配。
_LATENT_HINTS = ("latent", "safetensor")
_VIDEO_EXT = (".mp4", ".webm", ".mov", ".avi", ".gif")
_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp")

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

# 前端自定义事件名，web/rh_warning.js 监听它来做节点变黄
_WARNING_EVENT = "runninghub-latent-warning"


class RHUserError(RuntimeError):
    """面向用户的中文错误，ComfyUI 会完整展示在前端，不做英文堆栈。"""


# ---------------------------------------------------------------------------
# 通用辅助
# ---------------------------------------------------------------------------


def _make_progress(steps: int):
    """进度条。拿不到 ComfyUI 的 ProgressBar 时退化为空实现，保证兼容老版本。"""
    try:  # pragma: no cover - ComfyUI 专有
        from comfy.utils import ProgressBar

        return ProgressBar(max(1, steps))
    except Exception:

        class _Null:
            def update(self, *a, **k):
                pass

            def update_absolute(self, *a, **k):
                pass

        return _Null()


def _emit(tag: str, text: str) -> None:
    """在 ComfyUI 控制台留痕。节点里的大量等待必须有可见反馈。"""
    print(f"[RunningHub-Latent/{tag}] {text}")


def _send_warning(unique_id: Any, warnings: List[str]) -> None:
    """把警告推给前端，让节点边框变黄。纯 Python 做不到上色，靠 JS 扩展实现。

    失败完全静默——这只是一个视觉增强，不能因为前端没加载就把任务搞挂。
    """
    if not unique_id or not warnings:
        return
    try:  # pragma: no cover - ComfyUI 专有
        from server import PromptServer

        PromptServer.instance.send_sync(
            _WARNING_EVENT,
            {"node_id": str(unique_id), "message": "\n".join(warnings)},
        )
    except Exception:
        pass


def _resolve_api_key(raw: str) -> str:
    key = str(settings_mod.resolve(raw, "api_key", "") or "").strip()
    if not key:
        raise RHUserError(
            "缺少 RunningHub API Key。两种配置方式任选其一：\n"
            "  1. 直接填在节点的 api_key 输入框里；\n"
            f"  2. 在插件目录新建 config.ini（或 config.json）写入 api_key，"
            f"之后所有节点都不用再填。配置文件位置：{settings_mod.ini_path()}"
        )
    return key


def _resolve_workflow_id(raw: str) -> str:
    wid = str(settings_mod.resolve(raw, "default_workflow_id", "") or "").strip()
    if not wid:
        raise RHUserError(
            "缺少 workflow_id（RunningHub 工作流 ID）。\n"
            "可从云端工作流页面的 URL 里取：https://www.runninghub.cn/workflow/<这里就是 ID>"
        )
    return wid


def _make_client(api_key: str) -> "client_mod.RunningHubClient":
    base_url = str(settings_mod.get("base_url", client_mod.DEFAULT_BASE_URL))
    return client_mod.RunningHubClient(api_key=api_key, base_url=base_url, timeout=30.0)


# ---------------------------------------------------------------------------
# 参数注入解析
# ---------------------------------------------------------------------------


def _normalize_overrides(raw: Any) -> List[Dict[str, Any]]:
    """把用户填写的 overrides 解析成规范 nodeInfoList。

    宽容处理三件事（都是真实踩过的坑）：
      1. 空输入
      2. 用户写成 value 而不是 fieldValue
      3. 单个节点字段被写成 dict 简写形式 {"3": {"seed": 1}}
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
                "请检查是否漏了引号或多了逗号，标准格式形如："
                '[{"nodeId": "3", "fieldName": "seed", "fieldValue": "123"}]'
            ) from exc

    items_list: List[Dict[str, Any]] = []
    if isinstance(items, dict):
        for node_id, fields in items.items():
            if isinstance(fields, dict):
                for field_name, value in fields.items():
                    items_list.append(
                        {"nodeId": str(node_id), "fieldName": str(field_name), "fieldValue": value}
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
                f"正确格式：{{\"nodeId\": \"{node_id}\", \"fieldName\": \"{field_name}\", \"fieldValue\": \"你的值\"}}"
            )
        if node_id is None or field_name is None:
            raise RHUserError(f"参数映射缺少 nodeId 或 fieldName：{item}")
        result.append(
            {"nodeId": str(node_id), "fieldName": str(field_name), "fieldValue": field_value}
        )
    return result


def _apply_placeholders(
    node_info_list: List[Dict[str, Any]], values: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """把 {seed} / {positive} 之类的占位符替换成 widget 的真实值。

    两种形态区别对待：
      * 整值占位符 "{seed}" -> 直接替换为原值，**保留 int/float 类型**。
        整值替换若退化成字符串，云端收到的 seed 会是 "42" 而不是 42，
        部分工作流会因类型不匹配报错。
      * 混合表达式 "seed_{seed}" -> 退化为字符串插值。
    """
    resolved: List[Dict[str, Any]] = []
    for item in node_info_list:
        value = item["fieldValue"]
        if isinstance(value, str):
            whole = _PLACEHOLDER.fullmatch(value)
            if whole is not None:
                name = whole.group(1)
                if name not in values:
                    raise RHUserError(
                        f"参数映射里引用了未知占位符 {{{name}}}。"
                        f"当前可用占位符：{', '.join(sorted(values))}"
                    )
                value = values[name]
            else:

                def _sub(match: "re.Match[str]") -> str:
                    name = match.group(1)
                    if name not in values:
                        raise RHUserError(
                            f"参数映射里引用了未知占位符 {{{name}}}。"
                            f"当前可用占位符：{', '.join(sorted(values))}"
                        )
                    return str(values[name])

                value = _PLACEHOLDER.sub(_sub, value)
        resolved.append(
            {"nodeId": item["nodeId"], "fieldName": item["fieldName"], "fieldValue": value}
        )
    return resolved


def _merge_injections(
    *groups: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """合并多组注入项，后者覆盖前者（按 nodeId + fieldName 去重）。

    优先级：用户手工映射 > 自动定位。这样极简版自动找到的结果可以被用户在
    专业版里覆盖，不会出现「改了没反应」。
    """
    merged: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for group in groups:
        for item in group or []:
            merged[(str(item.get("nodeId", "")), str(item.get("fieldName", "")))] = item
    # 按 nodeId / fieldName 排序，保证缓存键稳定
    return [merged[k] for k in sorted(merged.keys())]


def _preview(text: str, limit: int = 60) -> str:
    """把提示词压成单行预览，方便在 report 里核对「实际提交的是什么」。

    这个字段看着不起眼，却是排查「提示词没生效」最快的手段：一旦输入框被
    转换成输入接口并接了上游节点，框里看到的文字就不再生效，只有这里显示的
    才是真正送进云端的内容。
    """
    flat = " ".join(str(text or "").split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


# ---------------------------------------------------------------------------
# 输出识别
# ---------------------------------------------------------------------------


def _output_type_of(item: Dict[str, Any]) -> str:
    """取出输出类型字符串并统一小写。

    真实接口里这个字段见过 fileType / outputType / type 三种名字，
    值也见过 "latent" / "Latent" 混用，所以：多个名字都读 + 一律小写。
    """
    for key in ("fileType", "outputType", "type", "file_type", "output_type"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return ""


def _classify(item: Dict[str, Any]) -> str:
    """判断一个输出文件是 latent / image / video / unknown。"""
    file_type = _output_type_of(item)
    url = str(item.get("fileUrl", "") or item.get("url", "") or "").lower()
    # 去掉查询串再取扩展名（带签名的 URL 末尾通常是 ?token=...）
    url = url.split("?", 1)[0]

    # .h3latent / .avlatent 是社区「H3 音视频 latent 保存节点」用的扩展名，
    # 一并认作 latent，否则这类文件会被判成 unknown 而取不到。
    if any(h in file_type for h in _LATENT_HINTS) or url.endswith(
        (".latent", ".safetensors", ".h3latent", ".avlatent")
    ):
        return "latent"
    if url.endswith(_VIDEO_EXT) or file_type in ("mp4", "webm"):
        return "video"
    if url.endswith(_IMAGE_EXT) or file_type in ("png", "jpg", "jpeg", "webp"):
        return "image"
    return "unknown"


def _collect_latents(outputs: List[Dict[str, Any]], raw_hint: str = "") -> List[Dict[str, Any]]:
    """筛出所有 latent 输出并按稳定顺序排序。找不到时给出可操作的中文指引。"""
    candidates = [o for o in outputs if _classify(o) == "latent"]
    if not candidates:
        summary: List[str] = []
        for item in outputs:
            kind = _classify(item)
            summary.append(
                f"  - [{kind}] {item.get('fileUrl', item.get('url', ''))} "
                f"(nodeId={item.get('nodeId', '?')}, "
                f"类型字段={_output_type_of(item) or '缺失'})"
            )
        detail = "\n".join(summary) if summary else "  （云端返回的输出列表为空）"
        extra = f"\n云端原始返回（供排错）：\n{raw_hint}" if raw_hint else ""

        raise RHUserError(
            "云端任务已完成，但没有识别出 latent 文件。\n"
            f"本次云端实际输出：\n{detail}\n{extra}\n\n"
            "请按顺序排查：\n"
            "  1. 云端工作流末端必须接「保存Latent」节点，而不是「保存图像」；\n"
            "  2. 「保存Latent」节点要能真正被执行到（不能被下游节点短路）；\n"
            "  3. 若上面显示类型字段为「缺失」，说明云端返回结构有变，"
            "请把上面这段原始返回发给插件作者，我会加进兼容列表。"
        )

    candidates.sort(key=lambda o: (str(o.get("nodeId", "")), str(o.get("fileUrl", ""))))
    return candidates


def _merge_latents(tensors: List[torch.Tensor]) -> torch.Tensor:
    """把多个输出的 latent 沿 batch 维合并。

    形状一致时合并成一个 batch（这也正是 ComfyUI 原生 SaveLatent 多图输出的语义）；
    形状不一致时不做猜测，直接取第一个并给出提示。
    """
    if len(tensors) == 1:
        return tensors[0]

    first_shape = tuple(tensors[0].shape[1:])
    if all(tuple(t.shape[1:]) == first_shape for t in tensors[1:]):
        return torch.cat(tensors, dim=0)

    print(
        "[RunningHub-Latent] 警告：云端输出了多个尺寸的 latent，无法沿 batch 维合并，"
        "本次仅取第一个。请检查云端工作流是否混接了多个分辨率的生成分支。"
        "（警告：其余 latent 已被丢弃，如需全部结果请拆成多个节点分别取回。）"
    )
    return tensors[0]


def _fetch_latent_blob(url: str, timeout: float = 120.0) -> bytes:
    # 中文文件名 / 带签名参数的 URL 交给 requests 原样处理，不做任何手工编解码
    return cache_mod.download(url, timeout=timeout)


def _describe_samples(samples: Any) -> str:
    """把 latent 形状写成人类可读文本；音视频复合 latent 两个都列出来。"""
    if samples is None:
        return "（空）"
    if latent_mod.is_nested(samples):
        parts = [tuple(t.shape) for t in samples.unbind()]
        if len(parts) >= 2:
            return f"视频 {parts[0]} + 音频 {parts[1]}"
        return f"视频 {parts[0]}"
    try:
        return str(tuple(samples.shape))
    except Exception:  # pragma: no cover
        return "（未知形状）"


def _load_latent(blob: bytes, url: str) -> dict:
    try:
        return latent_mod.parse(blob, url)
    except latent_mod.LatentParseError as exc:
        raise RHUserError(
            f"latent 文件下载成功但解析失败：{exc}\n"
            f"下载地址：{url}\n"
            "这通常意味着云端工作流末端接的不是标准「保存Latent」节点。"
        ) from exc


def _resolve_outputs_to_latent(
    outputs: List[Dict[str, Any]],
    cache_key: Optional[str] = None,
    client: Optional["client_mod.RunningHubClient"] = None,
    on_stage: Optional[Any] = None,
) -> Tuple[dict, List[str], Optional[str]]:
    """下载所有 latent 输出并合并成一个 ComfyUI LATENT。"""
    raw_hint = ""
    if client is not None:
        try:
            raw = client.last_raw_body()
            if raw is not None:
                text = json.dumps(raw, ensure_ascii=False)
                raw_hint = client._mask(text)[:600]
        except Exception:
            raw_hint = ""

    items = _collect_latents(outputs, raw_hint)
    tensors: List[torch.Tensor] = []
    urls: List[str] = []
    stored: Optional[str] = None

    for index, item in enumerate(items, 1):
        url = str(item.get("fileUrl", "") or item.get("url", ""))
        if on_stage is not None:
            on_stage(f"下载中（{index}/{len(items)}）")
        _emit("download", f"正在下载 latent：{url}")
        blob = _fetch_latent_blob(url)
        if cache_key and len(items) == 1:
            stored = cache_mod.store(cache_key, blob)
        tensors.append(_load_latent(blob, url)["samples"])
        urls.append(url)

    return ({"samples": _merge_latents(tensors)}, urls, stored)


# ---------------------------------------------------------------------------
# 提示词落点红线 + 803 逐条定位
# ---------------------------------------------------------------------------


def _require_prompt_targets(
    structure: Dict[str, Any],
    stats: Dict[str, int],
    positive: str,
    negative: str,
    overrides_json: str,
) -> None:
    """填了提示词却没有落点 -> **直接报错**（极简版与专业版共用的判定）。

    为什么从「警告」升级为「报错」：自动沿连线猜测落点的能力已推倒
    （每条工作流的提示词接法都不一样，猜错就是静默失效）。在新的纯手动契约下，
    「填了提示词但没有占位符、映射里也没指定落点」意味着这次运行**根本不会把
    提示词送上去**，用户会拿到云端默认文案却以为是自己的提示词生效了——
    这比报错更糟。

    `overrides_json` 必须是**占位符替换前**的映射原文，否则用户写的
    `{positive}` 已经变成提示词文本，会被误判成「没有指定落点」。

    结构拉不到时直接返回：无从判断，不能编造（沿用「结构缺失」那条提示）。
    """
    if not structure:
        return

    for token, label, value in (
        ("positive", "正面提示词", positive),
        ("negative", "负面提示词", negative),
    ):
        if not str(value or "").strip():
            continue
        if int(stats.get(token, 0) or 0) > 0:
            continue
        if "{" + token + "}" in overrides_json:
            continue
        reason = wf_mod.prompt_link_diagnosis(structure, token)
        raise RHUserError(
            f"{label}已填写，但云端工作流里没有 {{{token}}} 占位符，"
            f"参数映射里也没有引用 {{{token}}} —— 本次不会有任何提示词被送到云端，"
            "继续跑只会拿到云端默认文案（静默失效，比报错更糟）。\n"
            f"定位结果：{reason}\n"
            "请在参数映射里补一条（复制后把 nodeId 换成你自己的）：\n"
            f'  {{"nodeId": "<ID>", "fieldName": "text", "fieldValue": "{{{token}}}"}}\n'
            f"nodeId 用「RH 工作流参数探查」节点查询；"
            f"也可以在云端文本节点的文本框里写上 {{{token}}} 标记，本节点会自动替换。"
        )


def _warn_empty_prompt_vs_placeholder(
    structure: Dict[str, Any],
    positive: str,
    negative: str,
    extra_hint: str,
    warnings: List[str],
) -> None:
    """空提示词撞上云端占位符 = 会把云端提示词替换成空内容，必须说出来。

    这个场景没有任何报错、画面也完全不对，是排查成本最高的一类静默失效。
    """
    if not structure:
        return
    hits = wf_mod.placeholder_hits(structure)
    for token, label, value in (
        ("positive", "正面提示词", positive),
        ("negative", "负面提示词", negative),
    ):
        if str(value or "").strip() or int(hits.get(token, 0) or 0) <= 0:
            continue
        warnings.append(
            f"{label}是空的，但云端工作流里有 {hits[token]} 处 {{{token}}} 标记，"
            "本次会把云端提示词替换为空内容（画面对不上却没有任何报错，就是这个原因）。\n"
            f"{extra_hint}"
        )


def _diagnose_803(
    structure: Dict[str, Any],
    node_info_list: List[Dict[str, Any]],
    raw_message: str,
    refresher: "Optional[Callable[[], Dict[str, Any]]]" = None,
) -> str:
    """把平台 803「nodeInfoList 整包不合法」翻译成**逐条**清单。

    病根：平台只在提交阶段回一句「不合法」，10 条映射错 1 条时用户根本看不出
    是哪一条。而我们手里同时握着 `structure` 和 `diagnose_injections()`，
    完全可以逐条对照后指名道姓。
    """
    total = len(node_info_list)
    lines = [
        "云端拒绝了本次参数映射（错误码 803：nodeInfoList 与云端工作流不一致）。",
        f"本次共提交 {total} 条映射。",
        "",
    ]

    if not structure:
        lines.extend(
            [
                "本次没能读到云端工作流结构，无法逐条定位（请确认工作流 ID 正确且已保存）。",
                f"平台原始提示：{raw_message}",
                "",
                "请用「RH 工作流参数探查」节点重新拉一份真实的 nodeId / fieldName 清单后逐条核对。",
            ]
        )
        return "\n".join(lines)

    problems = wf_mod.diagnose_injections(structure, node_info_list)
    bad = [
        (idx + 1, item, problem)
        for idx, (item, problem) in enumerate(zip(node_info_list, problems))
        if problem is not None
    ]

    if not bad and refresher is not None:
        # 本地结构是带 TTL 的进程内缓存；云端刚改完工作流时它可能是旧的，于是
        # 「逐条对照后没有问题」这个结论本身就不成立。平台已经拒绝了本次提交
        # （803 不计费），多拉一次结构成本很低，值得换一个可信的结论。
        # 刷新失败不能掩盖原始报错，所以这里吞掉异常退回原结论。
        try:
            fresh = refresher()
        except Exception:  # noqa: BLE001 - 诊断阶段不允失败影响主流程
            fresh = {}
        if fresh:
            problems = wf_mod.diagnose_injections(fresh, node_info_list)
            bad = [
                (idx + 1, item, problem)
                for idx, (item, problem) in enumerate(zip(node_info_list, problems))
                if problem is not None
            ]
            if bad:
                lines.append("（本地缓存的结构可能已过期，已强制重新拉取云端结构后定位到问题）")
                lines.append("")

    if not bad:
        lines.extend(
            [
                "逐条对照云端结构后**没有**发现不合法的项。",
                "常见原因是类型不匹配（该字段只接受数字却提交了字符串），"
                "或云端工作流在你上次运行之后被改过。",
                f"平台原始提示：{raw_message}",
                "",
                "请用「RH 工作流参数探查」重新拉一次清单，对照字段的当前值形态填写。",
            ]
        )
        return "\n".join(lines)

    lines.append(f"其中 {len(bad)} 条不合法：")
    for idx, item, problem in bad:
        lines.append(
            f"  第 {idx} 条  nodeId={item.get('nodeId')}  fieldName={item.get('fieldName')}"
        )
        lines.append(f"      问题：{problem}")
    lines.extend(
        [
            "",
            "修法：",
            "  1. 用「RH 工作流参数探查」节点重新拉一次清单（它会直接给出可复制的映射片段）；",
            "  2. 把上面点名的 nodeId / fieldName 改成清单里的真实值；",
            "  3. 连线字段（探查结果里标 × 的）不能注入，请改注上游的字面量字段。",
        ]
    )
    return "\n".join(lines)


def _submit_task(
    client: "client_mod.RunningHubClient",
    workflow_id: str,
    node_info_list: List[Dict[str, Any]],
    structure: Optional[Dict[str, Any]],
    use_high_performance: bool,
) -> str:
    """提交任务；遇到 803 时转成带逐条清单的中文错误。"""
    try:
        return client.create_task(
            workflow_id,
            node_info_list,
            instance_type="plus" if use_high_performance else None,
        )
    except client_mod.RunningHubError as exc:
        if getattr(exc, "code", None) == client_mod.CODE_PARAM_INVALID:
            raise RHUserError(
                _diagnose_803(
                    structure or {},
                    node_info_list,
                    str(exc),
                    refresher=lambda: client.get_workflow_structure(
                        workflow_id, force=True
                    ),
                )
            ) from exc
        raise


# ---------------------------------------------------------------------------
# 共用执行流程
# ---------------------------------------------------------------------------


def _run_sampling(
    client: "client_mod.RunningHubClient",
    workflow_id: str,
    node_info_list: List[Dict[str, Any]],
    effective_seed: int,
    timeout: float,
    poll_interval: float,
    use_cache: bool,
    use_high_performance: bool,
    warnings: List[str],
    unique_id: Any = None,
    structure: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, torch.Tensor], str, List[str]]:
    """提交 -> 轮询 -> 下载 -> 组装报告。两个版本节点共用。

    `structure` 只用于 803 的逐条定位（提交失败时拿它逐条核对 nodeInfoList）。

    返回 (latent, task_id, report_lines)。
    """
    cache_key = cache_mod.compute_cache_key(workflow_id, node_info_list)

    if use_cache:
        hit = cache_mod.cache_path_for(cache_key)
        if hit:
            _emit("generator", f"命中本地缓存，跳过云端调用：{hit}")
            with open(hit, "rb") as fh:
                blob = fh.read()
            result = _load_latent(blob, hit)
            lines = [
                "命中本地缓存，本次未消耗云端额度。",
                f"工作流 ID：{workflow_id}",
                f"缓存文件：{hit}",
                f"张量形状：{_describe_samples(result.get('samples'))}",
            ]
            return (result, "cached", lines)

    pbar = _make_progress(5)

    def _on_stage(label: str) -> None:
        _emit("generator", label)

    _on_stage("提交中")
    task_id = _submit_task(
        client, workflow_id, node_info_list, structure, use_high_performance
    )
    _emit("generator", f"任务已提交，taskId={task_id}")
    pbar.update(1)

    def _on_progress(label: str, elapsed: float) -> None:
        pbar.update(1)
        _emit("generator", f"{label}（已用时 {elapsed:.0f} 秒）")

    outputs = client.wait_for_outputs(
        task_id,
        poll_interval=float(poll_interval),
        timeout=float(timeout),
        on_progress=_on_progress,
    )
    pbar.update(1)

    if not outputs:
        # 状态已成功但补查后依然拿不到文件：任务钱已经花了，必须给出可挽回的路径
        raise RHUserError(
            f"云端任务 {task_id} 已返回成功，但反复查询都拿不到输出文件。\n"
            "可能原因：\n"
            "  1. 云端工作流没有任何输出节点（末端既没接保存Latent也没接保存图像）；\n"
            "  2. 云端文件已过期被清理（较久之前的任务）。\n"
            f"请记下 taskId 稍后重试：{task_id}"
        )

    def _stage(text: str) -> None:
        _emit("generator", text)
        pbar.update(1)

    result, urls, stored = _resolve_outputs_to_latent(
        outputs,
        cache_key if use_cache else None,
        client=client,
        on_stage=_stage,
    )
    shape = tuple(result["samples"].shape)
    kind = "视频 latent（5 维）" if len(shape) == 5 else "图像 latent（4 维）"
    if latent_mod.is_nested(result.get("samples")):
        kind = "音视频复合 latent（MiniMax H3 等）"

    other = [o for o in outputs if _classify(o) != "latent"]
    lines = [
        f"云端采样完成，本地已获得 {kind}。",
        f"taskId：{task_id}",
        f"工作流 ID：{workflow_id}",
        f"张量形状：{_describe_samples(result.get('samples'))}（batch={shape[0]}）",
        f"生效随机种子：{effective_seed}",
    ]
    if len(urls) > 1:
        lines.append(
            f"云端共输出 {len(urls)} 个 latent，已沿 batch 维合并为 {shape[0]} 个结果。"
        )
    lines.append(
        f"latent 缓存路径：{stored}" if stored
        else "本次未写入本地缓存（多个 latent 输出时不缓存）。"
    )
    if other:
        lines.append(
            f"注意：云端还额外输出了 {len(other)} 个非 latent 文件，本次已忽略。"
            "建议到云端工作流删除这些输出节点，避免不必要的额度消耗。"
        )
    lines.append("下一步：把 latent 接到本地 VAEDecode / VAEDecodeTiled 即可出图。")

    if warnings:
        lines.append("")
        lines.append(f"⚠️ 本次执行有 {len(warnings)} 条警告：")
        lines.extend(f"  ⚠️ {w}" for w in warnings)
        _send_warning(unique_id, warnings)

    pbar.update(1)
    _emit("generator", "完成")
    return (result, task_id, lines)


def _auto_injections(
    client: "client_mod.RunningHubClient",
    workflow_id: str,
    positive: str,
    negative: str,
    seed: int,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any], Dict[str, int], List[Tuple[str, str]]]:
    """极简版/专业版共用的自动定位：提示词占位符全局替换 + 采样器种子注入。

    纯函数，不产生警告——警告怎么报（报几条、什么措辞）由调用方决定，
    因为两个节点对「没找到占位符」的容忍度不同。

    返回 (注入列表, 工作流结构, 占位符命中统计, 种子注入目标)。
    结构拿不到时返回空注入 + 空结构 + 零统计。
    """
    structure = client.get_workflow_structure(workflow_id)
    if not structure:
        return ([], {}, {"positive": 0, "negative": 0}, [])

    injections: List[Dict[str, Any]] = []

    prompt_injections, stats = wf_mod.build_prompt_injections(structure, positive, negative)
    injections.extend(prompt_injections)

    seed_targets = wf_mod.find_seed_targets(structure)
    for node_id, field in seed_targets:
        injections.append(
            {"nodeId": node_id, "fieldName": field, "fieldValue": int(seed)}
        )

    return (injections, structure, stats, seed_targets)


def _warn_missing_structure(warnings: List[str]) -> None:
    warnings.append(
        "没能读到云端工作流结构，自动注入已跳过（提示词/种子将沿用云端默认值）。"
        "请确认工作流 ID 正确且已保存（草稿状态无法被调用）；"
        "若持续失败，请在参数映射里手动填 nodeId / fieldName"
        "（用「RH 工作流参数探查」或在 RunningHub 网页编辑器里查 ID）。"
    )


# ---------------------------------------------------------------------------
# 节点 1：极简版（新手默认推荐）
# ---------------------------------------------------------------------------


class RunningHubLatentGeneratorSimple:
    """零配置节点：默认只填 api_key 和 workflow_id 就能出图。

    设计原则（用户原话：「什么都不用调，填入 api_key 和 workflow_id
    直接就能接 latent 解码出图」）：

      * 提示词：完全沿用云端工作流的默认文案（本地没有提示词输入框）；
      * 种子：每次执行自动生成一个新的随机种子并注入云端采样器，
        保证每次点运行都能出新图，而不是拿到同一张旧图；
      * timeout / poll_interval / use_cache 等参数不做成 widget，
        全部走 config.ini 的全局配置，想调的高级用户去配置文件里改。

    契约（用户拍板后定稿）：**只有 api_key / workflow_id 两个输入框**，不提供
    任何参数映射入口——极简版的一切参数都用云端的，本地只负责触发采样和解码。
    曾经短暂加过 `node_overrides` 框又移除：既然定位是极简版，要改参数
    就该换「专业版」，多一个用不上的框反而是负担。

    红线依然成立：种子没能注入时在 report 里明确警告，不静默。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "api_key": (
                    "STRING",
                    {
                        "multiline": False,
                        "default": "",
                        "placeholder": "留空则读取 config.ini / config.json",
                        "tooltip": "RunningHub API Key。留空则自动读取插件目录下的 config.ini。",
                    },
                ),
                "workflow_id": (
                    "STRING",
                    {
                        "multiline": False,
                        "default": "",
                        "placeholder": "RunningHub 工作流 ID",
                        "tooltip": "从云端工作流页面 URL 里取：runninghub.cn/workflow/<ID>。\n"
                                   "留空则读取配置文件里的 default_workflow_id。",
                    },
                ),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("LATENT", "STRING", "STRING")
    RETURN_NAMES = ("latent", "task_id", "report")
    FUNCTION = "execute"
    CATEGORY = CATEGORY
    TITLE = "RH 云端采样 · 极简版"
    DESCRIPTION = (
        "只填 API Key 和工作流 ID 两个框，其余全部沿用云端默认值。\n"
        "每次运行自动换随机种子；提示词、尺寸等一切参数都在云端工作流里改好，\n"
        "本地只负责触发采样与解码。要改云端参数请用「专业版」节点。"
    )

    @classmethod
    def IS_CHANGED(cls, **kwargs) -> float:
        # 本节点没有任何本地参数：每次执行都应换一个随机种子出新图，
        # 必须强制重算，否则 ComfyUI 会因为「输入没变」直接返回上次的 latent
        return float("nan")

    @classmethod
    def VALIDATE_INPUTS(cls, **kwargs) -> bool:
        return True

    def execute(
        self,
        api_key: str,
        workflow_id: str,
        unique_id: Any = None,
    ) -> Tuple[Dict[str, torch.Tensor], str, str]:
        api_key = _resolve_api_key(api_key)
        workflow_id = _resolve_workflow_id(workflow_id)
        client = _make_client(api_key)

        # 无 widget 的参数全部走配置文件，给高级用户留调节口
        timeout = float(settings_mod.get("timeout", 600.0))
        poll_interval = float(settings_mod.get("poll_interval", 5.0))
        use_cache = str(settings_mod.get("use_cache", True)).strip().lower() not in (
            "false", "0", "no", "off",
        )
        use_high_performance = str(
            settings_mod.get("use_high_performance", False)
        ).strip().lower() in ("true", "1", "yes", "on")

        effective_seed = random.randint(0, 0xFFFFFFFFFFFFFFFF)
        warnings: List[str] = []

        # 极简版契约：不提供参数映射入口，一切参数用云端的，本地只负责触发与解码。
        # 要改云端参数请换「专业版」（它有 node_overrides 映射框）。
        resolved_overrides: List[Dict[str, Any]] = []

        injections, _structure, _stats, seed_targets = _auto_injections(
            client, workflow_id, "", "", effective_seed
        )
        node_info_list = _merge_injections(injections, resolved_overrides)

        if not _structure:
            _warn_missing_structure(warnings)
        else:
            # 映射写错会在提交时被平台 803 整包拒绝，这里先本地逐条体检，
            # 能省一次「跑完才知道错了」的往返。
            warnings.extend(wf_mod.validate_injections(_structure, node_info_list))
            # 极简版没有提示词输入框：云端一旦写了占位符，本次就会被替换成空内容。
            # 这是本节点唯一可能「悄无声息毁掉云端提示词」的路径，必须显式说出来。
            _warn_empty_prompt_vs_placeholder(
                _structure,
                "",
                "",
                "极简版没有提示词输入框：请改用「专业版」填提示词，"
                "或在参数映射里把该节点的文本直接写死。",
                warnings,
            )
            if not seed_targets:
                warnings.append(
                    "云端工作流里没找到可注入的随机种子字段，本次沿用云端默认种子"
                    "（可能重复出同一张图）。如需固定/指定种子，请在参数映射里手动加 seed 字段。"
                )
            elif len(seed_targets) > 1:
                warnings.append(
                    f"云端检测到 {len(seed_targets)} 个采样器，已统一注入同一个随机种子。"
                )

        result, task_id, lines = _run_sampling(
            client,
            workflow_id,
            node_info_list,
            effective_seed,
            timeout,
            poll_interval,
            use_cache,
            use_high_performance,
            warnings,
            unique_id,
            structure=_structure,
        )
        return (result, task_id, "\n".join(lines))


# ---------------------------------------------------------------------------
# 节点 2：专业版（完整参数映射）
# ---------------------------------------------------------------------------


class RunningHubLatentGenerator:
    """面向需要精细调参的用户：本地提示词输入框 + 完整 nodeInfoList 参数映射。

    注册名保持 RunningHubLatentGenerator 不变——改名会让用户已保存的工作流
    全部断线，代价远大于显示名那点好处。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "api_key": (
                    "STRING",
                    {
                        "multiline": False,
                        "default": "",
                        "placeholder": "留空则读取 config.ini / config.json",
                        "tooltip": "RunningHub API Key。留空则自动读取插件目录下的 config.ini。",
                    },
                ),
                "workflow_id": (
                    "STRING",
                    {
                        "multiline": False,
                        "default": "",
                        "placeholder": "RunningHub 工作流 ID（留空读取配置文件）",
                        "tooltip": "从云端工作流页面 URL 里取：runninghub.cn/workflow/<ID>",
                    },
                ),
                "node_overrides": (
                    "STRING",
                    {
                        "multiline": True,
                        "default": "",
                        "placeholder": "advanced：参数映射 JSON（可选，留空即可。悬停查看格式）",
                        "tooltip": (
                            "【可选】云端节点参数映射，留空 = 完全使用云端默认值。\n"
                            "\n"
                            "留空时本节点仍会自动注入提示词与种子（与极简版同一套逻辑）。\n"
                            "只有想改步数 / CFG / 尺寸等云端参数时才需要填。\n"
                            "\n"
                            "格式（一行一个参数，整个是 JSON 数组）：\n"
                            '[{"nodeId": "3", "fieldName": "steps", "fieldValue": 30}]\n'
                            "\n"
                            "  * nodeId / fieldName：先用「RH 工作流参数探查」节点查出来；\n"
                            "  * fieldValue：直接写值；文本里可写 {seed} {positive} {negative}\n"
                            "    占位符，会绑定到下方对应输入框；\n"
                            "  * 与自动注入冲突时，以这里填的为准。"
                        ),
                    },
                ),
                "positive": (
                    "STRING",
                    {
                        "multiline": True,
                        "default": "",
                        "placeholder": "positive",
                        "tooltip": (
                            "正面提示词。自动替换云端工作流文本里的 {positive} 标记；\n"
                            "也可以在上方参数映射里用 {positive} 占位符精确指定注入位置。\n"
                            "留空 = 沿用云端默认文案。"
                        ),
                    },
                ),
                "negative": (
                    "STRING",
                    {
                        "multiline": True,
                        "default": "",
                        "placeholder": "negative",
                        "tooltip": (
                            "负面提示词。自动替换云端工作流文本里的 {negative} 标记；\n"
                            "也可以在上方参数映射里用 {negative} 占位符精确指定注入位置。\n"
                            "留空 = 沿用云端默认文案。"
                        ),
                    },
                ),
                "seed": ("INT", {"default": 0, "min": -1, "max": 0xFFFFFFFFFFFFFFFF, "step": 1}),
                "timeout": ("INT", {"default": 600, "min": 60, "max": 3600, "step": 10}),
                "poll_interval": ("FLOAT", {"default": 5.0, "min": 1.0, "max": 60.0, "step": 0.5}),
                "use_cache": ("BOOLEAN", {"default": True}),
                "use_high_performance": ("BOOLEAN", {"default": False}),
                "validate_mapping": ("BOOLEAN", {"default": True}),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("LATENT", "STRING", "STRING")
    RETURN_NAMES = ("latent", "task_id", "report")
    FUNCTION = "execute"
    CATEGORY = CATEGORY
    TITLE = "RH 云端采样 · 专业版"

    @classmethod
    def IS_CHANGED(cls, **kwargs) -> float:
        if int(kwargs.get("seed", 0) or 0) < 0:
            return float("nan")
        return float(hash(cache_mod.canonical_dumps(kwargs)) & 0xFFFFFFFF)

    @classmethod
    def VALIDATE_INPUTS(cls, **kwargs) -> bool:
        return True

    def execute(
        self,
        api_key: str,
        workflow_id: str,
        node_overrides: Any,
        positive: str,
        negative: str,
        seed: int,
        timeout: int,
        poll_interval: float,
        use_cache: bool,
        use_high_performance: bool,
        validate_mapping: bool = True,
        unique_id: Any = None,
    ) -> Tuple[Dict[str, torch.Tensor], str, str]:
        api_key = _resolve_api_key(api_key)
        workflow_id = _resolve_workflow_id(workflow_id)
        client = _make_client(api_key)

        effective_seed = random.randint(0, 0xFFFFFFFFFFFFFFFF) if seed < 0 else int(seed)
        overrides = _normalize_overrides(node_overrides)
        # 判「映射里有没有指定提示词落点」必须看**替换前**的原文：
        # 用户写的是 "{positive}"，替换后已经变成提示词文本，再查就永远查不到。
        raw_overrides_json = json.dumps(overrides, ensure_ascii=False)
        resolved_overrides = _apply_placeholders(
            overrides, {"seed": effective_seed, "positive": positive, "negative": negative}
        )

        warnings: List[str] = []

        # 第一步：自动注入（与极简版同一套占位符替换逻辑）
        injections, structure, stats, seed_targets = _auto_injections(
            client, workflow_id, positive, negative, effective_seed
        )
        if not structure:
            _warn_missing_structure(warnings)

        # 第二步：手工映射叠加在自动注入之上，同字段以手工映射为准
        node_info_list = _merge_injections(injections, resolved_overrides)

        # 提示词红线：填了提示词，但云端没有占位符、映射里也没指定落点 → **报错**
        # （自动沿连线猜测落点的能力已推倒，继续跑就是静默失效）
        _require_prompt_targets(
            structure, stats, positive, negative, raw_overrides_json
        )

        # 种子：自动定位没找到、手工映射也没管 → 提示会重复出图
        overrides_touch_seed = any(
            str(o.get("fieldName", "")).lower() == "seed" for o in resolved_overrides
        )
        if structure and not seed_targets and not overrides_touch_seed:
            warnings.append(
                "云端工作流里没找到可注入的随机种子字段，本次沿用云端默认种子"
                "（可能重复出同一张图）。如需指定种子，请在参数映射里手动加 seed 字段。"
            )
        if len(seed_targets) > 1:
            warnings.append(
                f"云端检测到 {len(seed_targets)} 个采样器，已统一注入同一个种子。"
            )

        # 空提示词 + 云端有占位符 = 会把云端提示词换成空内容，必须说出来
        _warn_empty_prompt_vs_placeholder(
            structure,
            positive,
            negative,
            "如果你在提示词输入框前面接了文本处理节点：请确认上游节点确实输出了内容，"
            "并且接的是它的文本输出口而不是 help/说明类输出口；"
            "注意输入框一旦连上节点，框里手写的文字就不再生效，值完全由连线决定。",
            warnings,
        )

        # 映射合法性校验：结构已经在自动注入时拉过一次，直接复用，不再多打一次接口
        if validate_mapping and resolved_overrides and structure:
            warnings.extend(wf_mod.validate_injections(structure, node_info_list))

        result, task_id, lines = _run_sampling(
            client,
            workflow_id,
            node_info_list,
            effective_seed,
            float(timeout),
            float(poll_interval),
            use_cache,
            use_high_performance,
            warnings,
            unique_id,
            structure=structure,
        )
        # 回显真正送进云端的提示词——排查「提示词不生效」最快的一眼定位点
        previews: List[str] = []
        if str(positive or "").strip():
            previews.append(f"本次提交的正面提示词：{_preview(positive)}")
        if str(negative or "").strip():
            previews.append(f"本次提交的负面提示词：{_preview(negative)}")
        if previews:
            lines = lines[:1] + previews + lines[1:]
        return (result, task_id, "\n".join(lines))


# ---------------------------------------------------------------------------
# 节点 3：按 taskId 取回
# ---------------------------------------------------------------------------


class RunningHubLatentFetch:
    """用已有的 taskId 重新拉取结果 —— 用于超时后续取或换会话复用。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "api_key": (
                    "STRING",
                    {
                        "multiline": False,
                        "default": "",
                        "placeholder": "留空则读取 config.ini / config.json",
                    },
                ),
                "task_id": (
                    "STRING",
                    {"multiline": False, "default": "", "placeholder": "上一次生成的任务号"},
                ),
                "timeout": ("INT", {"default": 600, "min": 60, "max": 3600, "step": 10}),
                "poll_interval": ("FLOAT", {"default": 5.0, "min": 1.0, "max": 60.0, "step": 0.5}),
            }
        }

    RETURN_TYPES = ("LATENT", "STRING")
    RETURN_NAMES = ("latent", "report")
    FUNCTION = "execute"
    CATEGORY = CATEGORY
    TITLE = "RH 按任务号取回 Latent"

    def execute(
        self, api_key: str, task_id: str, timeout: int, poll_interval: float
    ) -> Tuple[Dict[str, torch.Tensor], str]:
        task_id = str(task_id).strip()
        if not task_id:
            raise RHUserError("task_id 为空。请填入上一次生成的任务号。")

        api_key = _resolve_api_key(api_key)
        client = _make_client(api_key)

        outputs = client.wait_for_outputs(
            task_id, poll_interval=float(poll_interval), timeout=float(timeout)
        )
        if not outputs:
            raise RHUserError(
                f"任务 {task_id} 已返回成功但拿不到输出文件，可能已过期被清理。"
            )
        result, urls, _ = _resolve_outputs_to_latent(outputs, client=client)
        report = (
            f"已从云端任务 {task_id} 取回 latent。\n"
            f"下载地址：{chr(10).join('  ' + u for u in urls)}\n"
            f"张量形状：{_describe_samples(result.get('samples'))}"
        )
        return (result, report)


# ---------------------------------------------------------------------------
# 节点 4：直连 URL
# ---------------------------------------------------------------------------


class RunningHubLatentFromUrl:
    """直接给一个 latent 文件 URL 就加载 —— 适合手动从 RunningHub 下载的场景。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "url": (
                    "STRING",
                    {"multiline": False, "default": "", "placeholder": "latent 文件的直链"},
                ),
                "timeout": ("INT", {"default": 120, "min": 10, "max": 3600, "step": 10}),
            }
        }

    RETURN_TYPES = ("LATENT", "STRING")
    RETURN_NAMES = ("latent", "report")
    FUNCTION = "execute"
    CATEGORY = CATEGORY
    TITLE = "RH 从 URL 加载 Latent"

    def execute(self, url: str, timeout: int) -> Tuple[Dict[str, torch.Tensor], str]:
        url = str(url).strip()
        if not url:
            raise RHUserError("URL 为空。")
        result = _load_latent(_fetch_latent_blob(url, timeout=float(timeout)), url)
        return (result, f"已加载 {url}\n张量形状：{_describe_samples(result.get('samples'))}")


# ---------------------------------------------------------------------------
# 节点 5：工作流参数探查
# ---------------------------------------------------------------------------


class RunningHubWorkflowInspector:
    """从云端拉取工作流里所有可修改的节点与字段，免去手动翻网页抄 nodeId。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "api_key": (
                    "STRING",
                    {
                        "multiline": False,
                        "default": "",
                        "placeholder": "留空则读取 config.ini / config.json",
                    },
                ),
                "workflow_id": (
                    "STRING",
                    {"multiline": False, "default": "", "placeholder": "RunningHub 工作流 ID"},
                ),
            }
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("report",)
    FUNCTION = "execute"
    OUTPUT_NODE = True
    CATEGORY = CATEGORY
    TITLE = "RH 工作流参数探查"

    def execute(self, api_key: str, workflow_id: str) -> Tuple[str]:
        api_key = _resolve_api_key(api_key)
        workflow_id = _resolve_workflow_id(workflow_id)

        client = _make_client(api_key)
        structure = client.get_workflow_structure(workflow_id)

        if not structure:
            return (
                f"未能读取工作流 {workflow_id} 的结构。\n"
                "请确认：\n"
                "  1. 工作流 ID 正确（从 runninghub.cn/workflow/<ID> 的 URL 里取）；\n"
                "  2. 工作流已经保存过（草稿状态的工作流无法被调用）；\n"
                "  3. API Key 有该工作流的调用权限。\n"
                "若仍失败，可在 RunningHub 工作台点『导出 API』手动查看节点清单。",
            )

        body = wf_mod.describe(structure)
        templates = wf_mod.mapping_templates(structure)
        lines = [
            f"工作流 {workflow_id} 的可修改参数清单：",
            "（把需要的 nodeId / fieldName 抄进参数映射即可）",
            "",
            body,
        ]
        if templates:
            lines.extend(
                [
                    "",
                    "── 可直接复制的参数映射片段 ──",
                    "（复制进参数映射框，只改 fieldValue；标 × 的是连线字段，不能注入）",
                    "",
                    templates,
                ]
            )
        lines.extend(["", "── 自动定位检查 ──"])

        # 直接告诉用户：这条工作流能不能被极简版零配置使用
        injections, stats = wf_mod.build_prompt_injections(structure, "X", "X")
        seeds = wf_mod.find_seed_targets(structure)
        lines.append(
            f"提示词占位符：{{positive}} 命中 {stats['positive']} 处，"
            f"{{negative}} 命中 {stats['negative']} 处"
        )
        if stats["positive"] or stats["negative"]:
            lines.append("  → 本插件会自动替换这些占位符（提示词无需手写落点）。")
        else:
            lines.append(
                "  → 提示词没有自动落点：本节点不沿连线猜测，请在云端文本节点的文本框里"
                "写上 {positive} / {negative} 标记，或在参数映射里手动指定 nodeId。"
            )
        lines.append(
            f"随机种子：命中 {len(seeds)} 个可注入字段"
            + (f"（{'、'.join(f'nodeId={n}.{f}' for n, f in seeds)}）" if seeds else "")
        )
        lines.append("")
        lines.append(
            "参数映射示例：\n"
            '[{"nodeId": "这里填上面抄的 nodeId", '
            '"fieldName": "上面抄的 fieldName", '
            '"fieldValue": "{seed}"}]'
        )
        return ("\n".join(lines),)


# ---------------------------------------------------------------------------
# 节点 6-8：音视频复合 latent（MiniMax H3）
# ---------------------------------------------------------------------------
#
# 背景：MiniMax H3 这类「一次生成视频+音频」的模型，它的 latent 不是单个张量，
# 而是 comfy.nested_tensor.NestedTensor((视频 latent, 音频 latent))。
#
# 原生 SaveLatent 里有一行 samples["samples"].contiguous()，而 NestedTensor 是
# 纯 Python 容器、根本没有 contiguous 方法，所以直接报
#   AttributeError: 'NestedTensor' object has no attribute 'contiguous'
# 这不是接线错误，是原生节点没覆盖这种数据结构。
#
# 下面三个节点就是为此准备的：保存（替代原生 SaveLatent）、拆分（分别解码）、
# 合并（重新组装回 NestedTensor）。


class RunningHubAVLatentSplit:
    """把音视频复合 latent 拆成视频 / 音频两条，供各自的 VAE 解码。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"samples": ("LATENT",)}}

    RETURN_TYPES = ("LATENT", "LATENT")
    RETURN_NAMES = ("video", "audio")
    FUNCTION = "split"
    CATEGORY = CATEGORY
    TITLE = "RH AV Latent 拆分"
    DESCRIPTION = (
        "把 MiniMax H3 的音视频复合 latent 拆成视频 latent 和音频 latent。\n"
        "视频接 VAEDecode（H3 视频 VAE），音频接 VAEDecodeAudio（H3 音频 VAE）。\n"
        "原生 VAEDecode 遇到复合 latent 时只解视频、静默丢弃音频，所以必须拆开。"
    )

    def split(self, samples: dict) -> Tuple[dict, dict]:
        inner = (samples or {}).get("samples")
        if not latent_mod.is_nested(inner):
            raise RHUserError(
                "输入的不是音视频复合 latent（NestedTensor），无需拆分。\n"
                "普通工作流的 latent 是单个张量，直接接 VAEDecode 即可。\n"
                "如果你确实在跑 MiniMax H3，请检查上游是否真的是 H3 采样器的输出。"
            )
        parts = list(inner.unbind())
        if len(parts) < 2:
            raise RHUserError(
                f"复合 latent 里只有 {len(parts)} 个张量，无法拆出音频。\n"
                "请确认上游是 H3 采样器的输出，而不是被其它节点改写过。"
            )
        video, audio = parts[0], parts[1]
        noise_mask = (samples or {}).get("noise_mask")
        out_video: Dict[str, Any] = {"samples": video}
        out_audio: Dict[str, Any] = {"samples": audio}
        if noise_mask is not None:
            out_video["noise_mask"] = noise_mask
        return (out_video, out_audio)


class RunningHubAVLatentMerge:
    """把视频 latent + 音频 latent 重新组装成音视频复合 latent。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {"video": ("LATENT",)},
            "optional": {"audio": ("LATENT",)},
        }

    RETURN_TYPES = ("LATENT",)
    RETURN_NAMES = ("samples",)
    FUNCTION = "merge"
    CATEGORY = CATEGORY
    TITLE = "RH AV Latent 合并"
    DESCRIPTION = (
        "把视频 latent 与音频 latent 组装回音视频复合 latent（NestedTensor）。\n"
        "用于需要完整复合 latent 的下游节点（例如 H3 latent 放大 / 二次采样）。\n"
        "若只是为了分别解码，用「RH AV Latent 拆分」即可，不需要合并。"
    )

    def merge(self, video: dict, audio: Optional[dict] = None) -> Tuple[dict]:
        v = (video or {}).get("samples")
        if v is None:
            raise RHUserError("video 输入为空。")
        if audio is None:
            return (video,)
        a = (audio or {}).get("samples")
        if a is None:
            return (video,)
        merged: Dict[str, Any] = {"samples": latent_mod.make_nested(v, a)}
        return (merged,)


class RunningHubAVLatentSave:
    """替代原生 SaveLatent —— 能存原生节点存不了的音视频复合 latent。"""

    def __init__(self):
        self.output_dir = None
        if folder_paths is not None:
            try:
                self.output_dir = folder_paths.get_output_directory()
            except Exception:  # pragma: no cover
                self.output_dir = None

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "samples": ("LATENT",),
                "filename_prefix": ("STRING", {"default": "latents/MiniMaxH3"}),
            },
            "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"},
        }

    RETURN_TYPES = ("LATENT",)
    RETURN_NAMES = ("samples",)
    FUNCTION = "save"
    OUTPUT_NODE = True
    CATEGORY = CATEGORY
    TITLE = "RH AV Latent 保存"
    DESCRIPTION = (
        "保存音视频复合 latent（MiniMax H3）。原生 SaveLatent 遇到这类 latent 会报\n"
        "'NestedTensor' object has no attribute 'contiguous'，本节点用来替代它。\n"
        "扩展名沿用 .latent，云端平台才会把它当作 latent 输出收录到任务结果里。\n"
        "普通单张量 latent 也能存（此时等价于原生 SaveLatent）。"
    )

    def save(
        self,
        samples: dict,
        filename_prefix: str = "latents/MiniMaxH3",
        prompt=None,
        extra_pnginfo=None,
    ):
        if self.output_dir is None:
            raise RHUserError(
                "拿不到 ComfyUI 输出目录（folder_paths 不可用）。请确认本节点运行在 ComfyUI 内。"
            )
        inner = (samples or {}).get("samples")
        if inner is None:
            raise RHUserError("输入的 latent 为空。")

        full_folder, filename, counter, subfolder, _ = folder_paths.get_save_image_path(
            filename_prefix, self.output_dir
        )
        file = f"{filename}_{counter:05}_.latent"
        path = os.path.join(full_folder, file)

        metadata = {}
        if prompt is not None:
            metadata["prompt"] = json.dumps(prompt)
        for key, value in (extra_pnginfo or {}).items():
            metadata[str(key)] = json.dumps(value)

        if latent_mod.is_nested(inner):
            parts = list(inner.unbind())
            if len(parts) < 2:
                raise RHUserError(
                    f"复合 latent 只有 {len(parts)} 个张量，至少需要视频+音频两个。"
                )
            latent_mod.save_av_latent(path, parts[0], parts[1], metadata=metadata)
        else:
            # 单张量：按原生 SaveLatent 的格式写，保证 LoadLatent 也能读
            payload = {
                "latent_tensor": inner.detach().to("cpu").contiguous(),
                "latent_format_version_0": torch.tensor([]),
            }
            safetensors.torch.save_file(payload, path, metadata=metadata)

        return {
            "ui": {
                "latents": [{"filename": file, "subfolder": subfolder, "type": "output"}]
            },
            "result": (samples,),
        }


NODE_CLASS_MAPPINGS = {
    "RunningHubLatentGeneratorSimple": RunningHubLatentGeneratorSimple,
    "RunningHubLatentGenerator": RunningHubLatentGenerator,
    "RunningHubLatentFetch": RunningHubLatentFetch,
    "RunningHubLatentFromUrl": RunningHubLatentFromUrl,
    "RunningHubWorkflowInspector": RunningHubWorkflowInspector,
    "RunningHubAVLatentSplit": RunningHubAVLatentSplit,
    "RunningHubAVLatentMerge": RunningHubAVLatentMerge,
    "RunningHubAVLatentSave": RunningHubAVLatentSave,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    name: getattr(cls, "TITLE", name) for name, cls in NODE_CLASS_MAPPINGS.items()
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "CATEGORY"]
