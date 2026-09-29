"""
Latent 文件解析 —— 把云端返回的文件还原成 ComfyUI 标准 LATENT 张量。

对齐 ComfyUI 原生 nodes.LoadLatent 的行为，并额外覆盖视频 latent（5 维）。

## ComfyUI 原生 SaveLatent 产物的结构

safetensors 容器，常见两种 key：

    {"latent_tensor": Tensor}                # 新版（主流）
    {"latents": Tensor}                      # 旧版本
    {"latent_format_version_0": <空张量>}     # 存在即代表「新版格式」

## 那个神秘的 0.18215

原生 LoadLatent 里有这段：

    multiplier = 1.0
    if "latent_format_version_0" not in latent:
        multiplier = 1.0 / 0.18215

它是对 SDXL 时代「把 latent 除以 0.18215 再存盘」这一约定的补偿：新版格式在
保存时就带了标记，说明存的是**未经缩放**的原始 latent，所以不需要补；而老文件
存的是被压缩过的值，读取时要乘回来。

本文件严格照抄这条规则，不做任何「贴心」的自动推断——因为一旦对不需要补偿的
新模型（Flux / SD3 / Wan 视频等）误乘 5.49 倍，出图会直接崩坏成纯噪声，
且这类故障极难排查。

## 维度约定

    [B, C, H, W]       图像 latent（4 维）
    [B, C, F, H, W]    视频 latent（5 维，F 为帧数）
    [C, H, W]          无 batch 轴，补成 [1, C, H, W]

注意：5 维张量**不能**被当作缺 batch 轴去 unsqueeze，否则会变成非法的 6 维。
"""

from __future__ import annotations

import os
from io import BytesIO
from typing import Any, Optional, Tuple, Union

import torch

try:  # pragma: no cover - ComfyUI 专有
    import safetensors.torch
except ImportError:  # 离线 lint / 单测时允许缺失
    safetensors = None

try:
    import numpy as np
except ImportError:
    np = None

# SDXL 时代 latent 存储缩放因子，仅用于补偿缺少版本标记的老文件
_LEGACY_LATENT_SCALE = 1.0 / 0.18215

_LATENT_KEYS = ("latent_tensor", "latents")

# 音视频复合 latent（目前只有 MiniMax H3 在用）的常见键名组合。
# 顺序即优先级：社区 JerryZRic 的 .h3latent 用 latent_0/latent_1。
_AV_PAIR_KEYS = (
    ("latent_0", "latent_1"),
    ("h3_video", "h3_audio"),
    ("video_latent", "audio_latent"),
    ("video", "audio"),
)

# 出现任意一个即认为这是 AV 容器（用于兜底识别）
_AV_MARKER_KEYS = ("h3_format_version", "tensor_count", "av_format_version")


class LatentParseError(ValueError):
    """latent 文件无法解析。消息面向用户，使用中文。"""


class _FallbackNestedTensor:
    """comfy.nested_tensor 不可用时的最小替身。

    ComfyUI 的下游节点（VAEDecode / VAEDecodeAudio）只用到 is_nested 属性和
    unbind()，所以 duck typing 足够。真正跑在 ComfyUI 里时永远走真货，
    这里只是让离线单测也能覆盖 AV 分支。
    """

    def __init__(self, tensors):
        self.tensors = list(tensors)
        self.is_nested = True

    def unbind(self):
        return self.tensors

    def to(self, *args, **kwargs):
        return _FallbackNestedTensor([t.to(*args, **kwargs) for t in self.tensors])

    def cpu(self):
        return self.to("cpu")

    @property
    def shape(self):
        return self.tensors[0].shape

    @property
    def ndim(self):
        return max(t.ndim for t in self.tensors)

    @property
    def device(self):
        return self.tensors[0].device

    @property
    def dtype(self):
        return self.tensors[0].dtype

    def __repr__(self):
        return f"NestedTensor({[tuple(t.shape) for t in self.tensors]})"


