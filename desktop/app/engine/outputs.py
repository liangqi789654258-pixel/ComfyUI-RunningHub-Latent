"""云端输出文件分类 —— 转调插件 `core/output.py`。

历史上这里是「从插件 nodes.py 移植」的一份副本，后来两边各自演化
（出图器少了原始返回提示、报错指引少一条），所以收敛回单一实现。
本模块只保留出图器侧需要的薄封装，不再持有任何规则常量。
"""

from __future__ import annotations

from typing import Any, Dict, List

from . import core_bridge


def _core():
    return core_bridge.core_output()


def output_type_of(item: Dict[str, Any]) -> str:
    return _core().output_type_of(item)


def classify(item: Dict[str, Any]) -> str:
    return _core().classify(item)


def url_of(item: Dict[str, Any]) -> str:
    return _core().url_of(item)


def collect_latents(outputs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """筛出所有 latent 输出并按稳定顺序排序；找不到时给出可操作指引。"""
    return _core().collect_latents(outputs)


def merge_latents(tensors):
    """多 latent 合并：形状一致沿 batch 合并；不一致直接报错。"""
    return _core().merge_latents(tensors)
