"""帧数组归一化：把各款 VAE 的解码输出统一成 [H, W, 3] / uint8。

为什么单独抽一个模块：图片（PNG）与视频（mp4）两条保存路径要做的是同一件事，
放两份迟早会漂移。这里踩过的坑（避免回退）：

  * 视频 / 3D 卷积 VAE 输出 5 维，归一化后帧的通道数可能是 1（灰度）或 4（RGBA）；
  * PIL 遇到 (1,1,1152,3) 这种形状会报 "Cannot handle this data type"，
    所以必须在进 PIL / ffmpeg 之前就把通道数收敛到 3。
"""

from __future__ import annotations

from typing import Any, List

import numpy as np


def to_uint8_rgb(frame: Any) -> np.ndarray:
    """单帧 [H,W,C]（0..1 浮点）→ [H,W,3] uint8。

    形状不合法时抛 ValueError，文案里带原始形状与 VAE 提示，
    方便用户直接把报错贴给作者。
    """
    arr = np.asarray(frame)
    if arr.ndim != 3 or arr.shape[2] not in (1, 3, 4):
        raise ValueError(
            f"解码输出形状异常：{arr.shape}（期望 [H,W,C]，C 为 1/3/4）。"
            "请把此报错与所用 VAE 名称一并发给作者。"
        )
    if arr.shape[2] == 1:
        arr = np.repeat(arr, 3, axis=2)
    elif arr.shape[2] == 4:
        # VAE 的 alpha 通道直接丢弃（与 PIL RGBA→RGB 的行为一致）
        arr = arr[..., :3]
    return (np.clip(arr, 0.0, 1.0) * 255.0).round().astype("uint8")


def to_uint8_rgb_list(frames: Any) -> List[np.ndarray]:
    """批量归一化，返回 uint8 帧列表。"""
    return [to_uint8_rgb(frame) for frame in frames]
