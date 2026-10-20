"""媒体元数据嵌入：把生成参数写进 PNG tEXt / mp4 ©cmt，零多余文件。

三个消费方、一份数据：
  * ComfyUI 拖图还原工作流 —— 读 `prompt`（API 格式）+ `workflow`（UI 格式），
    与 ComfyUI 原生 SaveImage 完全同款（nodes.py PngInfo）；
  * ComfyUI 拖 mp4 还原工作流 —— VHS 前端从文件尾部扫 `©cmt` data 原子取
    `.workflow`（videoinfo.js）。**注意**：ffmpeg 7.1 对 prompt/workflow 这类
    自定义键写的是 mdta 表，VHS 自己写的 mp4 前端读不到（本机三份真实产物
    字节级验证）；正确姿势是 ffmetadata 写 `comment=<合并 JSON>` 键，
    ffmpeg 会落成 ©cmt（已在同版本 ffmpeg 上实测命中）；
  * RH 出图器右键复用 —— 读 `rh_params`（PNG tEXt / 合并 JSON 内），无损回填。

安全红线：**绝不嵌入 API Key**。还原出的工作流里 api_key 恒为空串，
执行者自己填（或走插件 config.ini 兜底）。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Optional, Tuple

RH_PARAMS_KEY = "rh_params"
_WORKFLOW_VERSION = 0.4

# 插件专业版（RunningHubLatentGenerator）的 widgets_values 顺序，
# 与其 INPUT_TYPES required 字段声明顺序一致；seed 后面是 UI 自动加的
# control_after_generate。若插件改字段顺序，这里要跟着改（UI 画布还原用，
# 错位只影响拖入显示，不影响复用与 API 执行）。
_RH_WIDGET_ORDER = (
    "api_key",
    "workflow_id",
    "node_overrides",
    "positive",
    "negative",
    "seed",
    "__seed_control__",
    "timeout",
    "timeout_retries",
    "poll_interval",
    "use_cache",
    "use_high_performance",
    "validate_mapping",
)


# ---------------------------------------------------------------- 参数打包
def build_rh_params(
    params: Any,
    task_id: str,
    kind: str,
    tier_label: str = "",
    video_tile: Optional[Tuple[int, int, int, int]] = None,
    fps_used: Optional[int] = None,
) -> Dict[str, Any]:
    """GenerationParams → 可嵌入的参数字典（rh_params）。

    fps_used：AV 任务强制 24fps（H3 原生帧率），记录实际生效值。
    """
    return {
        "rh_standalone_params": 1,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "task_id": task_id,
        "kind": kind,
        "workflow_id": params.workflow_id,
        "preset_name": params.preset_name,
        "positive": params.positive,
        "negative": params.negative,
        "mapping": params.mapping_json,
        "seed": params.seed,
        "random_seed": params.random_seed,
        "vae_path": params.vae_path,
        "video_tier": tier_label or params.video_tier,
        "video_fps": int(fps_used or params.video_fps),
        "video_crf": params.video_crf,
        "video_tile": list(video_tile) if video_tile else None,
    }


def build_workflow_jsons(
    rh: Dict[str, Any]
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """构造 (API 格式 prompt, UI 格式 workflow)。

    还原出的链路：RH 云端采样·专业版 → VAELoader → VAEDecode(Tiled) → SaveImage。
    节点注册键已对照插件 NODE_CLASS_MAPPINGS 与 ComfyUI 原生节点核实。
    """
    kind = str(rh.get("kind") or "image")
    vae_name = os.path.basename(str(rh.get("vae_path") or ""))
    mapping = str(rh.get("mapping") or "")
    seed = int(rh.get("seed") or 0)

    # ---- 节点 1：RH 云端采样·专业版（api_key 恒为空，安全红线）
    rh_inputs = {
        "api_key": "",
        "workflow_id": str(rh.get("workflow_id") or ""),
        "node_overrides": mapping,
        "positive": str(rh.get("positive") or ""),
        "negative": str(rh.get("negative") or ""),
        "seed": seed,
        "timeout": int(rh.get("timeout") or 600),
        "timeout_retries": int(rh.get("timeout_retries") or 1),
        "poll_interval": float(rh.get("poll_interval") or 5.0),
        "use_cache": True,
        "use_high_performance": False,
        "validate_mapping": True,
    }

    # ---- 节点 3：解码（视频用 Tiled + 当时实际生效的分块参数；图片用普通 Decode）
    tile = rh.get("video_tile") if kind == "video" else None
    if kind == "video":
        p = tile or [256, 64, 16, 4]  # 缺记录时按最省显存档兜底
        decode_cls = "VAEDecodeTiled"
        decode_inputs = {
            "samples": ["1", 0],
            "vae": ["2", 0],
            "tile_size": int(p[0]),
            "overlap": int(p[1]),
            "temporal_size": int(p[2]),
            "temporal_overlap": int(p[3]),
        }
        decode_widgets = [int(p[0]), int(p[1]), int(p[2]), int(p[3])]
    else:
        decode_cls = "VAEDecode"
        decode_inputs = {"samples": ["1", 0], "vae": ["2", 0]}
        decode_widgets = []

    prompt = {
        "1": {
            "class_type": "RunningHubLatentGenerator",
            "inputs": rh_inputs,
            "_meta": {"title": "RH 云端采样 · 专业版"},
        },
        "2": {
            "class_type": "VAELoader",
            "inputs": {"vae_name": vae_name},
            "_meta": {"title": "VAE加载器"},
        },
        "3": {
            "class_type": decode_cls,
            "inputs": decode_inputs,
            "_meta": {"title": "VAE解码"},
        },
        "4": {
            "class_type": "SaveImage",
            "inputs": {"images": ["3", 0], "filename_prefix": "RH"},
            "_meta": {"title": "保存图像"},
        },
    }

    # ---- UI 格式：拖入后还原画布。专业版有 3 个输出（latent/task_id/report）。
    widgets = []
    for name in _RH_WIDGET_ORDER:
        if name == "__seed_control__":
            widgets.append("fixed")
        else:
            widgets.append(rh_inputs.get(name, ""))
    workflow = {
        "id": f"rh-{rh.get('task_id') or 'local'}",
        "revision": 0,
        "last_node_id": 4,
        "last_link_id": 3,
        "nodes": [
            {
                "id": 1,
                "type": "RunningHubLatentGenerator",
                "pos": [80, 120],
                "size": [420, 420],
                "flags": {},
                "order": 0,
                "mode": 0,
                "inputs": [],
                "outputs": [
                    {"name": "latent", "type": "LATENT", "links": [1]},
                    {"name": "task_id", "type": "STRING", "links": []},
                    {"name": "report", "type": "STRING", "links": []},
                ],
                "properties": {"Node name for S&R": "RunningHubLatentGenerator"},
                "widgets_values": widgets,
            },
            {
                "id": 2,
                "type": "VAELoader",
                "pos": [560, 140],
                "size": [320, 60],
                "flags": {},
                "order": 1,
                "mode": 0,
                "inputs": [],
                "outputs": [{"name": "VAE", "type": "VAE", "links": [2]}],
                "properties": {"Node name for S&R": "VAELoader"},
                "widgets_values": [vae_name],
            },
            {
                "id": 3,
                "type": decode_cls,
                "pos": [560, 260],
                "size": [320, 180],
                "flags": {},
                "order": 2,
                "mode": 0,
                "inputs": [
                    {"name": "samples", "type": "LATENT", "link": 1},
                    {"name": "vae", "type": "VAE", "link": 2},
                ],
                "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": [3]}],
                "properties": {"Node name for S&R": decode_cls},
                "widgets_values": decode_widgets,
            },
            {
                "id": 4,
                "type": "SaveImage",
                "pos": [940, 260],
                "size": [340, 120],
                "flags": {},
                "order": 3,
                "mode": 0,
                "inputs": [{"name": "images", "type": "IMAGE", "link": 3}],
                "outputs": [],
                "properties": {},
                "widgets_values": ["RH"],
            },
        ],
        "links": [
            [1, 1, 0, 3, 0, "LATENT"],
            [2, 2, 0, 3, 1, "VAE"],
            [3, 3, 0, 4, 0, "IMAGE"],
        ],
        "groups": [],
        "config": {},
        "extra": {"rh_standalone": True},
        "version": _WORKFLOW_VERSION,
    }
    return prompt, workflow


def build_meta(
    params: Any,
    task_id: str,
    kind: str,
    tier_label: str = "",
    video_tile: Optional[Tuple[int, int, int, int]] = None,
    fps_used: Optional[int] = None,
) -> Dict[str, Any]:
    """一步到位：{"prompt", "workflow", "rh_params"} 三合一（mp4 ©cmt 用）。"""
    rh = build_rh_params(params, task_id, kind, tier_label, video_tile, fps_used)
    prompt, workflow = build_workflow_jsons(rh)
    return {"prompt": prompt, "workflow": workflow, RH_PARAMS_KEY: rh}


# ---------------------------------------------------------------- 写入
def attach_png(path: str, meta: Dict[str, Any]) -> None:
    """给已保存的 PNG 补写元数据（无损重打包：读进内存 → 另存 → 原子替换）。

    与 ComfyUI 原生 SaveImage 完全同款：json.dumps 默认 ASCII 转义
    （中文变 \\uXXXX）→ 纯 tEXt 块，兼容性最好（iTXt 有个别工具不认）。
    meta 里缺哪个键就跳过哪块（兼容只写 prompt/workflow 的场景）。
    """
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo

    info = PngInfo()
    for key in ("prompt", "workflow", RH_PARAMS_KEY):
        if key in meta:
            info.add_text(key, json.dumps(meta[key], separators=(",", ":")))
    img = Image.open(path)
    try:
        img.load()
        tmp = path + ".meta.tmp"
        img.save(tmp, "PNG", pnginfo=info)
        os.replace(tmp, path)
    finally:
        img.close()


def _escape_ffmetadata(value: str) -> str:
    """ffmetadata 值转义（VHS 同款规则：\\ ; # = 与换行）。"""
    value = str(value)
    for ch, rep in (("\\", "\\\\"), (";", "\\;"), ("#", "\\#"),
                    ("=", "\\="), ("\n", "\\\n")):
        value = value.replace(ch, rep)
    return value