def make_nested(video: torch.Tensor, audio: torch.Tensor):
    """把 (视频 latent, 音频 latent) 包成 ComfyUI 的 NestedTensor。"""
    try:  # pragma: no cover - ComfyUI 专有
        from comfy.nested_tensor import NestedTensor

        return NestedTensor((video, audio))
    except Exception:
        return _FallbackNestedTensor((video, audio))


def is_nested(obj: Any) -> bool:
    """判断一个对象是不是音视频复合 latent。"""
    return bool(getattr(obj, "is_nested", False)) and hasattr(obj, "unbind")


def _split_av_tensors(data: dict):
    """从 safetensors 结果里识别出 (视频 latent, 音频 latent)。

    命中返回 (video, audio)，否则返回 None。判据有二：
      1. 存在成对的已知键名（latent_0/latent_1 等）；
      2. 兜底：恰好两个 3~5 维张量且维度数不同 —— 5 维那个视为视频。
    普通 SaveLatent 产物只有一个 latent_tensor（外加 0 维的版本标记张量），
    不会被这条兜底误伤。
    """
    if not isinstance(data, dict) or not data:
        return None

    for vk, ak in _AV_PAIR_KEYS:
        v, a = data.get(vk), data.get(ak)
        if isinstance(v, torch.Tensor) and isinstance(a, torch.Tensor):
            return v, a

    if not any(k in data for k in _AV_MARKER_KEYS):
        return None

    usable = [
        v for v in data.values()
        if isinstance(v, torch.Tensor) and len(v.shape) in (3, 4, 5)
    ]
    if len(usable) != 2:
        return None
    videos = [t for t in usable if len(t.shape) == 5]
    if len(videos) != 1:
        return None
    video = videos[0]
    audio = [t for t in usable if t is not video][0]
    return video, audio


def parse_av(data: dict, source: str = ""):
    """把 AV 容器还原成 {"samples": NestedTensor}。不是 AV 容器时返回 None。

    ## 为什么这里一律不乘 1/0.18215

    那条补偿只对「SDXL 时代存盘前除以过 0.18215」的老 latent 成立。
    目前唯一会产生复合 latent 的 MiniMax H3，其 latent_format 的
    scale_factor 就是 1.0（comfy/latent_formats.py MiniMaxH3Video），
    存的是原始值。对 H3 误乘 5.49 倍会直接把画面毁成噪声。
    """
    pair = _split_av_tensors(data)
    if pair is None:
        return None
    video, audio = pair
    video = video.to(torch.float32)
    audio = audio.to(torch.float32)
    if len(video.shape) == 3:  # 缺 batch 轴
        video = video.unsqueeze(0)
    return {"samples": make_nested(video, audio)}


def _pick_tensor(data: dict, source: str) -> torch.Tensor:
    """从 safetensors 结果里挑出真正的 latent 张量并应用正确的缩放。"""
    if not isinstance(data, dict) or not data:
        raise LatentParseError(f"latent 文件为空或格式不正确（来源：{source}）")

    for key in _LATENT_KEYS:
        value = data.get(key)
        if isinstance(value, torch.Tensor):
            # 存在版本标记 => 新格式，张量已是原始值，禁止补偿缩放
            if "latent_format_version_0" in data:
                return value.to(torch.float32)
            return value.to(torch.float32) * _LEGACY_LATENT_SCALE

    # 兜底：部分第三方导出工具用了自定义 key，取形状最大且维度合法的那个
    candidates = [
        v for v in data.values()
        if isinstance(v, torch.Tensor) and len(v.shape) in (3, 4, 5)
    ]
    if not candidates:
        keys = ", ".join(sorted(str(k) for k in data.keys())[:8])
        raise LatentParseError(
            f"latent 文件中找不到 latent 张量（来源：{source}）。"
            f"文件内包含的键：{keys}。请确认云端工作流末端接的是「保存Latent」节点。"
        )
    tensor = max(candidates, key=lambda t: t.numel())
    if "latent_format_version_0" in data:
        return tensor.to(torch.float32)
    return tensor.to(torch.float32) * _LEGACY_LATENT_SCALE


