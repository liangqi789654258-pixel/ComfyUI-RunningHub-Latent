"""P1 视频链路冒烟测试（不花云端钱、不动 GPU）：

1. 档位自动选择（纯函数）；
2. 像素参数 → decode_tiled latent 参数的换算（对照 VAEDecodeTiled 节点规则）；
3. 帧归一化（1/3/4 通道 → RGB uint8）；
4. ffmpeg 管道编码 mp4 + 回读帧数；
5. 真机 wan_2.1_vae：随机视频 latent 分块解码 → mp4（CPU 跑，几秒）。
"""

import os
import sys

APP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP)

os.environ["CUDA_VISIBLE_DEVICES"] = ""  # 强制 CPU

import numpy as np  # noqa: E402
import torch  # noqa: E402

from app.engine.decoder import VIDEO_TIERS, Decoder, pick_video_tier  # noqa: E402
from app.engine.frames import to_uint8_rgb  # noqa: E402
from app.engine.video import available as ffmpeg_available  # noqa: E402
from app.engine.video import save_video  # noqa: E402

failures = []


def check(name, fn):
    try:
        fn()
        print(f"[OK] {name}")
    except Exception as exc:
        failures.append(name)
        print(f"[FAIL] {name}: {exc}")


def t_pick_tier():
    # wan 2.1：空间 8× / 时间 4×。480p/33 帧 latent = [1,16,9,60,104]
    s = torch.zeros(1, 16, 9, 60, 104)
    assert pick_video_tier(s) == "480p", pick_video_tier(s)
    # 720p/81 帧 latent = [1,16,21,90,160]
    s = torch.zeros(1, 16, 21, 90, 160)
    assert pick_video_tier(s) == "720p", pick_video_tier(s)
    # 720p/161 帧（>121）→ 长片档
    s = torch.zeros(1, 16, 41, 90, 160)
    assert pick_video_tier(s) == "720p_long", pick_video_tier(s)
    # F=1 不该走到这里，但函数要稳
    s = torch.zeros(1, 16, 1, 60, 104)
    assert pick_video_tier(s) == "480p"


check("档位自动选择（480p/720p/长片）", t_pick_tier)


def t_tier_params():
    dec = Decoder()  # 未加载 VAE → compression() 走 8/4 兜底，正好是 Wan 的值
    s = torch.zeros(1, 16, 9, 60, 104)
    key, (tile, ov, ts, tov) = dec.resolve_pixel_tier(s, "auto")
    assert key == "480p", key
    # 档位是像素单位；换算到 latent 参数由 ComfyUI 的 VAEDecodeTiled 节点负责
    assert (tile, ov, ts, tov) == (384, 64, 32, 8), (tile, ov, ts, tov)
    key, (tile, ov, ts, tov) = dec.resolve_pixel_tier(
        torch.zeros(1, 16, 21, 90, 160), "720p"
    )
    assert key == "720p" and (tile, ov, ts, tov) == (320, 64, 16, 4), (key, tile, ov, ts, tov)
    # 未知档位名回落 auto
    key, _ = dec.resolve_pixel_tier(s, "bogus")
    assert key in VIDEO_TIERS, key


check("像素→latent 参数换算（对照 VAEDecodeTiled 规则）", t_tier_params)


def t_frames():
    g = np.random.rand(16, 32, 3).astype("float32")
    assert to_uint8_rgb(g).dtype == np.uint8 and to_uint8_rgb(g).shape == (16, 32, 3)
    a1 = np.random.rand(16, 32, 1).astype("float32")
    assert to_uint8_rgb(a1).shape[2] == 3, "灰度帧应摊成 3 通道"
    a4 = np.random.rand(16, 32, 4).astype("float32")
    assert to_uint8_rgb(a4).shape[2] == 3, "RGBA 帧应转 RGB"
    bad = np.random.rand(16, 32).astype("float32")
    try:
        to_uint8_rgb(bad)
        raise AssertionError("坏形状应报错")
    except ValueError:
        pass


check("帧归一化（1/3/4 通道 → RGB uint8）", t_frames)


def t_ffmpeg():
    assert ffmpeg_available(), "找不到 ffmpeg（imageio-ffmpeg 未装？）"
    frames = []
    for i in range(12):
        f = np.zeros((64, 96, 3), dtype="float32")
        f[:, int(i * 8 % 88): int(i * 8 % 88) + 8, 1] = 1.0
        frames.append(f)
    out = os.path.join(APP, "output", "_smoke_video.mp4")
    save_video(frames, out, fps=16, on_stage=print)
    assert os.path.isfile(out) and os.path.getsize(out) > 1000
    # 回读帧数
    import imageio

    reader = imageio.get_reader(out)
    n = sum(1 for _ in reader)
    reader.close()
    assert n == 12, f"回读帧数 {n} != 12"
    os.remove(out)


check("ffmpeg 管道编码 mp4 + 回读", t_ffmpeg)


def t_wan_decode():
    vae_path = r"C:\AI\dongshan\ComfyUI\models\vae\wan_2.1_vae.safetensors"
    if not os.path.isfile(vae_path):
        print("   （本地没有 wan_2.1_vae，跳过真机解码）")
        return
    dec = Decoder()
    dec.load(vae_path, on_stage=print)
    spatial, temporal = dec.compression()
    assert spatial == 8 and temporal == 4, (spatial, temporal)

    # 3 个 latent 帧 → 9 像素帧；16×16 latent → 128×128 画面（CPU 几秒内）
    latent = torch.randn(1, 16, 3, 16, 16)
    frames, tier_used, tile_used = dec.decode_video(latent, tier="auto", on_stage=print)
    assert frames.ndim == 4, f"归一化后应为 [N,H,W,C]，收到 {frames.shape}"
    assert frames.shape[0] == 9, f"9 像素帧，实际 {frames.shape[0]}"
    assert frames.shape[-1] == 3 and frames.shape[-2] == 128, frames.shape
    assert isinstance(tile_used, tuple) and len(tile_used) == 4, tile_used
    out = os.path.join(APP, "output", "_smoke_wan.mp4")
    save_video(frames, out, fps=16, on_stage=print)
    assert os.path.isfile(out) and os.path.getsize(out) > 1000
    print(f"   wan_2.1_vae: latent {tuple(latent.shape)} → {frames.shape} → mp4 ({os.path.getsize(out)} 字节, 档位 {tier_used})")
    os.remove(out)
    dec.close()


check("真机 wan_2.1_vae 分块解码 → mp4（CPU）", t_wan_decode)


print()
if failures:
    print(f"视频链路测试失败 {len(failures)} 项：{failures}")
    sys.exit(1)
print("视频链路测试全部通过")