def build_ffmetadata_text(meta: Dict[str, Any]) -> str:
    """把三合一 JSON 放进 ffmetadata 的 comment 键（→ mp4 ©cmt）。"""
    blob = json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
    return ";FFMETADATA1\ncomment=" + _escape_ffmetadata(blob) + "\n"


# ---------------------------------------------------------------- 读取
def _parse_cmt_atom(data: bytes) -> Optional[str]:
    """照抄 VHS videoinfo.js 的扫描逻辑：从尾部找 ©cmt data 原子。"""
    offset = len(data) - 4
    while offset > 16:
        if data[offset:offset + 4] == b"data" and \
                data[offset - 8:offset - 4] == b"\xa9cmt":
            size = int.from_bytes(data[offset - 4:offset], "big") - 16
            return data[offset + 12:offset + 12 + size].decode("utf-8", "ignore")
        offset -= 1
    return None


def read_mp4_params(path: str) -> Optional[Dict[str, Any]]:
    """读 mp4 ©cmt 里的三合一 JSON（复用参数用）。"""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    raw = _parse_cmt_atom(data)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def read_png_params(path: str) -> Optional[Dict[str, Any]]:
    """读 PNG tEXt，返回 {"rh_params":…, "prompt":…, "workflow":…}（有什么返回什么）。"""
    try:
        from PIL import Image

        with Image.open(path) as img:
            text = dict(getattr(img, "text", {}) or {})
    except Exception:
        return None
    merged: Dict[str, Any] = {}
    for key in (RH_PARAMS_KEY, "prompt", "workflow"):
        if key not in text:
            continue
        try:
            merged[key] = json.loads(text[key])
        except json.JSONDecodeError:
            continue
    return merged or None


