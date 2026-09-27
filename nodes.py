"""
ComfyUI 节点定义。

## 关于参数注入（本插件最关键的设计决策）

RunningHub 的参数注入接口是：

    nodeInfoList = [{"nodeId": "6", "fieldName": "text", "fieldValue": "1 girl"}]

也就是说，**必须知道云端工作流里每个节点的 nodeId**。本地插件不可能凭空猜出来，
所以像「节点上直接摆 width/steps/cfg 滑块」这种设计在技术上不成立——除非约束用户
必须复刻某一条特定工作流，那样就失去了通用性。

本插件的解法是：**通用 JSON 注入 + 占位符绑定**

    [
      {"nodeId": "6", "fieldName": "text", "fieldValue": "{positive}"},
      {"nodeId": "7", "fieldName": "text", "fieldValue": "{negative}"},
      {"nodeId": "3", "fieldName": "seed",  "fieldValue": "{seed}"},
      {"nodeId": "3", "fieldName": "steps", "fieldValue": "20"}
    ]

凡是字符串 `{xxx}` 都会被替换为同名 widget 的值。这样既能覆盖任意工作流，又能像
原生节点一样在上面拖滑块调 seed / 倍数 Change。

配合「RH 工作流参数探查」节点，可以从云端直接拉取 nodeId 清单，免去手抄。
"""

from __future__ import annotations

import json
import random
import re
from typing import Any, Dict, List, Optional, Tuple

import torch

from .core import cache as cache_mod
from .core import client as client_mod
from .core import latent as latent_mod
from .core import settings as settings_mod

CATEGORY = "RunningHub"

# 常见的输出文件类型归类，用于从 outputs 列表里挑出 latent
_LATENT_HINTS = ("latent", "safetensor")
_VIDEO_EXT = (".mp4", ".webm", ".mov", ".avi", ".gif")
_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp")

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


class RHUserError(RuntimeError):
    """面向用户的中文错误，ComfyUI 会完整展示在前端，不做英文堆栈。"""


# ---------------------------------------------------------------------------
# 辅助
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


def _emit(node_id: str, text: str) -> None:
    """在 ComfyUI 控制台留痕。节点逻辑里的大量等待必须有可见反馈。"""
    print(f"[RunningHub-Latent/{node_id}] {text}")


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
                f"参数注入 JSON 格式有误：第 {exc.lineno} 行第 {exc.colno} 列 —— {exc.msg}。"
                f"请检查是否漏了引号或多了逗号，标准格式形如："
                f'[{{"nodeId": "3", "fieldName": "seed", "fieldValue": "123"}}]'
            ) from exc

    # 允许 {"nodeId": {"fieldName": value}} 的字典简写
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
            raise RHUserError(f"参数注入每一项都必须是对象，实际收到：{item!r}")
        node_id = item.get("nodeId") or item.get("node_id")
        field_name = item.get("fieldName") or item.get("field_name")
        # 官方文档反复强调必须是 fieldValue，这里直接帮用户纠正写法
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
                f"参数注入缺少 fieldValue 字段：{item}。"
                f"正确格式：{{'nodeId': '{node_id}', 'fieldName': '{field_name}', 'fieldValue': '你的值'}}"
            )
        if node_id is None or field_name is None:
            raise RHUserError(f"参数注入缺少 nodeId 或 fieldName：{item}")
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
        整值替换若退化成字符串，云端收到的 seed 会是 "42 而不是 42，
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
                        f"参数注入里引用了未知占位符 {{{name}}}。"
                        f"当前可用占位符：{', '.join(sorted(values))}"
                    )
                value = values[name]
            else:
                def _sub(match: "re.Match[str]") -> str:
                    name = match.group(1)
                    if name not in values:
                        raise RHUserError(
                            f"参数注入里引用了未知占位符 {{{name}}}。"
                            f"当前可用占位符：{', '.join(sorted(values))}"
                        )
                    return str(values[name])

                value = _PLACEHOLDER.sub(_sub, value)
        resolved.append(
            {"nodeId": item["nodeId"], "fieldName": item["fieldName"], "fieldValue": value}
        )
    return resolved


def _classify(item: Dict[str, Any]) -> str:
    """判断一个输出文件是 latent / image / video / unknown。"""
    file_type = str(item.get("fileType", "") or "").lower()
    url = str(item.get("fileUrl", "") or "").lower()
    # 去掉查询串再取扩展名
    url = url.split("?", 1)[0]

    if any(h in file_type for h in _LATENT_HINTS) or url.endswith((".latent", ".safetensors")):
        return "latent"
    if url.endswith(_VIDEO_EXT) or file_type in ("mp4", "webm"):
        return "video"
    if url.endswith(_IMAGE_EXT) or file_type in ("png", "jpg", "jpeg", "webp"):
        return "image"
    return "unknown"


