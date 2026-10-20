"""云端输出的识别、收集与合并。

**单一代码源**：插件节点与 RH 出图器共用。历史上出图器整份移植过这段逻辑，
随后两边各自演化（出图器少了原始返回提示、报错指引少一条），所以收敛到这里。

容忍度来自实测：真实接口的类型字段名不统一（`fileType` / `outputType` / `type`），
大小写也不统一（见过 `Latent`），URL 还常带签名查询串。
"""

from __future__ import annotations

from typing import Any, Dict, List

from .errors import CAT_PARSE, RHUserError

# 输出分类用的关键字
LATENT_HINTS = ("latent", "safetensor")
VIDEO_EXT = (".mp4", ".webm", ".mov", ".avi", ".gif")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp")

_LATENT_EXT = (".latent", ".safetensors", ".h3latent", ".avlatent")


def output_type_of(item: Dict[str, Any]) -> str:
    """取类型字段并统一小写（字段名不统一，按优先级依次尝试）。"""
    for key in ("fileType", "outputType", "type", "file_type", "output_type"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return ""


def url_of(item: Dict[str, Any]) -> str:
    return str(item.get("fileUrl", "") or item.get("url", "") or "")


def classify(item: Dict[str, Any]) -> str:
    """把一条输出归为 latent / video / image / unknown。"""
    file_type = output_type_of(item)
    url = url_of(item).lower().split("?", 1)[0]
    if any(h in file_type for h in LATENT_HINTS) or url.endswith(_LATENT_EXT):
        return "latent"
    if url.endswith(VIDEO_EXT) or file_type in ("mp4", "webm"):
        return "video"
    if url.endswith(IMAGE_EXT) or file_type in ("png", "jpg", "jpeg", "webp"):
        return "image"
    return "unknown"


def collect_latents(
    outputs: List[Dict[str, Any]], raw_hint: str = ""
) -> List[Dict[str, Any]]:
    """筛出所有 latent 输出并按稳定顺序排序；找不到时给出可操作的中文指引。"""
    candidates = [o for o in outputs if classify(o) == "latent"]
    if not candidates:
        summary: List[str] = []
        for item in outputs:
            summary.append(
                f"  - [{classify(item)}] {url_of(item)} "
                f"(nodeId={item.get('nodeId', '?')}, "
                f"类型字段={output_type_of(item) or '缺失'})"
            )
        detail = "\n".join(summary) if summary else "  （云端返回的输出列表为空）"
        context = {"本次云端实际输出": detail}
        if raw_hint:
            context["云端原始返回"] = raw_hint
        raise RHUserError.build(
            CAT_PARSE,
            "云端任务已完成，但输出里没有识别出 latent 文件。",
            stage="识别云端输出",
            context=context,
            actions=(
                "云端工作流末端必须接「保存Latent」类节点，而不是「保存图像」/「保存视频」；",
                "该保存节点要能真正被执行到（不能被下游节点短路，也不能被开关节点跳过）；",
                "若上面显示「类型字段=缺失」，说明云端返回结构有变，"
                "请把上面这段原始返回发给作者，会加进兼容列表。",
            ),
        )
    candidates.sort(key=lambda o: (str(o.get("nodeId", "")), str(o.get("fileUrl", ""))))
    return candidates


def merge_latents(tensors: List[Any]) -> Any:
    """把多个输出的 latent 沿 batch 维合并。

    形状一致时合并成一个 batch（ComfyUI 原生 SaveLatent 多图输出的语义）；
    形状不一致时**直接报错**——静默取第一个只会把问题推迟到下游解码才炸。
    """
    if len(tensors) == 1:
        return tensors[0]

    first_shape = tuple(tensors[0].shape[1:])
    if all(tuple(t.shape[1:]) == first_shape for t in tensors[1:]):
        import torch  # 延迟导入：本模块其余部分不依赖 torch

        return torch.cat(tensors, dim=0)

    shapes = "、".join(str(tuple(t.shape)) for t in tensors)
    raise RHUserError.build(
        CAT_PARSE,
        f"云端这一次输出了 {len(tensors)} 个**尺寸不同**的 latent，无法一起处理。",
        stage="合并云端 latent",
        context={"各 latent 形状": shapes},
        actions=(
            "正常情况下（云端末端只有一个「保存Latent」类节点）不会出现这个报错，"
            "请检查是否多接了保存节点或拆分节点；",
            "确实需要多路结果时：云端用多个保存节点分别对应多个取回节点。",
        ),
    )


__all__ = [
    "LATENT_HINTS",
    "VIDEO_EXT",
    "IMAGE_EXT",
    "output_type_of",
    "url_of",
    "classify",
    "collect_latents",
    "merge_latents",
]
