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

# 前端扩展目录：ComfyUI 会自动把这里面的 js 注入到画布。
# 用途：把运行期警告 / 报错诊断以 toast 形式弹出来，并把提示词输入框拉高。
# （ComfyUI 新版前端会把节点异常统一显示成「节点在执行过程中发生错误」，
#   自定义文案只有走自建事件通道才能到达用户。）
WEB_DIRECTORY = "web"


def _register_keyguard_routes():
    """注册 API Key 守卫的两个本地接口（仅在有 ComfyUI 服务端时生效）。

    - POST /runninghub_latent/save_key  ：前端把用户填写的 Key 转存进 config.ini
    - GET  /runninghub_latent/key_status：前端查询「是否已配置 + 脱敏预览」，
      绝不回传完整 Key。

    这是「Key 不进工作流 / 图片」方案的配套后端：前端在所有序列化出口
    （graphToPrompt / graph.serialize）清空 api_key 字段，执行时节点走
    「留空自动读取 config.ini」契约。
    """
    try:
        from aiohttp import web
        from server import PromptServer

        from .core import settings as _settings

        # 注意：PromptServer.instance 只在服务端真正启动后才存在。
        # 某些加载路径（CI 导入、单独 import 本包）里 server.py 能导入但
        # instance 是 None/缺失，这一步必须在 try 内，否则整个插件加载失败。
        routes = PromptServer.instance.routes
    except Exception:
        return  # 独立测试环境 / 老版本 ComfyUI / 无服务端，静默跳过

    @routes.post("/runninghub_latent/save_key")
    async def _rh_save_key(request):
        try:
            data = await request.json()
            key = str((data or {}).get("api_key") or "")
        except Exception:
            return web.json_response({"ok": False, "error": "bad json"}, status=400)
        ok = _settings.save_api_key(key)
        payload = {"ok": ok, "status": _settings.key_status()}
        if not ok:
            # 常见原因：插件目录只读（装到 Program Files / 受管环境）。
            # 必须把路径回给前端，否则用户点了「保存」却没有任何反馈。
            payload["config_path"] = _settings.ini_path()
            payload["error"] = "write_failed"
        return web.json_response(payload)

    @routes.get("/runninghub_latent/key_status")
    async def _rh_key_status(request):
        return web.json_response(_settings.key_status())


_register_keyguard_routes()


def _install_prompt_key_guard():
    """服务端兜底（官方钩子 add_on_prompt_handler）：执行前剥离本插件节点的 api_key。

    正常情况下前端 web/rh_key_guard.js 已在浏览器端（graphToPrompt / graph.serialize）
    剥离 Key；这层防的是前端扩展未加载（浏览器缓存 / 禁用 JS / 前端改版）导致的漏网。
    钩子在队列处理最前端触发（server.py trigger_on_prompt），prompt 对象随后被
    原样传给执行器并嵌入图片元数据——原位剥离一处即覆盖「执行输入 + PNG 元数据」。

    剥离后节点的 api_key 输入为空，执行走「留空自动读取 config.ini」契约，功能不变。
    只处理确实持有 api_key 的节点（widgets_values[0]）；
    RunningHubLatentFromUrl（首字段是 url）与 CacheManager（action）不碰。
    """
    try:
        from server import PromptServer
    except Exception:
        return  # 独立测试环境 / 老版本 ComfyUI，静默跳过

    _KEYHOLDER_TYPES = {
        "RunningHubLatentGeneratorSimple",
        "RunningHubLatentGenerator",
        "RunningHubLatentFetch",
        "RunningHubWorkflowInspector",
    }

    def _strip(data):
        if not isinstance(data, dict):
            return data
        try:
            prompt = data.get("prompt")
            if isinstance(prompt, dict):
                for node in prompt.values():
                    if not isinstance(node, dict):
                        continue
                    if str(node.get("class_type", "")) not in _KEYHOLDER_TYPES:
                        continue
                    inputs = node.get("inputs")
                    if isinstance(inputs, dict):
                        key = inputs.get("api_key")
                        if isinstance(key, str) and key.strip():
                            inputs["api_key"] = ""
            extra = data.get("extra_data")
            if isinstance(extra, dict):
                png_info = extra.get("extra_pnginfo")
                workflow = png_info.get("workflow") if isinstance(png_info, dict) else None
                if isinstance(workflow, dict):
                    for node in workflow.get("nodes", []) or []:
                        if not isinstance(node, dict):
                            continue
                        if str(node.get("type", "")) not in _KEYHOLDER_TYPES:
                            continue
                        wv = node.get("widgets_values")
                        if isinstance(wv, list) and wv and isinstance(wv[0], str) and wv[0].strip():
                            wv[0] = ""
        except Exception:
            pass  # 兜底逻辑自身绝不能阻断任务提交
        return data

    try:
        ps = PromptServer.instance
        if ps is not None and not getattr(ps, "__rh_key_guard", False):
            ps.add_on_prompt_handler(_strip)
            ps.__rh_key_guard = True
            print("[RunningHub-Latent] 服务端 Key 兜底已注册（执行/导出内容不含 Key）")
    except Exception:
        pass  # 拿不到服务端实例（极老版本）就跳过，前端守卫仍在


_install_prompt_key_guard()

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
__version__ = "1.5.0"