def _collect_latents(outputs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """筛出所有 latent 输出并按稳定顺序排序。找不到时给出可操作的中文指引。"""
    candidates = [o for o in outputs if _classify(o) == "latent"]
    if not candidates:
        summary: List[str] = []
        for item in outputs:
            kind = _classify(item)
            summary.append(
                f"  - [{kind}] {item.get('fileUrl', '')} (nodeId={item.get('nodeId', '?')})"
            )
        detail = "\n".join(summary) if summary else "  （云端未返回任何输出文件）"

        raise RHUserError(
            "云端任务已完成，但没有产出 latent 文件。\n"
            f"本次云端实际输出：\n{detail}\n\n"
            "请检查云端工作流（重点）：\n"
            "  1. 工作流末端必须接「保存Latent」节点，而不是「保存图像」；\n"
            "  2. 「保存Latent」节点要能真正被执行到（不能被下游节点短路）；\n"
            "  3. 若云端同时保留了「保存图像」，建议删除，以避免不必要的额度消耗。"
        )

    # 多输出时按 nodeId / URL 稳定排序，保证重复运行结果一致
    candidates.sort(key=lambda o: (str(o.get("nodeId", "")), str(o.get("fileUrl", ""))))
    return candidates


def _merge_latents(
    tensors: List[torch.Tensor], shapes_ok: bool = True
) -> torch.Tensor:
    """把多个输出的 latent 沿 batch 维合并。

    形状一致时合并成一个 batch（这也正是 ComfyUI 原生 SaveLatent 多图输出的语义）；
    形状不一致时不做猜测，直接取第一个并交由上层提示。
    """
    if len(tensors) == 1:
        return tensors[0]

    first_shape = tuple(tensors[0].shape[1:])
    if all(tuple(t.shape[1:]) == first_shape for t in tensors[1:]):
        return torch.cat(tensors, dim=0)

    print(
        "[RunningHub-Latent] 警告：云端输出了多个尺寸的 latent，无法沿 batch 维合并，"
        "本次仅取第一个。请检查云端工作流是否混接了多个分辨率的生成分支。"
    )
    return tensors[0]


def _fetch_latent_blob(url: str, timeout: float = 120.0) -> bytes:
    return cache_mod.download(url, timeout=timeout)


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
    outputs: List[Dict[str, Any]], cache_key: Optional[str] = None
) -> Tuple[dict, List[str], Optional[str]]:
    """下载所有 latent 输出并合并成一个 ComfyUI LATENT。

    返回 (latent, 已下载的 URL 列表, 缓存文件路径或 None)。
    """
    items = _collect_latents(outputs)
    tensors: List[torch.Tensor] = []
    urls: List[str] = []
    stored: Optional[str] = None

    for item in items:
        url = str(item.get("fileUrl", ""))
        _emit("download", f"正在下载 latent：{url}")
        blob = _fetch_latent_blob(url)
        # 多文件时这批输出无法用单一 key 表达，仅单文件场景落盘缓存
        if cache_key and len(items) == 1:
            stored = cache_mod.store(cache_key, blob)
        tensors.append(_load_latent(blob, url)["samples"])
        urls.append(url)

    return ({"samples": _merge_latents(tensors)}, urls, stored)


# ---------------------------------------------------------------------------
# 节点 1：主生成器
# ---------------------------------------------------------------------------