def _normalize_shape(tensor: torch.Tensor) -> torch.Tensor:
    """把 3 维 latent 补出 batch 轴；4/5 维保持原样。"""
    if len(tensor.shape) == 3:
        return tensor.unsqueeze(0)
    return tensor


def _validate(tensor: torch.Tensor, source: str) -> None:
    ndim = len(tensor.shape)
    if ndim == 4:
        _b, channels, _h, _w = tensor.shape
        expected = channels in (4, 8, 16, 32, 48, 64)
        note = "图像 latent 的通道数通常为 4，Flux / SD3 系列为 16。"
    elif ndim == 5:
        _b, channels, frames, _h, _w = tensor.shape
        expected = frames >= 1 and channels in (4, 8, 16, 32, 48, 64, 128)
        note = "视频 latent 形状应为 [B, C, F, H, W]，请确认 F 是帧数轴。"
    else:
        raise LatentParseError(
            f"latent 张量维度异常：期望 4 维（图像）或 5 维（视频），"
            f"实际为 {ndim} 维，形状 {tuple(tensor.shape)}（来源：{source}）"
        )

    if not expected:
        # 不阻断，仅提示——不同模型的通道数差异很大，误杀比误放更糟
        print(
            f"[RunningHub-Latent] 提示：latent 形状 {tuple(tensor.shape)} 与常见模型不完全一致。"
            f"{note} 若出图异常，请检查本地是否使用了匹配的 VAE。"
        )


def looks_like_safetensors(blob: bytes) -> bool:
    """用 safetensors 头部（8 字节 little-endian header 长度）判断文件类型。

    RunningHub 返回的下载链接有时不带扩展名，所以不能只靠 URL 判断。
    """
    if len(blob) < 8:
        return False
    header_size = int.from_bytes(blob[:8], "little")
    return 8 <= header_size <= len(blob) and header_size <= 100_000_000


def _load_safetensors_dict(source: Union[str, bytes]) -> Tuple[dict, str]:
    """读取 safetensors 文件，返回 (原始键值 dict, 来源标签)。"""
    if safetensors is None:
        raise LatentParseError(
            "缺少 safetensors 依赖，请在 ComfyUI 的 Python 环境中执行："
            "pip install safetensors"
        )
    if isinstance(source, (bytes, bytearray)):
        raw, label = bytes(source), f"<内存 {len(source)} 字节>"
    else:
        raw, label = source, os.path.basename(source)

    # safetensors 底层抛的是英文异常，必须包成中文 LatentParseError，
    # 否则用户会在 ComfyUI 前端看到一堆看不懂的 Rust traceback。
    try:
        data = safetensors.torch.load(raw) if isinstance(raw, bytes) else safetensors.torch.load_file(raw)
    except Exception as exc:
        raise LatentParseError(
            f"latent 文件不是有效的 safetensors 格式（来源：{label}）。"
            f"底层错误：{exc}。"
            f"若这是手动下载的文件，请确认下载完整（未下载到网页 HTML 或错误页）。"
        ) from exc
    return data, label


def load_safetensors(source: Union[str, bytes]) -> torch.Tensor:
    """加载 .latent / .safetensors。source 可为本地路径或内存字节。"""
    data, label = _load_safetensors_dict(source)
    return _pick_tensor(data, label)


def load_safetensors_av(source: Union[str, bytes]):
    """按音视频复合 latent 解析；不是 AV 容器返回 None。"""
    data, label = _load_safetensors_dict(source)
    return parse_av(data, label)


def load_numpy(source: Union[str, bytes]) -> torch.Tensor:
    """加载 .npy。"""
    if np is None:
        raise LatentParseError("缺少 numpy 依赖，请执行：pip install numpy")
    try:
        array = np.load(BytesIO(source) if isinstance(source, (bytes, bytearray)) else source)
    except Exception as exc:
        raise LatentParseError(f"npy 文件解析失败：{exc}") from exc
    return torch.from_numpy(array).to(torch.float32)


