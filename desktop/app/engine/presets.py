"""预设管理：一份预设 = 一套云端工作流的全部用法。

预设内容：
  workflow_id   云端工作流
  injections    参数映射骨架（fieldValue 用 {positive}/{negative}/{seed} 占位符）
  vae_file      默认选用的 VAE 文件名（可为空，运行时在界面选）
  notes         生成预设时拉到的云端字段清单（mapping_templates 全文），供参考

「从工作流 ID 生成预设」的自动逻辑：
  拉取 getJsonApiFormat 结构 → 找含 {positive}/{negative} 占位符的文本字段 →
  找采样器 seed 字段 → 各生成一条映射骨架；其余字段给参考清单不自动注入
 （参数落点不能靠推断——这是插件用真金白银换来的规则）。
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List

from .. import paths
from . import core_bridge

_SLUG_SAFE = re.compile(r"[^\w\-]+", re.UNICODE)


def presets_dir() -> str:
    paths.ensure_dirs()
    return paths.PRESETS_DIR


def list_presets() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    d = presets_dir()
    for name in sorted(os.listdir(d)):
        if not name.lower().endswith(".json"):
            continue
        try:
            with open(os.path.join(d, name), "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and data.get("workflow_id"):
                data["__file"] = name
                out.append(data)
        except Exception:
            continue  # 坏文件跳过，不阻塞其余预设
    return out


def save_preset(preset: Dict[str, Any]) -> str:
    d = presets_dir()
    base = _SLUG_SAFE.sub("_", str(preset.get("name") or preset.get("workflow_id") or "preset"))
    fname = base if base.endswith(".json") else base + ".json"
    target = os.path.join(d, fname)
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(preset, fh, ensure_ascii=False, indent=2)
    return fname


def delete_preset(fname: str) -> None:
    target = os.path.join(presets_dir(), os.path.basename(fname))
    if os.path.isfile(target):
        os.remove(target)


def create_preset_from_workflow(workflow_id: str) -> Dict[str, Any]:
    """拉取云端结构，自动生成映射骨架 + 参考清单。结构拉不到就抛插件错误。"""
    client_cls = core_bridge.core_client().RunningHubClient
    wf = core_bridge.core_workflow()

    from .. import appconfig

    api_key = appconfig.api_key()
    if not api_key:
        raise RuntimeError(
            "没有配置 API Key：请点应用右上角「设置」，在 API Key 一栏填入后保存。"
        )
    client = client_cls(api_key)
    structure = client.get_workflow_structure(str(workflow_id).strip())
    if not structure:
        err = client.last_structure_error()
        if err is not None:
            raise err
        raise RuntimeError(f"拉取工作流结构失败：{workflow_id}")

    injections: List[Dict[str, Any]] = []

    # 1) 提示词占位符字段：fieldValue 用整值占位符，运行时替换并保留类型
    for node_id, field, text in wf.iter_string_fields(structure):
        low = str(text)
        if "{positive}" in low and "{negative}" in low:
            # 同一字段同时要正负向（少见）——拆成两条会互相覆盖，保守不注入，
            # 只在 notes 里提示用户手工处理
            continue
        if "{positive}" in low:
            injections.append(
                {"nodeId": node_id, "fieldName": field, "fieldValue": "{positive}"}
            )
        elif "{negative}" in low:
            injections.append(
                {"nodeId": node_id, "fieldName": field, "fieldValue": "{negative}"}
            )

    # 2) 种子：优先采样器节点的 seed 字段
    targets = wf.find_seed_targets(structure)
    if targets:
        node_id, field = targets[0]
        injections.append(
            {"nodeId": node_id, "fieldName": field, "fieldValue": "{seed}"}
        )

    preset = {
        "name": f"工作流 {workflow_id}",
        "workflow_id": str(workflow_id).strip(),
        "injections": injections,
        "vae_file": "",
        "notes": wf.mapping_templates(structure),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_preset(preset)
    return preset