class RunningHubLatentGenerator:
    """提交云端任务 -> 轮询 -> 下载 latent -> 输出标准 LATENT。

    云端只负责采样，VAE 解码全程在本地，因此必须连样本地的 VAEDecode /
    VAEDecodeTiled 才能得到画面。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "api_key": ("STRING", {"multiline": False, "default": "", "placeholder": "留空则读取 config.json"}),
                "workflow_id": ("STRING", {"multiline": False, "default": "", "placeholder": "RunningHub 工作流 ID"}),
                "node_overrides": (
                    "STRING",
                    {
                        "multiline": True,
                        "default": "[]",
                        "placeholder": '[{"nodeId": "3", "fieldName": "seed", "fieldValue": "{seed}"}]',
                    },
                ),
                "positive": ("STRING", {"multiline": True, "default": "", "placeholder": "配合 {positive} 占位符使用"}),
                "negative": ("STRING", {"multiline": True, "default": "", "placeholder": "配合 {negative} 占位符使用"}),
                "seed": ("INT", {"default": 0, "min": -1, "max": 0xFFFFFFFFFFFFFFFF, "step": 1}),
                "timeout": ("INT", {"default": 600, "min": 60, "max": 3600, "step": 10}),
                "poll_interval": ("FLOAT", {"default": 5.0, "min": 1.0, "max": 60.0, "step": 0.5}),
                "use_cache": ("BOOLEAN", {"default": True}),
                "use_high_performance": ("BOOLEAN", {"default": False}),
            }
        }

    RETURN_TYPES = ("LATENT", "STRING", "STRING")
    RETURN_NAMES = ("latent", "task_id", "report")
    FUNCTION = "execute"
    CATEGORY = CATEGORY
    TITLE = "RH 云端采样 · 输出 Latent"

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        # seed < 0 表示「每次抽新种子」，必须强制重算，否则会得到重复画面
        if int(kwargs.get("seed", 0) or 0) < 0:
            return float("nan")
        # 其余情况返回输入指纹：ComfyUI 会跳过完全相同的重算，
        # 节点内部的 latent 缓存则保证即使重算也不会重复扣费
        return cache_mod.canonical_dumps(kwargs)

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
    ) -> Tuple[Dict[str, torch.Tensor], str, str]:
        api_key = settings_mod.resolve(api_key, "api_key", "")
        workflow_id = settings_mod.resolve(workflow_id, "default_workflow_id", "")

        base_url = str(settings_mod.get("base_url", client_mod.DEFAULT_BASE_URL))
        client = client_mod.RunningHubClient(api_key=api_key, base_url=base_url, timeout=30.0)

        overrides = _normalize_overrides(node_overrides)
        effective_seed = random.randint(0, 0xFFFFFFFFFFFFFFFF) if seed < 0 else int(seed)
        node_info_list = _apply_placeholders(
            overrides, {"seed": effective_seed, "positive": positive, "negative": negative}
        )

        cache_key = cache_mod.compute_cache_key(workflow_id, node_info_list)
        if use_cache:
            hit = cache_mod.cache_path_for(cache_key)
            if hit:
                _emit("generator", f"命中本地缓存，跳过云端调用：{hit}")
                with open(hit, "rb") as fh:
                    blob = fh.read()
                result = _load_latent(blob, hit)
                report = (
                    f"命中本地缓存，未消耗云端额度。\n"
                    f"工作流 ID：{workflow_id}\n缓存文件：{hit}\n"
                    f"张量形状：{tuple(result['samples'].shape)}"
                )
                return (result, "cached", report)

        task_id = client.create_task(
            workflow_id,
            node_info_list,
            instance_type="plus" if use_high_performance else None,
        )
        _emit("generator", f"任务已提交，taskId={task_id}")

        pbar = _make_progress(4)

        def _on_progress(label: str, elapsed: float) -> None:
            pbar.update(1)
            _emit("generator", f"{label}（已用时 {elapsed:.0f} 秒）")

        pbar.update(1)
        outputs = client.wait_for_outputs(
            task_id,
            poll_interval=float(poll_interval),
            timeout=float(timeout),
            on_progress=_on_progress,
        )
        pbar.update(1)
        result, urls, stored = _resolve_outputs_to_latent(
            outputs, cache_key if use_cache else None
        )
        shape = tuple(result["samples"].shape)
        kind = "视频 latent（5 维）" if len(shape) == 5 else "图像 latent（4 维）"

        other = [o for o in outputs if _classify(o) != "latent"]
        report_lines = [
            f"云端采样完成，本地已获得 {kind}。",
            f"taskId：{task_id}",
            f"工作流 ID：{workflow_id}",
            f"张量形状：{shape}（batch={shape[0]}）",
            f"生效随机种子：{effective_seed}",
        ]
        if len(urls) > 1:
            report_lines.append(
                f"云端共输出 {len(urls)} 个 latent，已沿 batch 维合并为 {shape[0]} 个结果。"
            )
        report_lines.append(
            f"latent 缓存路径：{stored}" if stored
            else "本次未写入本地缓存（多个 latent 输出时不缓存）。"
        )
        if other:
            report_lines.append(
                f"注意：云端还额外输出了 {len(other)} 个非 latent 文件，本次已忽略。"
                "建议到云端工作流删除这些输出节点，避免不必要的额度消耗。"
            )
        report_lines.append("下一步：把 latent 接到本地 VAEDecode / VAEDecodeTiled 即可出图。")

        pbar.update(1)
        return (result, task_id, "\n".join(report_lines))


# ---------------------------------------------------------------------------
# 节点 2：按 taskId 取回
# ---------------------------------------------------------------------------


class RunningHubLatentFetch:
    """用已有的 taskId 重新拉取结果 —— 用于超时后续取或换会话复用。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "api_key": ("STRING", {"multiline": False, "default": ""}),
                "task_id": ("STRING", {"multiline": False, "default": ""}),
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

        api_key = settings_mod.resolve(api_key, "api_key", "")
        base_url = str(settings_mod.get("base_url", client_mod.DEFAULT_BASE_URL))
        client = client_mod.RunningHubClient(api_key=api_key, base_url=base_url)

        outputs = client.wait_for_outputs(
            task_id, poll_interval=float(poll_interval), timeout=float(timeout)
        )
        result, urls, _ = _resolve_outputs_to_latent(outputs)
        shape = tuple(result["samples"].shape)
        report = (
            f"已从云端任务 {task_id} 取回 latent。\n"
            f"下载地址：{chr(10).join('  ' + u for u in urls)}\n"
            f"张量形状：{shape}"
        )
        return (result, report)


