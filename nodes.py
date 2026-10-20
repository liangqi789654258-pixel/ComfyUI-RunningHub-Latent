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

import contextlib
import functools
import json
import os
import random
import re
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import torch

from .core import cache as cache_mod
from .core import client as client_mod
from .core import errors as errors_mod
from .core import latent as latent_mod
from .core import ledger as ledger_mod
from .core import mapping as mapping_mod
from .core import output as output_mod
from .core import settings as settings_mod
from .core import workflow as wf_mod

try:  # ComfyUI 提供；离线单测环境下允许缺失
    import folder_paths
except Exception:  # pragma: no cover
    folder_paths = None

CATEGORY = "RunningHub"

# 输出分类关键字 / 占位符正则已随实现下沉到 core/output.py 与 core/mapping.py，
# 这里不再重复定义（避免两处常量各改一半）。

# 前端自定义事件名，web/rh_key_guard.js 监听它来做节点变黄 / 变红
_WARNING_EVENT = "runninghub-latent-warning"
_ERROR_EVENT = "runninghub-latent-error"


# 面向用户的中文错误统一由 core.errors 生成：自带「问题类型 / 失败环节 /
# 关键上下文 / 排查步骤」四段式。这里保留同名导出，既有引用不用改。
RHUserError = errors_mod.RHUserError


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


def _send_error(unique_id: Any, message: str) -> None:
    """把诊断文案推给前端：对应节点变红并弹出常驻提示。

    ComfyUI 新版前端把节点异常统一显示成「节点在执行过程中发生错误」，
    我们精心写的报错在那儿根本出不来。所以除了 raise（日志里一定有），
    再走自己的事件通道送一份到画布上，保证不查日志也看得到。
    失败完全静默——这只是增强，不能因为它导致任务二次失败。
    """
    if not unique_id or not message:
        return
    try:  # pragma: no cover - ComfyUI 专有
        from server import PromptServer

        PromptServer.instance.send_sync(
            _ERROR_EVENT,
            {"node_id": str(unique_id), "message": str(message)},
        )
    except Exception:
        pass


def _report_error(node_title: str, err: BaseException, unique_id: Any = None) -> None:
    """报错三连：控制台留痕 + 前端弹窗 + 抛给 ComfyUI。"""
    text = str(err)
    _emit("error", f"{node_title} 失败\n{text}")
    _send_error(unique_id, text)


@contextlib.contextmanager
def _stage(node: str, stage: str, **context: Any) -> Iterator[None]:
    """给某一步骤套上「失败环节」标签。

    下层（client / cache）抛错时不知道用户点的是哪个节点、卡在哪一环，
    这里补上；未分类的异常（我们自己代码的 bug）则在这里被兜底分类，
    避免用户看到一句英文堆栈。
    """
    try:
        yield
    except RHUserError as exc:
        refined = exc.retitled(node=node, stage=stage, extra_context=context or None)
        if refined is exc:
            raise
        raise refined from exc
    except Exception as exc:  # noqa: BLE001 - 兜底：任何异常都要变成中文诊断
        raise errors_mod.explain(
            exc, node=node, stage=stage, context=context or None
        ) from exc


