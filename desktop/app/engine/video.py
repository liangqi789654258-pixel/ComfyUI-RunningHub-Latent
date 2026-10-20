"""帧序列 → mp4：ffmpeg 管道直喂 rawvideo，不落中间图片。

设计取舍：
  * 不用 imageio 的 writer —— 下一步 H3 要合音轨，必须自己控制第二条输入，
    与其维护两条编码路径，不如一开始就只留「调 ffmpeg」这一条；
  * 不用「写一堆临时 PNG 再读回来」——81 帧 480p 就是上百 MB 的来回 IO；
  * ffmpeg 可执行文件优先取 imageio-ffmpeg 自带的（ComfyUI venv 已装 0.6.0，
    含 ffmpeg 7.1 静态版），找不到再回落 PATH。

编码参数：libx264 / crf 17 / yuv420p / +faststart。
yuv420p 要求宽高为偶数，奇数时补 1 像素黑边（比报错友好）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import Any, List, Optional

from .frames import to_uint8_rgb

_DEFAULT_CRF = 17


def ffmpeg_exe() -> str:
    """定位 ffmpeg 可执行文件（imageio-ffmpeg 自带优先）。"""
    try:  # pragma: no cover - 依赖安装情况而定
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            return exe
    except Exception:
        pass
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise RuntimeError(
        "找不到 ffmpeg，无法生成 mp4。请在 ComfyUI 的 Python 环境里执行：\n"
        "    python -m pip install imageio-ffmpeg\n"
        "（本项目的 install.bat 会自动装好。）"
    )


def available() -> bool:
    """ffmpeg 是否可用（供界面提前给出人话提示，而不是等到最后一步炸）。"""
    try:
        ffmpeg_exe()
        return True
    except Exception:
        return False


def _even_pad_filter(width: int, height: int) -> List[str]:
    if width % 2 == 0 and height % 2 == 0:
        return []
    return ["-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2"]


def save_video(
    frames: Any,
    path: str,
    fps: int = 16,
    crf: int = _DEFAULT_CRF,
    on_stage=None,
    meta: Optional[dict] = None,
) -> str:
    """把 [N,H,W,C] / 0..1 的帧序列编码成 mp4，返回输出路径。

    meta 不为空时（{"prompt","workflow","rh_params"} 三合一），通过 ffmetadata
    临时文件写进容器的 comment 键 —— ffmpeg 会落成 ©cmt 原子，ComfyUI/VHS
    前端拖入即可还原工作流（见 embed.py 模块说明）。
    """
    arrs = [to_uint8_rgb(f) for f in frames]
    if not arrs:
        raise ValueError("没有可编码的帧（解码结果为空）。")
    height, width = arrs[0].shape[:2]
    for i, arr in enumerate(arrs):
        if arr.shape[:2] != (height, width):
            raise ValueError(
                f"第 {i + 1} 帧尺寸 {arr.shape[:2]} 与首帧 {(height, width)} 不一致，无法编码。"
            )

    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)

    # 元数据：ffmetadata 临时文件（避免 CLI 参数长度上限；workflow JSON 可能上百 KB）
    meta_path = None
    extra_inputs: List[str] = []
    map_args: List[str] = []
    if meta is not None:
        from .embed import build_ffmetadata_text

        fd, meta_path = tempfile.mkstemp(suffix=".ffmeta", text=True)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(build_ffmetadata_text(meta))
        # 元数据文件作为第 2 路输入，显式映射其全局元数据；流仍取第 1 路
        extra_inputs = ["-i", meta_path]
        map_args = ["-map", "0:v", "-map_metadata", "1"]

    cmd = [
        ffmpeg_exe(),
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        "-s", f"{width}x{height}",
        "-r", str(max(1, int(fps))),
        "-i", "-",
        *extra_inputs,
        "-an",
        *map_args,
        *_even_pad_filter(width, height),
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", str(int(crf)),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        path,
    ]
    try:
        _run_encode(cmd, arrs, path, on_stage)
    finally:
        if meta_path and os.path.isfile(meta_path):
            try:
                os.remove(meta_path)
            except OSError:
                pass
    return path


def _run_encode(cmd: List[str], arrs: List[Any], path: str, on_stage=None) -> None:
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        # GUI 里起控制台程序不许弹黑框（Windows）
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
    )
    total = len(arrs)
    try:
        assert proc.stdin is not None
        for i, arr in enumerate(arrs):
            proc.stdin.write(arr.tobytes())
            if on_stage is not None and (i % 5 == 0 or i == total - 1):
                on_stage(f"编码视频… {i + 1}/{total} 帧")
    finally:
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:
            pass

    err = b""
    if proc.stderr is not None:
        try:
            err = proc.stderr.read() or b""
        finally:
            proc.stderr.close()
    code = proc.wait()

    if code != 0 or not os.path.isfile(path) or os.path.getsize(path) == 0:
        detail = err.decode("utf-8", "ignore").strip()[-600:]
        raise RuntimeError(
            f"ffmpeg 编码 mp4 失败（退出码 {code}）。\n"
            f"{detail or '（ffmpeg 没有输出错误信息）'}"
        )