def _reconstruct_rh_params(meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """没有 rh_params 时，从 prompt（API 格式）反推出复用所需的字段。"""
    prompt = meta.get("prompt") or {}
    rh_node = None
    decode_node = None
    for node in prompt.values():
        if not isinstance(node, dict):
            continue
        cls = node.get("class_type")
        if cls == "RunningHubLatentGenerator":
            rh_node = node
        elif cls in ("VAEDecode", "VAEDecodeTiled"):
            decode_node = node
    if not rh_node:
        return None
    inputs = rh_node.get("inputs") or {}
    is_video = (decode_node or {}).get("class_type") == "VAEDecodeTiled"
    d_inputs = (decode_node or {}).get("inputs") or {}
    return {
        "rh_standalone_params": 1,
        "task_id": "",
        "kind": "video" if is_video else "image",
        "workflow_id": inputs.get("workflow_id") or "",
        "preset_name": "",
        "positive": inputs.get("positive") or "",
        "negative": inputs.get("negative") or "",
        "mapping": inputs.get("node_overrides") or "",
        "seed": int(inputs.get("seed") or 0),
        "random_seed": False,
        "vae_path": "",
        "video_tier": "",
        "video_fps": 16,
        "video_crf": 17,
        "video_tile": (
            [d_inputs.get("tile_size"), d_inputs.get("overlap"),
             d_inputs.get("temporal_size"), d_inputs.get("temporal_overlap")]
            if is_video else None
        ),
    }


def read_params(path: str) -> Optional[Dict[str, Any]]:
    """画廊右键复用的统一入口。

    优先级：rh_params（新图，无损）→ prompt/workflow 反推 → 旧版 sidecar 文件。
    返回 rh_params 字典；都没有返回 None。
    """
    meta: Optional[Dict[str, Any]] = None
    low = path.lower()
    if low.endswith(".png"):
        meta = read_png_params(path)
    elif low.endswith((".mp4", ".webm", ".mkv", ".mov")):
        meta = read_mp4_params(path)

    if meta:
        rh = meta.get(RH_PARAMS_KEY)
        if isinstance(rh, dict) and rh.get("rh_standalone_params"):
            return rh
        return _reconstruct_rh_params(meta)

    # 旧版 sidecar 兼容（历史文件，只读不写）
    sidecar = path + ".json"
    if os.path.isfile(sidecar):
        try:
            with open(sidecar, "r", encoding="utf-8") as fh:
                rh = json.load(fh)
            if isinstance(rh, dict) and "positive" in rh:
                rh.setdefault("video_tile", None)
                return rh
        except Exception:
            pass
    return None
