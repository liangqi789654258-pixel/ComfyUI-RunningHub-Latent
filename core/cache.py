"""
Latent 磁盘缓存与下载。

## 为什么需要缓存

RunningHub 按调用次数扣费。同一个 workflow_id + 同一组参数（尤其是 seed 相同）
重复提交，产出的 latent 是逐位一致的，再付一次钱纯属浪费。

## 缓存键怎么算

    md5(canonical_json(workflow_id + nodeInfoList))

要点：
  * **不含 API Key** —— 换 Key 不影响复用，也避免密钥参与哈希运算。
  * nodeInfoList 先排序再序列化 —— 前端调整参数顺序不应导致缓存穿透。
  * 缓存的是**原始文件字节**，而不是解码后的张量。后者经过 dtype/shape 归一
    化会丢信息，且 torch.save 体积更大。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from typing import Any, Dict, List, Optional

import requests

CACHE_DIRNAME = ".rh_latent_cache"


def canonical_dumps(obj: Any) -> str:
    """稳定序列化：key 排序、无多余空格、非 ASCII 保留，保证同参数同结果。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def compute_cache_key(workflow_id: str, node_info_list: List[Dict[str, Any]]) -> str:
    """按「工作流 + 参数」计算缓存 fingerprint。"""
    payload = {
        "workflowId": str(workflow_id).strip(),
        # 参数顺序归一化，避免同一个语义算出两个不同的键
        "nodeInfoList": sorted(
            node_info_list or [],
            key=lambda item: (str(item.get("nodeId", "")), str(item.get("fieldName", ""))),
        ),
    }
    return hashlib.md5(canonical_dumps(payload).encode("utf-8")).hexdigest()


def get_cache_dir(root: Optional[str] = None) -> str:
    """缓存目录。优先放在 ComfyUI 的 output 目录下，保证用户能找到、能手动清理。"""
    base = root
    if base is None:
        try:
            import folder_paths  # ComfyUI 专有

            base = folder_paths.get_output_directory()
        except Exception:
            base = None
    if base is None:
        base = os.path.join(tempfile.gettempdir(), "runninghub_latent")

    path = os.path.join(base, CACHE_DIRNAME)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        path = os.path.join(tempfile.gettempdir(), CACHE_DIRNAME)
        os.makedirs(path, exist_ok=True)
    return path


def cache_path_for(key: str) -> Optional[str]:
    """命中缓存返回本地路径，否则 None。"""
    path = os.path.join(get_cache_dir(), f"{key}.latent")
    # 体积为 0 说明上次写入中断了，视为未命中
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        return path
    return None


def store(key: str, blob: bytes) -> str:
    """写入缓存，返回文件路径。"""
    path = os.path.join(get_cache_dir(), f"{key}.latent")
    tmp = f"{path}.tmp"
    with open(tmp, "wb") as fh:
        fh.write(blob)
    # 先写临时文件再原子替换，避免中断留下半截文件被当作有效缓存
    os.replace(tmp, path)
    return path


def download(url: str, timeout: float = 120.0, max_retries: int = 3) -> bytes:
    """下载远程文件到内存。latent 体积通常只有几十 MB，直接放内存最简单可靠。"""
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, timeout=timeout, stream=True)
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries:
                time.sleep(2.0 * attempt)
    raise RuntimeError(f"下载失败（已重试 {max_retries} 次）：{url}\n原因：{last_exc}")


__all__ = [
    "compute_cache_key",
    "cache_path_for",
    "store",
    "download",
    "get_cache_dir",
    "canonical_dumps",
]