def load_npz(source: Union[str, bytes]) -> torch.Tensor:
    """加载 sd-scripts 导出的 .npz。"""
    if np is None:
        raise LatentParseError("缺少 numpy 依赖，请执行：pip install numpy")
    try:
        with np.load(BytesIO(source) if isinstance(source, (bytes, bytearray)) else source) as data:
            keys = data.files
            if "latents" in keys:
                array = data["latents"]
            else:
                # 取维度 >= 3 的第一个数组
                usable = [k for k in keys if getattr(data[k], "ndim", 0) >= 3]
                if not usable:
                    raise LatentParseError(
                        f"npz 文件中没有可用的 latent 数组（来源键：{keys}）"
                    )
                array = data[usable[0]]
    except LatentParseError:
        raise
    except Exception as exc:
        raise LatentParseError(f"npz 文件解析失败：{exc}") from exc
    return torch.from_numpy(array).to(torch.float32)


def parse_latent_bytes(blob: bytes, url: str = "") -> torch.Tensor:
    """按内容嗅探格式并解析。优先信 content sniff，其次信扩展名。"""
    if not blob:
        raise LatentParseError(f"下载到的 latent 文件为空（{url or '未知来源'}）")

    if looks_like_safetensors(blob):
        return load_safetensors(blob)

    lowered = (url or "").lower()
    if lowered.endswith((".npy",)):
        return load_numpy(blob)
    if lowered.endswith((".npz",)):
        return load_npz(blob)

    # 扩展名不可靠时（RunningHub 的部分下载链接不带后缀），按两种格式依次试
    try:
        return load_safetensors(blob)
    except LatentParseError as st_err:
        try:
            return load_numpy(blob)
        except LatentParseError:
            # 两种都不是。safetensors 是「保存Latent」的产物，报错时优先返回它的诊断
            raise st_err from None


def to_comfy_latent(tensor: torch.Tensor, source: str = "") -> dict:
    """归一化维度 + 校验，返回 ComfyUI 标准的 {"samples": tensor}。"""
    tensor = _normalize_shape(tensor)
    _validate(tensor, source)
    return {"samples": tensor}


def parse_av_bytes(blob: bytes, url: str = ""):
    """尝试按音视频复合 latent 解析；不是 AV 容器返回 None。

    MiniMax H3 的 latent 是 NestedTensor(视频, 音频)，原生 SaveLatent 存不了
    （它没有 .contiguous()），需要用专门的保存节点把两个张量分键存进一个
    safetensors 容器。这里负责把它还原回 NestedTensor。
    """
    if not blob or not looks_like_safetensors(blob):
        return None
    try:
        data, label = _load_safetensors_dict(blob)
    except LatentParseError:
        return None
    return parse_av(data, label)


def parse(blob: bytes, url: str = "") -> dict:
    """一步到位：字节 -> ComfyUI LATENT dict（自动识别音视频复合 latent）。"""
    av = parse_av_bytes(blob, url)
    if av is not None:
        return av
    return to_comfy_latent(parse_latent_bytes(blob, url), url)


def save_av_latent(
    path: str,
    video: torch.Tensor,
    audio: torch.Tensor,
    metadata: Optional[dict] = None,
) -> str:
    """把 (视频 latent, 音频 latent) 写成一个 safetensors 文件。

    键名沿用社区约定 latent_0 / latent_1，并带上 latent_format_version_0
    标记（读回时据此判断「不需要做 0.18215 补偿」）。
    """
    if safetensors is None:
        raise LatentParseError("缺少 safetensors 依赖，请执行：pip install safetensors")

    payload = {
        "latent_0": video.detach().to("cpu").contiguous(),
        "latent_1": audio.detach().to("cpu").contiguous(),
        "latent_format_version_0": torch.tensor([]),
        "tensor_count": torch.tensor([2]),
    }
    meta = {str(k): str(v) for k, v in (metadata or {}).items() if v is not None}
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    safetensors.torch.save_file(payload, path, metadata=meta)
    return path


__all__ = [
    "LatentParseError",
    "parse",
    "parse_av_bytes",
    "parse_latent_bytes",
    "to_comfy_latent",
    "looks_like_safetensors",
    "is_nested",
    "make_nested",
    "save_av_latent",
]
