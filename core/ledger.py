"""
历史任务台账 —— 每次云端调用的成本与结果留痕。

为什么需要：RunningHub 按次扣积分，「刚才那次到底花了多少、跑了多久」
在 ComfyUI 里无处可查，网页端控制台又不方便对着本地工作流回溯。
本模块把每次任务追加写进一行 JSON（jsonl），供：
  * 「RH 缓存管理」节点回看最近记录；
  * 用户排错时核对「哪一次任务、花了多少积分、什么结果」。

设计约束：
  * 只追加不修改（append-only），写坏一行不影响其他行；
  * 任何写失败只打印警告，绝不让任务本身报错——台账是附赠品；
  * 不记录 API Key，不记录完整提示词（提示词已在 report 里回显过）。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Any, Dict, List, Optional

LEDGER_FILENAME = "runninghub_ledger.jsonl"


def ledger_path(root: Optional[str] = None) -> str:
    """台账文件位置。优先放 ComfyUI 的 output 目录，用户找得到、能手动删。"""
    base = root
    if base is None:
        try:
            import folder_paths  # ComfyUI 专有

            base = folder_paths.get_output_directory()
        except Exception:
            base = None
    if base is None:
        base = os.path.join(tempfile.gettempdir(), "runninghub_latent")
    try:
        os.makedirs(base, exist_ok=True)
    except OSError:
        base = tempfile.gettempdir()
    return os.path.join(base, LEDGER_FILENAME)


def append_record(record: Dict[str, Any], root: Optional[str] = None) -> bool:
    """追加一条记录。成功返回 True；任何失败都只打印警告返回 False。"""
    try:
        record = dict(record)
        record.setdefault("ts", time.strftime("%Y-%m-%d %H:%M:%S"))
        line = json.dumps(record, ensure_ascii=False, default=str)
        with open(ledger_path(root), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return True
    except Exception as exc:  # 台账绝不能连累任务
        print(f"[RunningHub-Latent] 台账写入失败（已忽略）：{exc}")
        return False


def read_records(root: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
    """读取最近 limit 条记录（坏行跳过）。文件不存在返回空列表。"""
    path = ledger_path(root)
    if not os.path.isfile(path):
        return []
    lines: List[str] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    records: List[Dict[str, Any]] = []
    for line in reversed(lines):  # 从最新往回读
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue  # 写坏的一行直接跳过
        if len(records) >= limit:
            break
    return records


def clear(root: Optional[str] = None) -> int:
    """删除台账文件，返回删除的记录数（估算）。"""
    path = ledger_path(root)
    if not os.path.isfile(path):
        return 0
    count = 0
    try:
        with open(path, "r", encoding="utf-8") as fh:
            count = sum(1 for ln in fh if ln.strip())
        os.remove(path)
    except OSError as exc:
        print(f"[RunningHub-Latent] 台账清理失败：{exc}")
        return 0
    return count


def format_record(record: Dict[str, Any]) -> str:
    """把一条记录渲染成单行人话，供节点 report 展示。"""
    ts = record.get("ts", "?")
    outcome = {
        "success": "✅",
        "cached": "📦",
        "failed": "❌",
    }.get(str(record.get("outcome")), "·")
    parts = [f"{ts} {outcome}"]
    if record.get("task_id"):
        parts.append(f"taskId={record['task_id']}")
    coins = record.get("coins_spent")
    if isinstance(coins, (int, float)):
        parts.append(f"积分={coins:g}")
    elapsed = record.get("elapsed_s")
    if isinstance(elapsed, (int, float)):
        parts.append(f"用时={elapsed:.0f}s")
    if record.get("outcome") == "failed":
        parts.append(f"失败({record.get('error_category', '?')})")
        reason = str(record.get("error_reason", ""))
        if reason:
            parts.append(reason[:60])
    elif record.get("latent_shape"):
        parts.append(f"形状={record['latent_shape']}")
    return "  ".join(parts)


__all__ = [
    "ledger_path",
    "append_record",
    "read_records",
    "clear",
    "format_record",
    "LEDGER_FILENAME",
]
