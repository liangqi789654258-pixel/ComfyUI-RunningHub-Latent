"""音频侧工具：波形 → WAV 文件、mp4 合轨。零新依赖（wave + 已有 ffmpeg）。"""

from __future__ import annotations

import os
import subprocess
import wave
from typing import Any

import numpy as np

from .video import ffmpeg_exe


def write_wav(path: str, waveform: Any, sample_rate: int) -> str:
    """[S, 2] float 波形 → 16bit 立体声 WAV。

    波形值域约 [-1, 1]（解码侧已做 std 归一化），越界部分裁剪。
    """
    arr = np.clip(np.asarray(waveform, dtype=np.float32), -1.0, 1.0)
    if arr.ndim != 2 or arr.shape[1] != 2:
        raise ValueError(f"波形形状异常：{arr.shape}（期望 [S, 2] 立体声）")
    pcm = (arr * 32767.0).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(int(sample_rate))
        w.writeframes(pcm.tobytes())
    return path


def mux_audio(video_path: str, wav_path: str, on_stage=None) -> str:
    """把 WAV 音轨合进 mp4，覆盖原文件。

    - `-c:v copy`：视频流零重编码，画质与编码参数原样保留；
    - 默认 `-map_metadata 0`：全局元数据（含工作流 ©cmt）随视频流保留；
    - `-shortest`：以较短的一边为准（音画时长由云端对齐，正常一致）。
    """
    tmp = video_path + ".mux.tmp.mp4"
    cmd = [
        ffmpeg_exe(),
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-i", video_path,
        "-i", wav_path,
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        "-movflags", "+faststart",
        tmp,
    ]
    if on_stage is not None:
        on_stage("合成音轨…")
    proc = subprocess.run(cmd, capture_output=True)
    ok = proc.returncode == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 0
    if not ok:
        detail = proc.stderr.decode("utf-8", "ignore").strip()[-400:]
        if os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise RuntimeError(f"ffmpeg 合轨失败：{detail or '（无错误输出）'}")
    os.replace(tmp, video_path)
    return video_path