# ---------------------------------------------------------------------------
# 节点 3：直连 URL
# ---------------------------------------------------------------------------


class RunningHubLatentFromUrl:
    """直接给一个 latent 文件 URL 就加载 —— 适合手动从 RunningHub 下载的场景。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "url": ("STRING", {"multiline": False, "default": ""}),
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
        shape = tuple(result["samples"].shape)
        return (result, f"已加载 {url}\n张量形状：{shape}")


# ---------------------------------------------------------------------------
# 节点 4：工作流参数探查
# ---------------------------------------------------------------------------


class RunningHubWorkflowInspector:
    """从云端拉取工作流里所有可修改的节点与字段，免去手动翻网页抄 nodeId。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "api_key": ("STRING", {"multiline": False, "default": ""}),
                "workflow_id": ("STRING", {"multiline": False, "default": ""}),
            }
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("report",)
    FUNCTION = "execute"
    OUTPUT_NODE = True
    CATEGORY = CATEGORY
    TITLE = "RH 工作流参数探查"

    def execute(self, api_key: str, workflow_id: str) -> Tuple[str]:
        api_key = settings_mod.resolve(api_key, "api_key", "")
        workflow_id = settings_mod.resolve(workflow_id, "default_workflow_id", "")
        if not workflow_id:
            raise RHUserError("请填写 RunningHub 工作流 ID。")

        base_url = str(settings_mod.get("base_url", client_mod.DEFAULT_BASE_URL))
        client = client_mod.RunningHubClient(api_key=api_key, base_url=base_url)
        data = client.get_workflow_params(workflow_id)

        if not data or not isinstance(data, dict):
            return (
                f"未能读取工作流 {workflow_id} 的参数清单。\n"
                "请确认：\n"
                "  1. 工作流 ID 正确（从 runninghub.cn/workflow/<ID> 的 URL 里取）；\n"
                "  2. 工作流已经保存过（草稿状态的工作流无法被调用）；\n"
                "  3. API Key 有该工作流的调用权限。\n"
                "若仍失败，可在 RunningHub 工作台点『导出 API』手动查看节点清单。",
            )

        lines = [
            f"工作流 {workflow_id} 的可修改参数清单：",
            "（把需要的 nodeId / fieldName 抄进 node_overrides 即可）",
            "",
        ]
        for node_id in sorted(data.keys(), key=lambda x: str(x)):
            meta = data[node_id]
            if not isinstance(meta, dict):
                continue
            class_type = meta.get("class_type", meta.get("type", "?"))
            inputs = meta.get("inputs") or {}
            if not isinstance(inputs, dict):
                inputs = {}
            # 连线属性是上游节点引用，不是可注入的 widget，需要剔除
            fields = {
                k: v for k, v in inputs.items() if not (isinstance(v, list) and len(v) == 2)
            }
            if not fields:
                continue
            lines.append(f"● nodeId={node_id}  [{class_type}]")
            for name in sorted(fields.keys()):
                value = fields[name]
                shown = str(value)
                if len(shown) > 60:
                    shown = shown[:57] + "..."
                lines.append(f"    fieldName = {name}   当前值 = {shown}")

        lines.append("")
        lines.append(
            "注入示例：\n"
            '[{"nodeId": "这里填上面抄的 nodeId", '
            '"fieldName": "上面抄的 fieldName", '
            '"fieldValue": "{seed}"}]'
        )
        return ("\n".join(lines),)


NODE_CLASS_MAPPINGS = {
    "RunningHubLatentGenerator": RunningHubLatentGenerator,
    "RunningHubLatentFetch": RunningHubLatentFetch,
    "RunningHubLatentFromUrl": RunningHubLatentFromUrl,
    "RunningHubWorkflowInspector": RunningHubWorkflowInspector,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    name: getattr(cls, "TITLE", name) for name, cls in NODE_CLASS_MAPPINGS.items()
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "CATEGORY"]