def _guarded(title: str):
    """节点执行入口的统一兜底：保证抛出去的一定是带分类的中文诊断。"""

    def _decorate(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            unique_id = kwargs.get("unique_id")
            try:
                return func(*args, **kwargs)
            except RHUserError as exc:
                final = exc.retitled(node=title)
                _report_error(title, final, unique_id)
                if final is exc:
                    raise
                raise final from exc
            except Exception as exc:  # noqa: BLE001 - 兜底
                final = errors_mod.explain(exc, node=title)
                _report_error(title, final, unique_id)
                raise final from exc

        return wrapper

    return _decorate


def _api_key_input() -> Tuple[str, Dict[str, Any]]:
    """api_key 输入声明（4 个节点共用一份，避免四处各写一遍）。

    语义已从「密钥输入框」变成「状态指示器」：
      * 默认值 = `settings_mod.api_key_hint()`，有 Key 时是
        「已配置 · 使用 config.ini」这个**无害标签**，没 Key 时是空串；
      * 真 Key 永远不经过这个框（右键节点 →「设置 API Key」写进 config.ini）；
        若用户仍然直接粘进来，前端会在失焦时转存并把框换回标签。
    这样画布上永远看得到当前状态，而工作流 JSON / 导出 / PNG 元数据里没有密钥。
    """
    return (
        "STRING",
        {
            "multiline": False,
            "default": settings_mod.api_key_hint(),
            "password": False,  # 值本身就是标签，需要可读；真 Key 不会留在这里
            "placeholder": settings_mod.api_key_placeholder(),
            "tooltip": (
                "这个框只显示 Key 的状态，不存密钥。\n"
                "有 Key 时显示「已配置 · 使用 config.ini」，执行时自动读取；\n"
                "要更换 Key：右键本节点 →「设置 API Key」。\n"
                "工作流文件 / 导出 / 图片元数据里永远不含 Key。"
            ),
        },
    )


def _resolve_api_key(raw: str) -> str:
    # 状态标签不是密钥：settings.resolve_api_key 会把它当未填，回落 config.ini
    key = settings_mod.resolve_api_key(raw)
    if not key:
        raise errors_mod.RHUserError.build(
            errors_mod.CAT_AUTH,
            "没有拿到 API Key：节点上没有配 Key，config.ini 里也没有。",
            stage="读取配置",
            context={
                "配置文件位置": settings_mod.ini_path(),
                "两种填法": "① 右键本节点 →「设置 API Key」（推荐，只写进 config.ini，"
                "不进工作流/导出/图片）；② 直接在 config.ini 里写 api_key=xxx。",
            },
        )
    return key


def _resolve_workflow_id(raw: str) -> str:
    wid = str(settings_mod.resolve(raw, "default_workflow_id", "") or "").strip()
    if not wid:
        raise errors_mod.RHUserError.build(
            errors_mod.CAT_WORKFLOW,
            "没有拿到 workflow_id：节点输入框是空的，配置文件里也没有。",
            stage="读取配置",
            context={
                "取值方法": "打开 runninghub.cn/workflow/<ID>，地址栏末尾那串数字就是 ID",
                "配置文件位置": settings_mod.ini_path(),
            },
        )
    # 用户经常把整段 URL 粘进来（甚至带 ?tab= 参数），直接帮他抽出来，
    # 比报一句「ID 不存在」友好得多
    match = re.search(r"/workflow/(\d+)", wid)
    if match:
        wid = match.group(1)
        _emit("input", f"已从 URL 中提取 workflow_id={wid}")
    return wid


def _make_client(api_key: str) -> "client_mod.RunningHubClient":
    base_url = str(settings_mod.get("base_url", client_mod.DEFAULT_BASE_URL))
    return client_mod.RunningHubClient(api_key=api_key, base_url=base_url, timeout=30.0)


# ---------------------------------------------------------------------------
# 参数注入解析
# ---------------------------------------------------------------------------


# 参数映射解析与占位符替换已下沉到 core/mapping.py（插件与出图器共用一份）。
_normalize_overrides = mapping_mod.normalize_overrides
_apply_placeholders = mapping_mod.apply_placeholders
_merge_injections = mapping_mod.merge_injections


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


# 输出识别已下沉到 core/output.py（插件与出图器共用一份）。
_output_type_of = output_mod.output_type_of
_classify = output_mod.classify
_collect_latents = output_mod.collect_latents
_merge_latents = output_mod.merge_latents


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
    """下载到的字节 -> ComfyUI LATENT。解析失败要说出「下下来的是什么」。"""
    try:
        return latent_mod.parse(blob, url)
    except latent_mod.LatentParseError as exc:
        raise errors_mod.RHUserError.build(
            errors_mod.CAT_PARSE,
            f"latent 文件下载成功但解析失败：{errors_mod.truncate(exc, 300)}",
            stage="解析 latent 文件",
            context={
                "下载地址": errors_mod.truncate(url, 200),
                "文件大小": f"{len(blob) / 1024:.0f} KB",
                "文件头猜测": _sniff_kind(blob),
            },
        ) from exc


def _sniff_kind(blob: bytes) -> str:
    """按文件头猜一下下载到的到底是什么，帮用户判断是不是下错了东西。"""
    if not blob:
        return "空文件（0 字节）"
    head = blob[:16]
    if latent_mod.looks_like_safetensors(blob):
        return "safetensors / .latent（符合预期）"
    if head[:4] == b"PK\x03\x04":
        return "ZIP 压缩包"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "PNG 图片 —— 云端末端接的是「保存图像」，请改成「保存Latent」"
    if head[:4] == bytes.fromhex("00000020") or head[4:8] == b"ftyp":
        return "MP4 视频 —— 云端末端接的是「保存视频」，请改成「保存Latent」"
    if head[:1] in (b"<", b"{"):
        return "HTML / JSON 文本 —— 多半是错误页或接口返回，不是 latent 文件"
    if head[:6] == b"NUMPY\x01":
        return "npy 数组"
    return f"未知格式（前 8 字节：{head[:8]!r}）"


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
        # 字段名必须给**真实的**那个：写死 "text" 会让人照抄后被 803 打回
        targets = wf_mod.suggest_text_targets(structure)
        if targets:
            best_id, best_field = targets[0]
            example = (
                f'  {{"nodeId": "{best_id}", "fieldName": "{best_field}", '
                f'"fieldValue": "{{{token}}}"}}'
            )
            example_note = (
                f"已按云端真实字段生成（候选第一个：nodeId={best_id}.{best_field}）；"
                f"若该节点不是提示词节点，请换成上面定位结果里列出的其它候选。"
            )
        else:
            example = (
                f'  {{"nodeId": "<ID>", "fieldName": "<字段名>", '
                f'"fieldValue": "{{{token}}}"}}'
            )
            example_note = "云端没有可写入的文本字段，请先到云端把提示词节点改成字面量输入。"

        raise errors_mod.RHUserError.build(
            errors_mod.CAT_MAPPING,
            f"{label}已填写，但云端工作流里没有 {{{token}}} 占位符，"
            f"参数映射里也没有引用 {{{token}}} —— 本次不会有任何提示词被送到云端，"
            "继续跑只会拿到云端默认文案（静默失效，比报错更糟）。",
            stage="定位提示词落点",
            context={
                "定位结果": reason,
                "可照抄的修法": example_note + "\n" + example,
                "另一个办法": f"在云端文本节点的文本框里写上 {{{token}}} 标记，"
                "本节点会自动替换。",
            },
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


def _prevalidate_injections(
    structure: Optional[Dict[str, Any]],
    node_info_list: List[Dict[str, Any]],
    warnings: List[str],
) -> None:
    """提交前本地预校验：把注定被 803 拒绝的映射拦在本地，不花云端钱试错。

    复用 workflow 层的逐条诊断（nodeId / fieldName 不存在 = 致命，直接报错；
    指向连线字段 = 注入不生效但平台不拦，降级为警告）。结构没读到时跳过。
    """
    if not structure or not node_info_list:
        return
    fatal, linked = wf_mod.split_injection_problems(structure, node_info_list)
    if fatal:
        raise errors_mod.RHUserError.build(
            errors_mod.CAT_MAPPING,
            f"参数映射里有 {len(fatal)} 条指向了云端工作流中不存在的落点，"
            "提交上去只会收到 803 错误（不发任务、但白折腾一轮）。",
            stage="提交前本地预校验",
            context={
                "问题明细": "；".join(fatal),
                "本次映射条数": len(node_info_list),
                "核对依据": "云端工作流结构（getJsonApiFormat）",
            },
            actions=(
                "用「RH 工作流参数探查」节点重新拉一次清单，"
                "核对 nodeId 与 fieldName 的真实拼写；",
                "若刚在云端改过工作流：改动必须先在平台保存，本地拉到的才是新结构。",
            ),
        )
    for problem in linked:
        warnings.append(f"参数映射警告：{problem}")


def _query_coins(client: "client_mod.RunningHubClient") -> Optional[float]:
    """查询当前积分余额。查询失败返回 None——积分显示是锦上添花，不能连累任务。"""
    try:
        data = client.get_account_status()
        raw = data.get("remainCoins", data.get("remain_coins"))
        if raw is None:
            return None
        return float(raw)
    except Exception as exc:
        print(f"[RunningHub-Latent] 积分查询失败（不影响任务）：{errors_mod.truncate(exc, 120)}")
        return None


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
    node: str = "",
    timeout_retries: int = 0,
    query_coins: bool = True,
) -> Tuple[Dict[str, torch.Tensor], str, List[str]]:
    """提交 -> 轮询 -> 下载 -> 组装报告。两个版本节点共用。

    `structure` 用于 803 的逐条定位 + 提交前的映射预校验。
    `node` 是节点显示名，只用于给报错补上「哪个节点、哪一环」。
    `timeout_retries` 是轮询超时后的自动续等次数（0 = 超时立即报错），
    应对「任务在排队、600 秒不够」的场景——已付费的任务不该被本地计时器判死。

    返回 (latent, task_id, report_lines)。
    """
    cache_key = cache_mod.compute_cache_key(workflow_id, node_info_list)

    if use_cache:
        hit = cache_mod.cache_path_for(cache_key)
        if hit:
            _emit("generator", f"命中本地缓存，跳过云端调用：{hit}")
            with open(hit, "rb") as fh:
                blob = fh.read()
            with _stage(node, "读取本地 latent 缓存", 缓存文件=hit):
                result = _load_latent(blob, hit)
            lines = [
                "命中本地缓存，本次未消耗云端额度。",
                f"工作流 ID：{workflow_id}",
                f"缓存文件：{hit}",
                f"张量形状：{_describe_samples(result.get('samples'))}",
            ]
            ledger_mod.append_record(
                {
                    "node": node,
                    "task_id": "cached",
                    "workflow_id": workflow_id,
                    "outcome": "cached",
                    "elapsed_s": 0,
                    "coins_spent": 0,
                    "latent_shape": _describe_samples(result.get("samples")),
                }
            )
            return (result, "cached", lines)

    # ---- 提交前准备：预校验 + 计时 + 积分基线 ------------------------------
    _prevalidate_injections(structure, node_info_list, warnings)

    t0 = time.time()
    coins_before = _query_coins(client) if query_coins else None

    pbar = _make_progress(5)

    def _on_stage(label: str) -> None:
        _emit("generator", label)

    _on_stage("提交中")
    with _stage(
        node,
        "提交任务到云端",
        工作流ID=workflow_id,
        参数注入条数=len(node_info_list),
    ):
        task_id = _submit_task(
            client, workflow_id, node_info_list, structure, use_high_performance
        )
    # 从这里起任务已经付费：之后任何失败都值得留台账（含 taskId 可续取）
    try:
        _emit("generator", f"任务已提交，taskId={task_id}")
        pbar.update(1)

        def _on_progress(label: str, elapsed: float) -> None:
            pbar.update(1)
            _emit("generator", f"{label}（已用时 {elapsed:.0f} 秒）")

        # 轮询 + 超时自动续等：排队 600 秒不稀奇，已付费的任务不该被本地计时器判死
        rounds_left = max(0, int(timeout_retries))
        while True:
            try:
                with _stage(
                    node,
                    "等待云端采样完成",
                    taskId=task_id,
                    轮询超时=f"{float(timeout):.0f} 秒"
                    + (f" × 最多 {rounds_left + 1} 轮" if rounds_left else ""),
                ):
                    outputs = client.wait_for_outputs(
                        task_id,
                        poll_interval=float(poll_interval),
                        timeout=float(timeout),
                        on_progress=_on_progress,
                    )
                break
            except client_mod.TaskTimeoutError as exc:
                if rounds_left <= 0:
                    raise
                rounds_left -= 1
                _emit(
                    "generator",
                    f"轮询超时，任务可能仍在排队，自动续等（剩余 {rounds_left} 次）："
                    f"taskId={task_id}",
                )
        pbar.update(1)

        if not outputs:
            # 状态已成功但补查后依然拿不到文件：任务钱已经花了，必须给出可挽回的路径
            raise errors_mod.RHUserError.build(
                errors_mod.CAT_CLOUD,
                f"云端任务 {task_id} 已返回成功，但反复查询都拿不到输出文件。",
                node=node,
                stage="等待云端采样完成",
                context={
                    "taskId": task_id,
                    "工作流 ID": workflow_id,
                    "可能原因": "① 云端工作流末端没有输出节点（既没接保存Latent 也没接保存图像）；"
                    "② 云端结果文件已过期被清理。",
                    "补救办法": "记住上面的 taskId，用「RH 按任务号取回」节点再试一次；"
                    "仍取不到就到 RunningHub 网页端看该任务的输出列表。",
                },
            )

        # 注意：这里不能叫 `_stage`——函数体内任何同名赋值都会把模块级
        # `_stage(node, stage, ...)` 上下文管理器遮蔽成局部名，前面的
        # `with _stage(...)` 就会 UnboundLocalError（此坑真实炸过一次）。
        def _announce(text: str) -> None:
            _emit("generator", text)
            pbar.update(1)

        with _stage(node, "下载并解析云端 latent 文件", taskId=task_id):
            result, urls, stored = _resolve_outputs_to_latent(
                outputs,
                cache_key if use_cache else None,
                client=client,
                on_stage=_announce,
            )
        elapsed_s = time.time() - t0
        shape = tuple(result["samples"].shape)
        kind = "视频 latent（5 维）" if len(shape) == 5 else "图像 latent（4 维）"
        if latent_mod.is_nested(result.get("samples")):
            kind = "音视频复合 latent（MiniMax H3 等）"

        # ---- 积分结算 ---------------------------------------------------------
        coins_after = _query_coins(client) if query_coins else None
        coins_spent: Optional[float] = None
        if coins_before is not None and coins_after is not None:
            coins_spent = round(coins_before - coins_after, 2)

        lines = [
            f"云端采样完成，本地已获得 {kind}。",
            f"taskId：{task_id}",
            f"工作流 ID：{workflow_id}",
            f"张量形状：{_describe_samples(result.get('samples'))}（batch={shape[0]}）",
            f"生效随机种子：{effective_seed}",
        ]
        if coins_spent is not None:
            line = f"本次积分消耗：约 {coins_spent:g}（任务前 {coins_before:g} → 任务后 {coins_after:g}）"
            if coins_spent < 0:
                line += "。负数说明期间账户还有其他任务在并发扣费，差值仅供参考。"
            lines.append(line)
        elif query_coins:
            lines.append("本次积分消耗：未能查询（接口失败或字段缺失），请到 RunningHub 控制台核对。")
        lines.append(f"整体用时：{elapsed_s:.0f} 秒（含提交、排队、采样、下载）。")
        if len(urls) > 1:
            lines.append(
                f"云端共输出 {len(urls)} 个 latent，已沿 batch 维合并为 {shape[0]} 个结果。"
            )
        lines.append(
            f"latent 缓存路径：{stored}" if stored
            else "本次未写入本地缓存（多个 latent 输出时不缓存）。"
        )
        if other := [o for o in outputs if _classify(o) != "latent"]:
            lines.append(
                f"注意：云端还额外输出了 {len(other)} 个非 latent 文件，本次已忽略。"
                "建议到云端工作流删除这些输出节点，避免不必要的额度消耗。"
            )
        lines.append("下一步：把 latent 接到本地 VAEDecode / VAEDecodeTiled 即可出图。")

        ledger_mod.append_record(
            {
                "node": node,
                "task_id": task_id,
                "workflow_id": workflow_id,
                "outcome": "success",
                "elapsed_s": round(elapsed_s, 1),
                "coins_spent": coins_spent,
                "coins_before": coins_before,
                "coins_after": coins_after,
                "latent_shape": f"{shape[0:2]}…",
                "seed": effective_seed,
            }
        )

        if warnings:
            lines.append("")
            lines.append(f"⚠️ 本次执行有 {len(warnings)} 条警告：")
            lines.extend(f"  ⚠️ {w}" for w in warnings)
            _send_warning(unique_id, warnings)

        pbar.update(1)
        _emit("generator", "完成")
        return (result, task_id, lines)
    except BaseException as exc:
        ledger_mod.append_record(
            {
                "node": node,
                "task_id": task_id,
                "workflow_id": workflow_id,
                "outcome": "failed",
                "elapsed_s": round(time.time() - t0, 1),
                "coins_before": coins_before,
                "error_category": getattr(exc, "category", "unknown"),
                "error_reason": errors_mod.truncate(str(exc), 200),
            }
        )
        raise


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


def _warn_missing_structure(
    warnings: List[str], client: Optional["client_mod.RunningHubClient"] = None
) -> None:
    """结构读不到时的降级警告。

    以前这里只有一句「没能读到结构」，用户无从判断是 Key 错了、ID 错了
    还是网络断了。现在把 client 记下的失败原因一并带上：错误码、服务端原话、
    以及按分类给出的排查方向。
    """
    head = "没能读到云端工作流结构，自动注入已跳过（提示词 / 种子将沿用云端默认值）。"
    err: Optional[errors_mod.RHUserError] = None
    if client is not None:
        try:
            err = client.last_structure_error()
        except Exception:  # pragma: no cover
            err = None

    if err is None:
        warnings.append(
            head
            + "\n请确认工作流 ID 正确且已保存（草稿状态无法被调用）；"
            "若持续失败，请在参数映射里手动填 nodeId / fieldName"
            "（用「RH 工作流参数探查」或在 RunningHub 网页编辑器里查 ID）。"
        )
        return

    category = getattr(err, "category", errors_mod.CAT_UNKNOWN)
    steps = errors_mod.ACTIONS.get(category) or errors_mod.ACTIONS[errors_mod.CAT_UNKNOWN]
    warnings.append(
        head
        + f"\n问题类型：{errors_mod.CAT_TITLES.get(category, errors_mod.CAT_TITLES[errors_mod.CAT_UNKNOWN])}"
        + f"\n失败原因：{errors_mod.truncate(err, 400)}"
        + "\n怎么排查：\n" + "\n".join(f"  {i}. {s}" for i, s in enumerate(steps[:3], 1))
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
                "api_key": _api_key_input(),
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

    # 报错里要写清「哪个节点失败」，这里用字面量与 TITLE 保持一致
    @_guarded("RH 云端采样 · 极简版")
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
        timeout_retries = int(float(settings_mod.get("timeout_retries", 1)))
        query_coins = str(settings_mod.get("query_coins", True)).strip().lower() not in (
            "false", "0", "no", "off",
        )
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

        with _stage("RH 云端采样 · 极简版", "读取云端工作流结构", 工作流ID=workflow_id):
            injections, _structure, _stats, seed_targets = _auto_injections(
                client, workflow_id, "", "", effective_seed
            )
        node_info_list = _merge_injections(injections, resolved_overrides)

        if not _structure:
            _warn_missing_structure(warnings, client)
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
            node="RH 云端采样 · 极简版",
            timeout_retries=timeout_retries,
            query_coins=query_coins,
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
                "api_key": _api_key_input(),
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
                "timeout_retries": (
                    "INT",
                    {
                        "default": 1,
                        "min": 0,
                        "max": 5,
                        "step": 1,
                        "tooltip": (
                            "轮询超时后的自动续等次数。0 = 超时立即报错；\n"
                            "1~5 = 每次超时自动再等一轮（等价于 timeout×(n+1)），\n"
                            "应对云端排队久、已付费的任务不该被本地计时器判死。"
                        ),
                    },
                ),
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

    @_guarded("RH 云端采样 · 专业版")
    def execute(
        self,
        api_key: str,
        workflow_id: str,
        node_overrides: Any,
        positive: str,
        negative: str,
        seed: int,
        timeout: int,
        timeout_retries: int = 1,
        poll_interval: float = 5.0,
        use_cache: bool = True,
        use_high_performance: bool = False,
        validate_mapping: bool = True,
        unique_id: Any = None,
    ) -> Tuple[Dict[str, torch.Tensor], str, str]:
        api_key = _resolve_api_key(api_key)
        workflow_id = _resolve_workflow_id(workflow_id)
        client = _make_client(api_key)
        query_coins = str(settings_mod.get("query_coins", True)).strip().lower() not in (
            "false", "0", "no", "off",
        )

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
        with _stage("RH 云端采样 · 专业版", "读取云端工作流结构", 工作流ID=workflow_id):
            injections, structure, stats, seed_targets = _auto_injections(
                client, workflow_id, positive, negative, effective_seed
            )
        if not structure:
            _warn_missing_structure(warnings, client)

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
            node="RH 云端采样 · 专业版",
            timeout_retries=int(timeout_retries),
            query_coins=query_coins,
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
                "api_key": _api_key_input(),
                "task_id": (
                    "STRING",
                    {"multiline": False, "default": "", "placeholder": "上一次生成的任务号"},
                ),
                "timeout": ("INT", {"default": 600, "min": 60, "max": 3600, "step": 10}),
                "timeout_retries": (
                    "INT",
                    {
                        "default": 1,
                        "min": 0,
                        "max": 5,
                        "step": 1,
                        "tooltip": (
                            "轮询超时后的自动续等次数。0 = 超时立即报错；\n"
                            "1~5 = 每次超时自动再等一轮（等价于 timeout×(n+1)），\n"
                            "应对云端排队久、已付费的任务不该被本地计时器判死。"
                        ),
                    },
                ),
                "poll_interval": ("FLOAT", {"default": 5.0, "min": 1.0, "max": 60.0, "step": 0.5}),
            }
        }

    RETURN_TYPES = ("LATENT", "STRING")
    RETURN_NAMES = ("latent", "report")
    FUNCTION = "execute"
    CATEGORY = CATEGORY
    TITLE = "RH 按任务号取回 Latent"

    @_guarded("RH 按任务号取回 Latent")
    def execute(
        self,
        api_key: str,
        task_id: str,
        timeout: int,
        timeout_retries: int = 1,
        poll_interval: float = 5.0,
    ) -> Tuple[Dict[str, torch.Tensor], str]:
        task_id = str(task_id).strip()
        if not task_id:
            raise errors_mod.RHUserError.build(
                errors_mod.CAT_INPUT,
                "task_id 是空的，没有可查询的任务。",
                node="RH 按任务号取回 Latent",
                context={
                    "取值方法": "task_id 是「RH 云端采样」节点第二个输出口（task_id）给的数字串；"
                    "超时报错里也会带上它，照抄过来即可续取结果。"
                },
            )

        api_key = _resolve_api_key(api_key)
        client = _make_client(api_key)

        rounds_left = max(0, int(timeout_retries))
        while True:
            try:
                with _stage("RH 按任务号取回 Latent", "按 taskId 查询云端结果", taskId=task_id):
                    outputs = client.wait_for_outputs(
                        task_id, poll_interval=float(poll_interval), timeout=float(timeout)
                    )
                break
            except client_mod.TaskTimeoutError:
                if rounds_left <= 0:
                    raise
                rounds_left -= 1
                _emit(
                    "generator",
                    f"轮询超时，任务可能仍在排队，自动续等（剩余 {rounds_left} 次）",
                )
        if not outputs:
            raise errors_mod.RHUserError.build(
                errors_mod.CAT_DOWNLOAD,
                f"任务 {task_id} 已返回成功，但拿不到输出文件。",
                node="RH 按任务号取回 Latent",
                stage="按 taskId 查询云端结果",
                context={
                    "taskId": task_id,
                    "最常见原因": "云端结果文件有有效期，过期会被清理，只能重跑一次任务。",
                    "其它可能": "该任务本来就没有输出节点；或 task_id 抄错了。",
                },
            )
        with _stage("RH 按任务号取回 Latent", "下载并解析云端 latent 文件", taskId=task_id):
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

    @_guarded("RH 从 URL 加载 Latent")
    def execute(self, url: str, timeout: int) -> Tuple[Dict[str, torch.Tensor], str]:
        url = str(url).strip()
        if not url:
            raise errors_mod.RHUserError.build(
                errors_mod.CAT_INPUT,
                "URL 是空的，没有可加载的文件。",
                node="RH 从 URL 加载 Latent",
                context={
                    "取值方法": "从 RunningHub 任务输出列表里右键复制 latent 文件的下载地址；"
                    "注意直链带签名且会过期（通常 14 天）。"
                },
            )
        with _stage("RH 从 URL 加载 Latent", "下载 latent 文件", 下载地址=url):
            blob = _fetch_latent_blob(url, timeout=float(timeout))
        result = _load_latent(blob, url)
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
                "api_key": _api_key_input(),
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
            # 探查节点本来就是「用来排查」的，拉不到结构时把诊断直接打在
            # 输出里比抛异常更好用——用户要的就是这份清单。
            reason: Optional[errors_mod.RHUserError] = None
            try:
                reason = client.last_structure_error()
            except Exception:  # pragma: no cover
                reason = None
            if reason is not None:
                return (f"未能读取工作流 {workflow_id} 的结构。\n\n{reason}",)
            return (
                f"未能读取工作流 {workflow_id} 的结构，且云端没有返回明确的错误码"
                "（通常是接口形态有变化）。\n"
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


def _human_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024 or unit == "GB":
            return f"{num_bytes:.1f} {unit}" if unit != "B" else f"{num_bytes:.0f} B"
        num_bytes /= 1024
    return f"{num_bytes:.1f} GB"


class RunningHubCacheManager:
    """查看 / 清理本插件的 latent 缓存，并回看最近任务的积分消耗台账。

    缓存命中 = 同参数重跑不花钱，所以默认 512MB 上限内自动保留；
    想立刻释放磁盘或强制下次重新生成时用「清理」。
    台账（runninghub_ledger.jsonl）记录每次任务的 taskId / 积分 / 用时，
    存放在 ComfyUI 的 output 目录，方便对账。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "action": (
                    ["查看", "清理"],
                    {
                        "default": "查看",
                        "tooltip": (
                            "查看：列出缓存占用与最近任务台账（不改动任何文件）。\n"
                            "清理：删除全部 latent 缓存文件（不动台账历史）。"
                            "下次同参数任务会重新花钱跑云端。"
                        ),
                    },
                ),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("report",)
    FUNCTION = "run"
    CATEGORY = CATEGORY
    TITLE = "RH 缓存管理"
    DESCRIPTION = (
        "查看 / 清理 latent 磁盘缓存，并回看最近任务的积分消耗与用时台账。\n"
        "输出接到「展示任何」类节点即可阅读。"
    )

    @_guarded("RH 缓存管理")
    def run(self, action: str = "查看", unique_id: Any = None) -> Tuple[str]:
        action = str(action or "查看").strip()
        cache_dir = cache_mod.get_cache_dir()
        files: List[str] = []
        total = 0
        if os.path.isdir(cache_dir):
            for name in os.listdir(cache_dir):
                path = os.path.join(cache_dir, name)
                if not os.path.isfile(path):
                    continue
                if name.endswith(".latent"):
                    files.append(path)
                    total += os.path.getsize(path)

        lines: List[str] = [f"缓存目录：{cache_dir}"]
        if action == "清理":
            removed = 0
            freed = 0
            for path in files:
                try:
                    freed += os.path.getsize(path)
                    os.remove(path)
                    removed += 1
                except OSError as exc:
                    lines.append(f"  跳过（文件被占用）：{os.path.basename(path)}（{exc}）")
            # 半截的 .tmp 一并扫掉
            if os.path.isdir(cache_dir):
                for name in os.listdir(cache_dir):
                    if name.endswith(".tmp"):
                        try:
                            os.remove(os.path.join(cache_dir, name))
                        except OSError:
                            pass
            lines.insert(1, f"已清理 {removed} 个缓存文件，释放 {_human_size(freed)}。")
            lines.insert(
                2,
                f"缓存上限仍为 {cache_mod._max_cache_mb():.0f}MB"
                "（config.ini 的 cache_max_mb，设 0 关闭自动清理）。",
            )
        else:
            max_mb = cache_mod._max_cache_mb()
            lines.append(
                f"当前缓存：{len(files)} 个文件，共 {_human_size(total)}"
                f"（自动清理上限 {max_mb:.0f}MB，设 0 关闭）"
            )
            lines.append("说明：缓存键 = 工作流 + 全部参数，同参数重跑直接复用、不花积分。")

        lines.append("")
        records = ledger_mod.read_records(limit=10)
        if records:
            lines.append(f"最近 {len(records)} 次任务台账（新→旧，完整文件见 output 目录）：")
            lines.extend("  " + ledger_mod.format_record(r) for r in records)
        else:
            lines.append("台账为空：还没有成功/失败的任务记录。")

        return ("\n".join(lines),)


NODE_CLASS_MAPPINGS = {
    "RunningHubLatentGeneratorSimple": RunningHubLatentGeneratorSimple,
    "RunningHubLatentGenerator": RunningHubLatentGenerator,
    "RunningHubLatentFetch": RunningHubLatentFetch,
    "RunningHubLatentFromUrl": RunningHubLatentFromUrl,
    "RunningHubWorkflowInspector": RunningHubWorkflowInspector,
    "RunningHubCacheManager": RunningHubCacheManager,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    name: getattr(cls, "TITLE", name) for name, cls in NODE_CLASS_MAPPINGS.items()
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "CATEGORY"]
