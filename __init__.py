"""
ComfyUI-RunningHub-Latent
=========================

把 RunningHub 云端当作「远程采样器」来用：

    本地 ComfyUI 画布
        -> 提交任务到 RunningHub 云端
        -> 云端跑完采样，输出 latent 二进制文件
        -> 本插件下载 latent 并还原为 ComfyUI 标准 LATENT
        -> 本地 VAE 解码出图（图像 / 视频均可）

这样做的好处不止于算力卸载：本地不必再放置十几 GB 的底模，显存压力大幅下降，
最终画质也由自己选的 VAE 决定。

设计上遵守三条底线：
  1. 不修改 ComfyUI 任何核心代码，放进 custom_nodes 即用；
  2. 不引入内核依赖，兼容 ComfyUI 0.10 以上主流版本；
  3. 输出严格是原生 LATENT，可直接接 VAEDecode / VAEDecodeTiled / 视频 VAE 节点。

请务必遵守法律法规与 RunningHub 平台规则使用。详见 README 的「合规与免责」。
"""

from __future__ import annotations

try:
    from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS, CATEGORY
except Exception:  # 依赖缺失时不要让整个 custom_nodes 加载炸掉
    import traceback

    print(
        "[RunningHub-Latent] 插件加载失败，请确认已安装依赖："
        "pip install -r requirements.txt"
    )
    traceback.print_exc()
    NODE_CLASS_MAPPINGS = {}
    NODE_DISPLAY_NAME_MAPPINGS = {}
    CATEGORY = "RunningHub"
    WEB_DIRECTORY = "web"

# 前端扩展目录：ComfyUI 会自动把这里面的 js 注入到画布。
# 用途是把运行期警告（例如提示词占位符没匹配上）在节点上标黄，
# 纯 Python 做不到给节点上色。
WEB_DIRECTORY = "web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
__version__ = "1.1.0"
