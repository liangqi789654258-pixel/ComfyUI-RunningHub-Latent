"""作品栏缩略图：视频取首帧，落磁盘缓存。

为什么要有缓存：拉首帧必须调 ffmpeg（约 80~300ms/个，视分辨率与关键帧位置），
输出目录一旦指向 Downloads 这类「重灾区」，每次刷新都现算会让界面卡死。
缓存 key = 绝对路径 + 文件大小 + mtime，同名覆盖/重新生成后自动失效。

不做 Qt 依赖：本模块只吐字节，QPixmap 转换留给 GUI 层（便于单测与离屏环境）。
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
from typing import Callable, Optional

from . import paths
from .engine import video as video_mod

# 缩略图在棋盘上一个方块是 ~160px（iconSize），给到 2 倍供 hidpi 屏用
THUMB_WIDTH = 640
_CACHE_SUBDIR = "thumb_cache"
_FFMPEG_TIMEOUT = 20.0  # 秒；坏文件/网络盘挂死时不至于永久阻塞
_POLL = 0.15            # 中止检查间隔（秒）
# GUI 进程里起 ffmpeg 不许弹黑框（Windows 上控制台程序会闪一下）
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def is_video(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in (".mp4", ".webm", ".mov", ".mkv")


def video_ffmpeg_ok() -> bool:
    """ffmpeg 可用才值得起线程抽帧（缺 ffmpeg 时作品栏保留占位图标）。"""
    return video_mod.available()


def cache_dir() -> str:
    d = os.path.join(paths.DATA_DIR, _CACHE_SUBDIR)
    os.makedirs(d, exist_ok=True)
    return d


def cache_path(video_path: str) -> str:
    """缓存文件名：路径+mtime+size 的哈希（内容一变 key 就变，无需主动清理）。"""
    abs_path = os.path.abspath(video_path)
    try:
        st = os.stat(abs_path)
        sig = f"{abs_path}|{st.st_size}|{int(st.st_mtime)}|{THUMB_WIDTH}"
    except OSError:
        sig = f"{abs_path}|0|0|{THUMB_WIDTH}"
    digest = hashlib.md5(sig.encode("utf-8")).hexdigest()[:20]
    return os.path.join(cache_dir(), digest + ".jpg")


def _extract_first_frame(
    video_path: str, should_stop: Optional[Callable[[], bool]] = None
) -> Optional[bytes]:
    """调 ffmpeg 抽首帧，输出 mjpeg（比 png 小一个数量级，图标用够了）。

    should_stop 每 150ms 问一次，返回 True 就 kill 掉 ffmpeg 立刻退出——
    这是关窗口/刷新列表时能立刻收线程的关键（大文件 ffmpeg 可能要读很久，
    干等 wait() 会超时，而 QThread 在运行中被销毁会导致进程 abort）。
    """
    if not video_mod.available():
        return None
    try:
        exe = video_mod.ffmpeg_exe()
    except Exception:
        return None
    cmd = [
        exe,
        "-v", "error",     # 只报错，避免 stderr 刷一堆 banner
        "-nostdin",        # 绝不吃 stdin（GUI 进程里老实用不到）
        "-y",
        "-i", video_path,
        "-frames:v", "1",  # 只取第一帧
        "-vf", f"scale={THUMB_WIDTH}:-2",
        "-f", "image2pipe",
        "-vcodec", "mjpeg",
        "-q:v", "3",
        "-",
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=_NO_WINDOW,
        )
    except OSError:
        return None

    deadline = time.monotonic() + _FFMPEG_TIMEOUT
    out = b""
    try:
        while True:
            try:
                out, _ = proc.communicate(timeout=_POLL)
                break
            except subprocess.TimeoutExpired:
                if should_stop is not None and should_stop():
                    return None
                if time.monotonic() > deadline:
                    return None
    finally:
        # 无论正常结束、中止还是超时，都不许把 ffmpeg 进程留在后台
        if proc.poll() is None:
            try:
                proc.kill()
                proc.communicate(timeout=2)
            except Exception:
                pass
    if proc.returncode != 0 or not out:
        return None
    return out


def thumb_bytes(
    video_path: str, should_stop: Optional[Callable[[], bool]] = None
) -> Optional[bytes]:
    """返回视频首帧缩略图的 JPEG 字节；失败（无 ffmpeg / 坏文件）返回 None。"""
    cached = cache_path(video_path)
    if os.path.isfile(cached):
        try:
            if os.path.getsize(cached) > 0:
                with open(cached, "rb") as fh:
                    return fh.read()
        except OSError:
            pass  # 缓存读不出来就当没命中，重新生成

    data = _extract_first_frame(video_path, should_stop=should_stop)
    if not data:
        return None

    # 原子写：先写 .tmp 再 replace，避免并发读到半截文件
    tmp = f"{cached}.{os.getpid()}.{id(data)}.tmp"
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, cached)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return data


def clear_cache(prefix: str = "") -> int:
    """清缓存（prefix 非空时只清文件名以它开头的），返回删除数量。

    缓存可以随时删：删掉只是下次要重新抽一次首帧，不影响任何产物。
    """
    d = cache_dir()
    removed = 0
    for name in os.listdir(d):
        if prefix and not name.startswith(prefix):
            continue
        try:
            os.unlink(os.path.join(d, name))
            removed += 1
        except OSError:
            pass
    return removed
