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

from . import errors as errors_mod

CACHE_DIRNAME = ".rh_latent_cache"

# 缓存总大小默认上限（MB）。512MB 约够存 20~100 个 H3 单文件 latent，
# 既能留住近期可复用的结果，又不至于悄悄吃掉几个 G 磁盘。
DEFAULT_MAX_MB = 512.0


def _max_cache_mb() -> float:
    """读 config.ini 里的 cache_max_mb；读不到/写坏回落默认值。"""
    try:
        from . import settings as settings_mod

        raw = settings_mod.get("cache_max_mb", None)
        if raw is None or str(raw).strip() == "":
            return DEFAULT_MAX_MB
        return float(raw)
    except Exception:
        return DEFAULT_MAX_MB


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
    """写入缓存，返回文件路径。写完顺手做一次超限清理。"""
    path = os.path.join(get_cache_dir(), f"{key}.latent")
    tmp = f"{path}.tmp"
    with open(tmp, "wb") as fh:
        fh.write(blob)
    # 先写临时文件再原子替换，避免中断留下半截文件被当作有效缓存
    os.replace(tmp, path)
    prune(keep_path=path)
    return path


def prune(
    max_mb: Optional[float] = None,
    root: Optional[str] = None,
    keep_path: Optional[str] = None,
) -> int:
    """缓存总大小超过 max_mb（MB）时，按修改时间从旧到新删除，返回删除个数。

    max_mb 传 0 或负数 = 关闭自动清理。
    `root` 供测试注入临时目录；`keep_path` 保护刚写入的文件不被立刻清掉。
    """
    if max_mb is None:
        max_mb = _max_cache_mb()
    if max_mb is None or max_mb <= 0:
        return 0

    cache_dir = get_cache_dir(root)
    entries: List[tuple] = []
    total = 0
    for name in os.listdir(cache_dir):
        path = os.path.join(cache_dir, name)
        if not name.endswith(".latent") or not os.path.isfile(path):
            continue
        try:
            st = os.stat(path)
        except OSError:
            continue
        entries.append((st.st_mtime, st.st_size, path))
        total += st.st_size

    limit = max_mb * 1024 * 1024
    if total <= limit:
        return 0

    removed = 0
    for _mtime, size, path in sorted(entries):
        if total <= limit:
            break
        if keep_path and os.path.abspath(path) == os.path.abspath(keep_path):
            continue
        try:
            os.remove(path)
            total -= size
            removed += 1
        except OSError:
            continue  # 文件被占用等，跳过下次再清

    if removed:
        print(
            f"[RunningHub-Latent] latent 缓存已超过 {max_mb:.0f}MB 上限，"
            f"自动清理了 {removed} 个最旧的缓存文件。"
            "可在 config.ini 里设 cache_max_mb 调整上限（设 0 关闭自动清理）。"
        )
    return removed


def _download_error(url: str, status: int = 0, detail: str = "") -> "errors_mod.RHUserError":
    """把下载失败翻译成「链接过期 / 网络问题 / 服务端故障」三类之一。

    以前一律报「下载失败（已重试 N 次）」，用户分不清是该重跑任务还是查网络。
    """
    if status in (401, 403):
        reason = (
            f"云端文件不允许下载（HTTP {status}）：直链带签名且会过期，"
            "通常是链接失效或账号无权限。重跑一次任务即可拿到新链接。"
        )
        category = errors_mod.CAT_DOWNLOAD
    elif status == 404:
        reason = (
            "云端文件已不存在（HTTP 404）：结果文件有有效期，过期会被清理。"
            "只能重跑一次任务。"
        )
        category = errors_mod.CAT_DOWNLOAD
    elif 500 <= status < 600:
        reason = f"对象存储服务端故障（HTTP {status}），稍后重试即可。"
        category = errors_mod.CAT_DOWNLOAD
    elif status >= 400:
        reason = f"下载请求被拒绝（HTTP {status}）。"
        category = errors_mod.CAT_DOWNLOAD
    elif detail:
        reason = detail
        category = errors_mod.CAT_NETWORK
    else:
        reason = "未知下载错误。"
        category = errors_mod.CAT_DOWNLOAD

    err = errors_mod.RHUserError.build(
        category,
        reason,
        stage="下载云端 latent 文件",
        context={"下载地址": errors_mod.truncate(url, 200)},
        actions=(
            errors_mod.ACTIONS[errors_mod.CAT_DOWNLOAD]
            if category == errors_mod.CAT_DOWNLOAD
            else errors_mod.ACTIONS[errors_mod.CAT_NETWORK]
        ),
    )
    err.retryable = 500 <= status < 600
    return err


def download(url: str, timeout: float = 120.0, max_retries: int = 3) -> bytes:
    """下载远程文件到内存。latent 体积通常只有几十 MB，直接放内存最简单可靠。"""
    last_error: Optional[errors_mod.RHUserError] = None

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, timeout=timeout, stream=True)
        except requests.exceptions.Timeout:
            last_error = _download_error(
                url, detail=f"下载超时：{timeout:.0f} 秒内没有收完数据。"
            )
        except requests.exceptions.ConnectionError as exc:
            last_error = _download_error(url, detail=f"连不上文件服务器：{exc}")
        except requests.exceptions.RequestException as exc:
            last_error = _download_error(url, detail=f"下载请求异常：{exc}")
        else:
            status = int(getattr(resp, "status_code", 0) or 0)
            if status >= 400:
                last_error = _download_error(url, status=status)
            else:
                return resp.content

        if attempt < max_retries:
            time.sleep(2.0 * attempt)

    raise last_error or _download_error(url)


__all__ = [
    "compute_cache_key",
    "cache_path_for",
    "store",
    "download",
    "get_cache_dir",
    "canonical_dumps",
    "prune",
    "DEFAULT_MAX_MB",
]
