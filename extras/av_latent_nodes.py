"""
AV Latent 节点（云端 / 本地通用，零第三方依赖）

用途：让 MiniMax H3 这类「一次生成视频+音频」的模型能把 latent 存下来。

为什么原生 SaveLatent 不行
--------------------------
H3 的 latent 不是单个张量，而是：

    {"samples": NestedTensor((video [B,24,T,H/16,W/16], audio [B,32,2,T40]))}

而 ComfyUI 原生 nodes.SaveLatent 里有一行：

    output["latent_tensor"] = samples["samples"].contiguous()

NestedTensor 是纯 Python 容器，没有 contiguous 方法，于是直接抛：

    AttributeError: 'NestedTensor' object has no attribute 'contiguous'

这不是接线错误，是原生节点没覆盖这种数据结构。本文件用两个节点补上：

    AV Latent 保存    把复合 latent 写成一个 .latent 文件（视频/音频分键存）
    AV Latent 拆分    把复合 latent 拆成两条普通 latent

安装（云端或本地都一样）
------------------------
把本文件单独放进一个目录即可，例如：

    ComfyUI/custom_nodes/RH-AVLatent/__init__.py   <- 本文件内容

**重要：RunningHub 云端装不了这个包。**
RunningHub 不接受用户自行上传安装自定义节点（2026-09-30 确认），
所以在 RunningHub 上跑 MiniMax H3 时，本文件的节点用不上。
H3 在 RunningHub 上的正确用法是「云端整体出片 + 音频单独导出」：

    H3 采样器 →┬─ VAEDecode(H3 视频 VAE)      → VHS_VideoCombine(24fps) → mp4
               └─ VAEDecodeAudio(H3 音频 VAE) → Save Audio (Advanced)   → flac/mp3

本文件的节点保留给以下场景：本地运行 H3、自建 ComfyUI 云端实例、
或其它允许安装自定义节点的平台。详见 docs/h3-av-latent.md。

不需要 pip install 任何东西（只用到 torch / safetensors / folder_paths，
这些都是 ComfyUI 自带）。重启 ComfyUI 后在「model/latent」分类里能找到。

文件格式
--------
safetensors 容器，扩展名沿用 .latent（云端平台据此把它当作 latent 输出收录）：

    latent_0                  视频 latent  [B, 24, T, H, W]
    latent_1                  音频 latent  [B, 32, 2, T]
    latent_format_version_0   空张量，标记「存的是原始值，无需 0.18215 补偿」
    tensor_count              [2]
"""

from __future__ import annotations

import json
import os

import torch
import safetensors.torch

import folder_paths

try:
    from comfy.nested_tensor import NestedTensor
except Exception:  # pragma: no cover
    NestedTensor = None


CATEGORY = "model/latent"


def _is_nested(obj) -> bool:
    return bool(getattr(obj, "is_nested", False)) and hasattr(obj, "unbind")


def _make_nested(video, audio):
    if NestedTensor is not None:
        return NestedTensor((video, audio))
    # 极端兜底：duck typing 版本，下游只用到 is_nested 与 unbind()
    class _NT:
        def __init__(self, tensors):
            self.tensors = list(tensors)
            self.is_nested = True

        def unbind(self):
            return self.tensors

        @property
        def shape(self):
            return self.tensors[0].shape

    return _NT((video, audio))


class AVLatentSave:
    """替代原生 SaveLatent，能存音视频复合 latent。"""

    def __init__(self):
        self.output_dir = folder_paths.get_output_directory()

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
    TITLE = "AV Latent 保存"
    DESCRIPTION = (
        "保存音视频复合 latent（MiniMax H3）。原生 SaveLatent 会报 "
        "'NestedTensor' object has no attribute 'contiguous'，用本节点替代。"
    )

    def save(self, samples, filename_prefix="latents/MiniMaxH3", prompt=None, extra_pnginfo=None):
        inner = (samples or {}).get("samples")
        if inner is None:
            raise ValueError("AV Latent 保存：输入的 latent 为空。")

        full_folder, filename, counter, subfolder, _ = folder_paths.get_save_image_path(
            filename_prefix, self.output_dir
        )
        file = f"{filename}_{counter:05}_.latent"
        path = os.path.join(full_folder, file)
        os.makedirs(full_folder, exist_ok=True)

        metadata = {}
        if prompt is not None:
            metadata["prompt"] = json.dumps(prompt)
        for key, value in (extra_pnginfo or {}).items():
            metadata[str(key)] = json.dumps(value)

        if _is_nested(inner):
            parts = list(inner.unbind())
            if len(parts) < 2:
                raise ValueError(
                    f"AV Latent 保存：复合 latent 只有 {len(parts)} 个张量，"
                    "至少需要视频+音频两个。"
                )
            payload = {
                "latent_0": parts[0].detach().to("cpu").contiguous(),
                "latent_1": parts[1].detach().to("cpu").contiguous(),
                "latent_format_version_0": torch.tensor([]),
                "tensor_count": torch.tensor([2]),
            }
        else:
            # 普通单张量 latent：按原生格式写，保证 LoadLatent 也能读
            payload = {
                "latent_tensor": inner.detach().to("cpu").contiguous(),
                "latent_format_version_0": torch.tensor([]),
            }

        safetensors.torch.save_file(payload, path, metadata=metadata)
        return {
            "ui": {"latents": [{"filename": file, "subfolder": subfolder, "type": "output"}]},
            "result": (samples,),
        }


class AVLatentSplit:
    """把音视频复合 latent 拆成视频 latent 与音频 latent。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"samples": ("LATENT",)}}

    RETURN_TYPES = ("LATENT", "LATENT")
    RETURN_NAMES = ("video", "audio")
    FUNCTION = "split"
    CATEGORY = CATEGORY
    TITLE = "AV Latent 拆分"
    DESCRIPTION = (
        "把 MiniMax H3 的音视频复合 latent 拆成视频与音频两条。\n"
        "原生 VAEDecode 遇到复合 latent 只解视频并静默丢弃音频，所以必须先拆开。"
    )

    def split(self, samples):
        inner = (samples or {}).get("samples")
        if not _is_nested(inner):
            raise ValueError(
                "AV Latent 拆分：输入不是音视频复合 latent（NestedTensor），无需拆分。\n"
                "普通 latent 是单个张量，直接接 VAEDecode 即可。"
            )
        parts = list(inner.unbind())
        if len(parts) < 2:
            raise ValueError(
                f"AV Latent 拆分：复合 latent 只有 {len(parts)} 个张量，拆不出音频。"
            )
        return ({"samples": parts[0]}, {"samples": parts[1]})


NODE_CLASS_MAPPINGS = {
    "AVLatentSave": AVLatentSave,
    "AVLatentSplit": AVLatentSplit,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "AVLatentSave": "AV Latent 保存",
    "AVLatentSplit": "AV Latent 拆分",
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
