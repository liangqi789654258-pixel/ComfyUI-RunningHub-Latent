"""
ComfyUI-RunningHub-Latent 冒烟测试

在没有 RunningHub 账号、没有真实云端环境的前提下，验证所有可以离线验证的核心逻辑：
  * latent 解析（新/旧格式、缩放因子、4D/5D/3D 维度）
  * 输出文件分类
  * 参数注入规范化与占位符
  * 缓存键稳定性
  * API 客户端状态机（用 mock session）
"""

import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.abspath(__file__))
# tests/ 的上一级就是插件根目录
PLUGIN = os.path.abspath(os.path.join(ROOT, ".."))

# 插件目录名含连字符，不能直接用真实目录名做包名。
# 这里注册一个虚拟包并把 __path__ 指向插件目录，从而让 nodes.py 里的
# 「from .core import ...」这类相对导入在测试环境下也能正常解析。
_pkg = types.ModuleType("rhplugin")
_pkg.__path__ = [PLUGIN]
sys.modules["rhplugin"] = _pkg

import torch
import safetensors.torch

from rhplugin.core import cache as cache_mod
from rhplugin.core import client as client_mod
from rhplugin.core import latent as latent_mod
from rhplugin.nodes import (
    _apply_placeholders,
    _classify,
    _collect_latents,
    _merge_latents,
    _normalize_overrides,
    _preview,
    RHUserError,
)

PASSED = []
FAILED = []


def check(name, cond, extra=""):
    if cond:
        PASSED.append(name)
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name} {extra}")


def save_latent(path, tensor, versioned=True, key="latent_tensor"):
    """完全复刻 ComfyUI 原生 SaveLatent 的存储结构。"""
    payload = {key: tensor.contiguous()}
    if versioned:
        payload["latent_format_version_0"] = torch.tensor([])
    safetensors.torch.save_file(payload, path, metadata={"prompt": "{}"})
    return path


# ---------------------------------------------------------------------------
print("\n=== 1. latent 解析：新版格式（带版本标记）不应缩放 ===")
tmp = tempfile.mkdtemp()
LEGACY = 1.0 / 0.18215

t4 = torch.ones(1, 4, 8, 8) * 2.0
p_new = save_latent(os.path.join(tmp, "ComfyUI_00001_.latent"), t4, versioned=True)
loaded = latent_mod.load_safetensors(p_new)
check("新版 latent 保持原始数值（不乘 1/0.18215）", torch.allclose(loaded, t4), f"got {loaded[0,0,0,0]}")

print("\n=== 2. latent 解析：旧版格式必须做缩放补偿 ===")
p_old = save_latent(os.path.join(tmp, "legacy.latent"), t4, versioned=False)
loaded_old = latent_mod.load_safetensors(p_old)
check("旧版 latent 正确乘以 1/0.18215", torch.allclose(loaded_old, t4 * LEGACY), f"got {loaded_old[0,0,0,0]}")

print("\n=== 3. 新版与旧版的倍数关系符合预期（约 5.49 倍） ===")
ratio = (loaded_old[0, 0, 0, 0] / loaded[0, 0, 0, 0]).item()
check("缩放倍率 ≈ 5.4922", abs(ratio - LEGACY) < 1e-4, f"ratio={ratio}")

print("\n=== 4. 视频 latent（5 维）不被误当成缺 batch 轴 ===")
t5 = torch.ones(1, 16, 5, 8, 8)
p_vid = save_latent(os.path.join(tmp, "video.latent"), t5, versioned=True)
loaded_vid = latent_mod.load_safetensors(p_vid)
res_vid = latent_mod.to_comfy_latent(loaded_vid, "video.latent")
check("5 维 latent 形状保持不变 [1,16,5,8,8]", tuple(res_vid["samples"].shape) == (1, 16, 5, 8, 8), str(tuple(res_vid['samples'].shape)))
check("LATENT 输出为标准 {'samples': tensor} 结构", set(res_vid.keys()) == {"samples"})

print("\n=== 5. 无 batch 轴的 3 维 latent 自动补维 ===")
t3 = torch.ones(4, 8, 8)
p3 = save_latent(os.path.join(tmp, "t3.latent"), t3, versioned=True)
res3 = latent_mod.to_comfy_latent(latent_mod.load_safetensors(p3), "t3.latent")
check("3 维补成 [1,4,8,8]", tuple(res3["samples"].shape) == (1, 4, 8, 8), str(tuple(res3['samples'].shape)))

print("\n=== 6. 旧版 key 名 'latents' 也能兼容 ===")
p_alt = save_latent(os.path.join(tmp, "alt.latent"), t4, versioned=True, key="latents")
try:
    loaded_alt = latent_mod.load_safetensors(p_alt)
    check("key='latents' 可被识别", torch.allclose(loaded_alt, t4))
except Exception as e:
    check("key='latents' 可被识别", False, str(e))

print("\n=== 7. 字节流解析 + safetensors 内容嗅探 ===")
with open(p_new, "rb") as f:
    blob = f.read()
check("looks_like_safetensors 识别真文件", latent_mod.looks_like_safetensors(blob))
check("looks_like_safetensors 拒绝随机字节", not latent_mod.looks_like_safetensors(b"not a latent at all!!"))
res_bytes = latent_mod.parse(blob, "https://rh-images.example.com/x/ComfyUI_00001_.latent")
check("从内存字节解析成功", tuple(res_bytes["samples"].shape) == (1, 4, 8, 8))

print("\n=== 8. 损坏文件给出中文可读错误而非崩溃 ===")
try:
    latent_mod.parse(b"\x99" * 64, "https://x/broken.latent")
    check("损坏文件抛出中文异常", False, "未抛异常")
except latent_mod.LatentParseError as e:
    check("损坏文件抛出中文异常", True)
    print(f"         消息示例：{str(e)[:80]}...")
except Exception as e:
    check("损坏文件抛出中文异常", False, type(e).__name__)

# ---------------------------------------------------------------------------
print("\n=== 9. 输出文件分类 ===")
cases = [
    ({"fileType": "latent", "fileUrl": "https://x/ComfyUI_00001_.latent"}, "latent"),
    ({"fileType": "png", "fileUrl": "https://x/a.png"}, "image"),
    ({"fileType": "mp4", "fileUrl": "https://x/a.mp4"}, "video"),
    ({"fileUrl": "https://x/ComfyUI_00002_.safetensors"}, "latent"),
    # 无扩展名但 fileType 是 latent —— RunningHub 真实返回里出现过
    ({"fileType": "latent", "fileUrl": "https://rh-images.example.com/abc123?x=1"}, "latent"),
]
for item, want in cases:
    got = _classify(item)
    check(f"分类 {item.get('fileUrl','')[:40]} -> {want}", got == want, f"got {got}")

print("\n=== 10. 多 latent 合并 / 缺 latent 报错 ===")
outs = [
    {"fileType": "latent", "fileUrl": "https://x/a.latent", "nodeId": "10"},
    {"fileType": "latent", "fileUrl": "https://x/b.latent", "nodeId": "11"},
    {"fileType": "png", "fileUrl": "https://x/c.png", "nodeId": "9"},
]
picked = _collect_latents(outs)
check("正确筛出 2 个 latent", len(picked) == 2, str(len(picked)))
check("按 nodeId 稳定排序", picked[0]["nodeId"] == "10")
merged = _merge_latents([torch.ones(1, 4, 8, 8), torch.ones(1, 4, 8, 8)])
check("同形状沿 batch 合并为 [2,4,8,8]", tuple(merged.shape) == (2, 4, 8, 8))
mism = _merge_latents([torch.ones(1, 4, 8, 8), torch.ones(1, 4, 16, 16)])
check("形状不一致时回退取第一个", tuple(mism.shape) == (1, 4, 8, 8))

try:
    _collect_latents([{"fileType": "png", "fileUrl": "https://x/only.png", "nodeId": "9"}])
    check("无 latent 输出时抛出指引性错误", False)
except RHUserError as e:
    check("无 latent 输出时抛出指引性错误", "保存Latent" in str(e))
    print(f"         消息示例：{str(e).splitlines()[0]}")

# ---------------------------------------------------------------------------
print("\n=== 11. 参数注入规范化 ===")
check("空字符串 -> 空列表", _normalize_overrides("") == [])
check("'[]' -> 空列表", _normalize_overrides("[]") == [])
norm = _normalize_overrides(
    '[{"nodeId": "6", "fieldName": "text", "fieldValue": "1 girl"}]'
)
check("标准 JSON 解析", norm[0]["nodeId"] == "6" and norm[0]["fieldValue"] == "1 girl")

# 用户误写成 value
norm2 = _normalize_overrides('[{"nodeId": "6", "fieldName": "text", "value": "hi"}]')
check("自动纠正 value -> fieldValue", norm2[0]["fieldValue"] == "hi")

# 字典简写
norm3 = _normalize_overrides({"3": {"seed": 123, "steps": 20}, "6": "prompt text"})
check("字典简写展开为 3 项", len(norm3) == 3, str(norm3))
check("字典简写保留 nodeId", all("nodeId" in x and "fieldName" in x for x in norm3))

try:
    _normalize_overrides('[{"nodeId": "6",]')
    check("非法 JSON 抛中文错误", False)
except RHUserError as e:
    check("非法 JSON 抛中文错误", "行" in str(e) or "列" in str(e))

print("\n=== 12. 占位符绑定 ===")
resolved = _apply_placeholders(
    [
        {"nodeId": "3", "fieldName": "seed", "fieldValue": "{seed}"},
        {"nodeId": "6", "fieldName": "text", "fieldValue": "{positive}"},
        {"nodeId": "7", "fieldName": "text", "fieldValue": "raw text"},
    ],
    {"seed": 42, "positive": "a cat"},
)
check("{seed} 被替换为整数值", resolved[0]["fieldValue"] == 42, str(resolved[0]["fieldValue"]))
check("{positive} 被替换", resolved[1]["fieldValue"] == "a cat")
check("无占位符原样保留", resolved[2]["fieldValue"] == "raw text")
try:
    _apply_placeholders(
        [{"nodeId": "3", "fieldName": "seed", "fieldValue": "{unknown_thing}"}], {"seed": 1}
    )
    check("未知占位符报错", False)
except RHUserError as e:
    check("未知占位符报错", "unknown_thing" in str(e))

# ---------------------------------------------------------------------------
print("\n=== 13. 缓存键稳定性 ===")
k1 = cache_mod.compute_cache_key("wf1", [{"nodeId": "3", "fieldName": "seed", "fieldValue": 1}])
k2 = cache_mod.compute_cache_key("wf1", [{"nodeId": "3", "fieldName": "seed", "fieldValue": 1}])
k3 = cache_mod.compute_cache_key("wf1", [{"nodeId": "3", "fieldName": "seed", "fieldValue": 2}])
# 顺序不同但语义相同
k4 = cache_mod.compute_cache_key(
    "wf1",
    [
        {"nodeId": "3", "fieldName": "seed", "fieldValue": 1},
        {"nodeId": "6", "fieldName": "text", "fieldValue": "a"},
    ],
)
k5 = cache_mod.compute_cache_key(
    "wf1",
    [
        {"nodeId": "6", "fieldName": "text", "fieldValue": "a"},
        {"nodeId": "3", "fieldName": "seed", "fieldValue": 1},
    ],
)
k6 = cache_mod.compute_cache_key("wf2", [{"nodeId": "3", "fieldName": "seed", "fieldValue": 1}])
check("相同参数 -> 相同键", k1 == k2)
check("参数变化 -> 不同键", k1 != k3)
check("参数顺序无关（已归一化）", k4 == k5)
check("工作流 ID 参与哈希", k1 != k6)
check("键为 32 位 md5", len(k1) == 32)

# ---------------------------------------------------------------------------
print("\n=== 14. API 客户端状态机（mock） ===")


class FakeResponse:
    def __init__(self, body):
        self._body = body
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class FakeSession:
    """按脚本顺序返回预设响应，模拟 排队->运行->成功 的完整生命周期。"""

    def __init__(self, script):
        self.script = list(script)
        self.payloads = []

    def post(self, url, data=None, headers=None, timeout=None):
        import json as _json

        self.payloads.append((url, data))
        return FakeResponse(self.script.pop(0))

    def get(self, url, params=None, timeout=None):
        return FakeResponse({"code": 0, "data": {}})


script = [
    # 第 1 次调用是 create_task
    {"code": 0, "msg": "success", "data": {"taskId": "1980471280073846785"}},
    # 之后依次是 排队 -> 运行 -> 成功
    {"code": 813, "msg": "queued", "data": None},
    {"code": 804, "msg": "running", "data": None},
    {
        "code": 0,
        "msg": "success",
        "data": [{"fileUrl": "https://x/ComfyUI_00001_.latent", "fileType": "latent", "nodeId": "17"}],
    },
]
sess = FakeSession(script)
client = client_mod.RunningHubClient(api_key="fake-key-0123456789abcdef", session=sess)
task_id = client.create_task("1111", [{"nodeId": "3", "fieldName": "seed", "fieldValue": 1}])
check("create_task 返回 taskId", task_id == "1980471280073846785", str(task_id))

# create_task 必须走 POST /task/openapi/create，且 body 用 fieldValue 字段
sent_url, sent_body = sess.payloads[0]
import json as _json_check

sent = _json_check.loads(sent_body.decode("utf-8"))
check("请求端点正确", sent_url.endswith("/task/openapi/create"), sent_url)
check("载荷含 workflowId", sent.get("workflowId") == "1111")
check("注入字段使用 fieldValue", sent["nodeInfoList"][0].get("fieldValue") == 1, str(sent))

state = client.fetch_outputs("t1")
check("813 -> queued", state["state"] == "queued")
state = client.fetch_outputs("t1")
check("804 -> running", state["state"] == "running")
state = client.fetch_outputs("t1")
check("code 0 -> success 且返回结果列表", state["state"] == "success" and len(state["outputs"]) == 1)

fail_sess = FakeSession(
    [
        {
            "code": 805,
            "msg": "failed",
            "data": {
                "failedReason": {
                    "node_name": "KSampler",
                    "exception_message": "Allocation on device",
                    "traceback": "File x",
                }
            },
        }
    ]
)
fail_client = client_mod.RunningHubClient(api_key="k", session=fail_sess)
try:
    fail_client.fetch_outputs("t2")
    check("805 抛出 TaskFailedError", False)
except client_mod.TaskFailedError as e:
    check("805 抛出 TaskFailedError", "KSampler" in str(e) and "Allocation" in str(e))
    print(f"         消息示例：{e}")
    check("错误信息包含云端节点名", e.node_name == "KSampler")

# API Key 泄漏检查
print("\n=== 15. 密钥脱敏 ===")
masked = client_mod.RunningHubClient._mask("key abcdefghij0123456789abcdef leaked")
check("长串密钥被打码", "****" in masked, masked)
check("脱敏后不再包含完整密钥", "abcdefghij0123456789" not in masked, masked)
check("短串不会被误伤", client_mod.RunningHubClient._mask("shortword") == "shortword")

# ---------------------------------------------------------------------------
# 以下为 v1.1 改造新增的回归测试，覆盖文档里列出的每一个 P0/P1 缺陷
# ---------------------------------------------------------------------------

from rhplugin.core import workflow as wf_mod
from rhplugin.core import settings as settings_mod
from rhplugin.nodes import _merge_injections, _output_type_of

print("\n=== 16. 输出类型识别：字段名与大小写都要兼容 ===")
# 用户实测反馈：云端返回的是 outputType（首字母大写），不是 fileType
check("outputType=Latent 识别为 latent",
      _classify({"outputType": "Latent", "fileUrl": "https://x/a.bin"}) == "latent")
check("fileType=LATENT 识别为 latent",
      _classify({"fileType": "LATENT", "fileUrl": "https://x/a.bin"}) == "latent")
check("type=latent 识别为 latent",
      _classify({"type": "latent", "fileUrl": "https://x/a.bin"}) == "latent")
check("outputType=Image 识别为 image",
      _classify({"outputType": "Image", "fileUrl": "https://x/a.png"}) == "image")
check("大小写不敏感", _output_type_of({"outputType": " LATENT "}) == "latent")
check("字段缺失时返回空串", _output_type_of({}) == "")
check("带查询串的 latent 链接仍可识别",
      _classify({"fileUrl": "https://x/ComfyUI_00001_.latent?token=abc"}) == "latent")

print("\n=== 17. 输出列表抽取：多种响应形态 ===")
extract = client_mod.RunningHubClient._extract_outputs
check("data 直接是 list", extract([{"a": 1}]) == [{"a": 1}])
check("data = {results: [...]}", extract({"results": [{"a": 1}]}) == [{"a": 1}])
check("data = {outputs: [...]}", extract({"outputs": [{"a": 1}]}) == [{"a": 1}])
check("data 是单个文件 dict", extract({"fileUrl": "u"}) == [{"fileUrl": "u"}])
check("空/异常输入返回空列表", extract(None) == [] and extract({}) == [])

status_of = client_mod.RunningHubClient._status_of
check("从 data 里读 status", status_of({"status": "SUCCESS"}, {}) == "SUCCESS")
check("从 body 里读 status", status_of(None, {"status": "success"}) == "SUCCESS")
check("都没有时返回空串", status_of({}, {}) == "")

print("\n=== 18. P0 修复：任务成功但文件未同步，必须补查而不是报错 ===")


class EmptyThenFullSession:
    """第一次返回空 results（模拟对象存储延迟），第二次才有文件。"""

    def __init__(self):
        self.calls = 0

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls += 1
        if self.calls == 1:
            return FakeResponse({"code": 0, "data": {"status": "SUCCESS", "results": []}})
        return FakeResponse(
            {
                "code": 0,
                "data": {"status": "SUCCESS",
                         "results": [{"fileUrl": "https://x/a.latent", "outputType": "Latent"}]},
            }
        )

    def get(self, url, params=None, timeout=None):
        return FakeResponse({"code": 0, "data": {}})


sess = EmptyThenFullSession()
c = client_mod.RunningHubClient(api_key="k" * 24, session=sess)
outs = c.wait_for_outputs("t1", poll_interval=0.01, timeout=5.0)
check("补查后成功拿到输出", len(outs) == 1, f"实际 {len(outs)} 个")
check("确实发起了第二次查询", sess.calls == 2, f"共查询 {sess.calls} 次")


class AlwaysEmptySession:
    def post(self, url, data=None, headers=None, timeout=None):
        return FakeResponse({"code": 0, "data": {"status": "SUCCESS", "results": []}})

    def get(self, url, params=None, timeout=None):
        return FakeResponse({"code": 0, "data": {}})


c2 = client_mod.RunningHubClient(api_key="k" * 24, session=AlwaysEmptySession())
outs2 = c2.wait_for_outputs("t1", poll_interval=0.01, timeout=5.0, empty_retries=1)
check("始终为空时返回空列表（由调用方报错）", outs2 == [])

print("\n=== 19. 工作流结构归一化 ===")
wf_prompt = {
    "3": {"class_type": "KSampler", "inputs": {"seed": 1, "steps": 20}},
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "{positive}"}},
}
norm = wf_mod.normalize(wf_prompt)
check("ComfyUI prompt 格式", set(norm.keys()) == {"3", "6"})
check("保留 class_type", norm["6"]["class_type"] == "CLIPTextEncode")

import json as _json

norm2 = wf_mod.normalize({"prompt": _json.dumps(wf_prompt)})
check("prompt 为 JSON 字符串时也能解析", set(norm2.keys()) == {"3", "6"})

norm3 = wf_mod.normalize(
    {"nodes": [{"id": 3, "type": "KSampler", "inputs": {"seed": 1}}]}
)
check("nodes 列表格式", set(norm3.keys()) == {"3"} and norm3["3"]["class_type"] == "KSampler")
check("空输入不炸", wf_mod.normalize(None) == {} and wf_mod.normalize({}) == {})

print("\n=== 20. P0 修复：提示词全局替换 ===")
struct = {
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "{positive}"}},
    "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "{negative}"}},
    "3": {"class_type": "KSampler", "inputs": {"seed": 1}},
}
inj, stats = wf_mod.build_prompt_injections(struct, "1girl", "bad")
check("正向命中 1 处", stats["positive"] == 1, str(stats))
check("负向命中 1 处", stats["negative"] == 1, str(stats))
check("生成 2 条注入", len(inj) == 2)
by_node = {i["nodeId"]: i["fieldValue"] for i in inj}
check("正向被替换为实际提示词", by_node.get("6") == "1girl")
check("负向被替换为实际提示词", by_node.get("7") == "bad")

# 多节点 + 前后缀 + 大小写变体
struct2 = {
    "1": {"class_type": "A", "inputs": {"text": "masterpiece, {positive}, best quality"}},
    "2": {"class_type": "B", "inputs": {"text": "{Positive} and {NEGATIVE}"}},
    "9": {"class_type": "C", "inputs": {"text": "no placeholder here"}},
}
inj2, stats2 = wf_mod.build_prompt_injections(struct2, "cat", "ugly")
check("前后缀保留",
      any(i["fieldValue"] == "masterpiece, cat, best quality" for i in inj2))
check("大小写变体也认",
      any(i["fieldValue"] == "cat and ugly" for i in inj2))
check("无占位符的节点不产生注入", len(inj2) == 2)
check("统计正确", stats2["positive"] == 2 and stats2["negative"] == 1, str(stats2))

# 关键：一个占位符都没有时必须能被检测出来（这是静默失效的根因）
inj3, stats3 = wf_mod.build_prompt_injections(
    {"1": {"class_type": "A", "inputs": {"text": "fixed prompt"}}}, "cat", "ugly"
)
check("无占位符时命中数为 0（用于触发警告）",
      stats3["positive"] == 0 and stats3["negative"] == 0 and inj3 == [])

print("\n=== 21. 种子自动定位 ===")
seeds = wf_mod.find_seed_targets(struct)
check("定位到 KSampler 的 seed", seeds == [("3", "seed")], str(seeds))
seeds2 = wf_mod.find_seed_targets(
    {"1": {"class_type": "RandomNoise", "inputs": {"noise_seed": 5}}}
)
check("无采样器时退化为任意 seed 字段", seeds2 == [("1", "noise_seed")], str(seeds2))
check("完全没有 seed 字段时返回空",
      wf_mod.find_seed_targets({"1": {"class_type": "A", "inputs": {"x": 1}}}) == [])

print("\n=== 22. 参数映射合法性校验 ===")
probs = wf_mod.validate_injections(struct, [{"nodeId": "999", "fieldName": "seed"}])
check("nodeId 不存在被检出", len(probs) == 1 and "不存在" in probs[0], str(probs))
probs2 = wf_mod.validate_injections(struct, [{"nodeId": "3", "fieldName": "nope"}])
check("fieldName 不存在被检出", len(probs2) == 1 and "没有字段" in probs2[0], str(probs2))
check("合法映射无告警",
      wf_mod.validate_injections(struct, [{"nodeId": "3", "fieldName": "seed"}]) == [])
check("结构不可得时不误报",
      wf_mod.validate_injections({}, [{"nodeId": "3", "fieldName": "seed"}]) == [])

# 不变量 A：连线字段不得注入（字段值是 [源节点, 槽位] 说明已被上游接管）
struct_linked = {
    "3": {"class_type": "KSampler", "inputs": {"seed": 1, "positive": ["6", 0]}},
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "{positive}"}},
}
probs3 = wf_mod.validate_injections(struct_linked, [{"nodeId": "3", "fieldName": "positive"}])
check("连线字段被拒绝（不变量 A）", len(probs3) == 1 and "连线" in probs3[0], str(probs3))
check("连线字段报错里点名来源节点", "nodeId=6" in probs3[0], str(probs3))
check("同一节点的字面量字段仍然合法",
      wf_mod.validate_injections(struct_linked, [{"nodeId": "6", "fieldName": "text"}]) == [])

# 逐条诊断：与 injections 一一对应（803 逐条定位依赖这个对齐关系）
diags = wf_mod.diagnose_injections(
    struct_linked,
    [
        {"nodeId": "6", "fieldName": "text"},
        {"nodeId": "999", "fieldName": "seed"},
        {"nodeId": "3", "fieldName": "positive"},
    ],
)
check("diagnose_injections 与入参一一对应", len(diags) == 3, str(diags))
check("合法的那一格是 None", diags[0] is None, str(diags[0]))
check("不合法的两格分别给出原因",
      diags[1] is not None and diags[2] is not None and diags[1] != diags[2], str(diags))

print("\n=== 23. 注入项合并：手工映射优先于自动定位 ===")
auto = [{"nodeId": "6", "fieldName": "text", "fieldValue": "自动"}]
manual = [{"nodeId": "6", "fieldName": "text", "fieldValue": "手工"}]
merged = _merge_injections(auto, manual)
check("同字段只保留一条", len(merged) == 1)
check("后者覆盖前者（手工优先）", merged[0]["fieldValue"] == "手工")
merged2 = _merge_injections(
    [{"nodeId": "3", "fieldName": "seed", "fieldValue": 1}],
    [{"nodeId": "6", "fieldName": "text", "fieldValue": "x"}],
)
check("不同字段都保留", len(merged2) == 2)

print("\n=== 24. config.ini 解析 ===")
import tempfile

tmp_ini = os.path.join(tempfile.mkdtemp(), "config.ini")
with open(tmp_ini, "w", encoding="utf-8") as fh:
    fh.write(
        "[runninghub]\n"
        "api_key = MY_TEST_KEY_1234567890\n"
        "default_workflow_id = 12345\n"
        "timeout = 900\n"
        "poll_interval = 3\n"
    )
parsed = settings_mod._read_ini(tmp_ini)
check("读到 api_key", parsed.get("api_key") == "MY_TEST_KEY_1234567890")
check("数字字段被转成 float", parsed.get("timeout") == 900.0, str(parsed.get("timeout")))
check("轮询间隔被转成 float", parsed.get("poll_interval") == 3.0)

with open(tmp_ini, "w", encoding="utf-8") as fh:
    fh.write("[runninghub]\napi_key = \n")
parsed2 = settings_mod._read_ini(tmp_ini)
check("空值保留为空串（交由 resolve 兜底）", parsed2.get("api_key") == "")

print("\n=== 25. 极简版节点定义完整性（真·零配置契约） ===")
from rhplugin.nodes import RunningHubLatentGeneratorSimple as Simple

it = Simple.INPUT_TYPES()
req = it["required"]
check("极简版含字段 api_key", "api_key" in req)
check("极简版含字段 workflow_id", "workflow_id" in req)
# 契约最终定稿（用户二次拍板）：极简版回到「只有两个输入框」，且**没有**
# 参数映射入口——一切参数都用云端的，本地只负责触发与解码。
# node_overrides 短暂加入后移除：极简版要改参数就该换专业版。
check(
    "极简版只有这两个输入框（api_key / workflow_id）",
    set(req.keys()) == {"api_key", "workflow_id"},
    f"实际字段：{sorted(req.keys())}",
)
check("极简版不含 node_overrides（映射入口只属于专业版）", "node_overrides" not in req)
check("极简版声明了 hidden unique_id（用于前端上色）",
      it.get("hidden", {}).get("unique_id") == "UNIQUE_ID")
check("极简版显示名为中文", getattr(Simple, "TITLE", "").startswith("RH 云端采样"))
check("极简版每次执行都强制重算（保证随机种子出新图）",
      Simple.IS_CHANGED(api_key="", workflow_id="") != Simple.IS_CHANGED(api_key="", workflow_id=""))

print("\n=== 26. 专业版提示词输入框只显示 positive / negative ===")
from rhplugin.nodes import RunningHubLatentGenerator as Pro

pit = Pro.INPUT_TYPES()["required"]
check("专业版 positive 的可见文本就是 positive",
      pit["positive"][1].get("placeholder") == "positive")
check("专业版 negative 的可见文本就是 negative",
      pit["negative"][1].get("placeholder") == "negative")
check("专业版参数映射默认留空（新手不被示例 JSON 吓退）",
      pit["node_overrides"][1].get("default") == "")
check("专业版参数映射带悬停说明", "nodeId" in pit["node_overrides"][1].get("tooltip", ""))

# ---------------------------------------------------------------------------
print("\n=== 27. 推倒连线兜底：没有占位符就不再产生任何注入 ===")
# 契约变更：「沿采样器 positive/negative 连线向上游回溯找文本字段」的兜底已删除。
# 下面这些结构以前能靠兜底命中，现在必须 0 命中、0 注入 —— 猜错就是静默失效，
# 而 0 命中会由调用方（nodes.py）转成明确报错，比悄悄打一发无效注入好得多。

# 用户真实场景：getJsonApiFormat 只返回采样器与连线，看不到文本节点的 widget 值
struct_link = {
    "272": {"class_type": "EmptyLatentImage", "inputs": {"width": 768, "height": 512}},
    "270": {"class_type": "CLIPTextEncode", "inputs": {"text": "cloud default", "clip": ["266", 0]}},
    "263": {"class_type": "CLIPTextEncode", "inputs": {"text": "cloud negative", "clip": ["266", 0]}},
    "266": {"class_type": "CLIPTextEncode", "inputs": {"text": "lora trigger words"}},
    "152": {"class_type": "KSampler", "inputs": {"seed": 1, "positive": ["270", 0], "negative": ["263", 0]}},
    "161": {"class_type": "KSamplerAdvanced", "inputs": {"noise_seed": 2, "positive": ["270", 0], "negative": ["263", 0]}},
}
inj_l, stats_l = wf_mod.build_prompt_injections(struct_link, "1girl", "lowres")
check("契约变更：不再沿采样器连线兜底（正向 0 命中）", stats_l["positive"] == 0, str(stats_l))
check("契约变更：不再沿采样器连线兜底（负向 0 命中）", stats_l["negative"] == 0, str(stats_l))
check("契约变更：不再产生任何注入", inj_l == [], str(inj_l))
check("绝不污染非提示词节点（266 是共享触发词，被覆盖就是画面崩坏）",
      all(i["nodeId"] != "266" for i in inj_l))

diag_l = wf_mod.prompt_link_diagnosis(struct_link, "positive")
check("诊断语义改为「指引手写映射」而非「解释猜测失败」",
      "参数映射" in diag_l and "占位符" in diag_l, diag_l)
check("诊断给出可直接复制的模板", "{positive}" in diag_l and "nodeId" in diag_l, diag_l)
check("诊断落到具体 nodeId（含数字，能定位）", any(ch.isdigit() for ch in diag_l), diag_l)

# 连线指向的节点不在结构里（部分返回缺节点）-> 0 命中，行为与推倒前一致
struct_missing = {
    "3": {"class_type": "KSampler", "inputs": {"seed": 1, "positive": ["999", 0], "negative": ["998", 0]}},
}
inj_m, stats_m = wf_mod.build_prompt_injections(struct_missing, "a", "b")
check("连线源缺失时安全放弃（保持 0 命中）",
      stats_m["positive"] == 0 and stats_m["negative"] == 0 and inj_m == [])

# 占位符主路径不受推倒影响：这是作者显式声明的契约，必须继续成立
inj_p, stats_p = wf_mod.build_prompt_injections(struct, "1girl", "bad")
check("占位符主路径仍然只替换写了标记的节点",
      all(i["nodeId"] in ("6", "7") for i in inj_p) and len(inj_p) == 2)

# 正负连线指向同一个节点：不再有「positive 优先认领」这种猜测行为
struct_same = {
    "270": {"class_type": "CLIPTextEncode", "inputs": {"text": "same node"}},
    "3": {"class_type": "KSampler", "inputs": {"seed": 1, "positive": ["270", 0], "negative": ["270", 0]}},
}
inj_s, stats_s = wf_mod.build_prompt_injections(struct_same, "pos", "neg")
check("契约变更：正负同节点也不再自动注入",
      stats_s["positive"] == 0 and stats_s["negative"] == 0 and inj_s == [], str(stats_s))

print("\n=== 28. 文本处理节点接在前面：不再回溯，落点交回给用户手写 ===")
# 契约变更：原「沿上游回溯到唯一可写字段」的逻辑已删除。它同时踩两条禁令：
#   * 架构文档 §3.5「唯一即默认采用」——上游只有一个字符串字段不代表它就是提示词；
#   * S0 fixture c5 已给出反例：唯一字段是模板前缀 prefix，覆盖它会破坏模板。

# 用户场景：CLIPTextEncode 的 text 被上游「字符串拼接/通配符」节点接管，
# 此时往编码节点注入 text 会被上游覆盖 —— 属于最坏的静默失效。
struct_chain = {
    "272": {"class_type": "EmptyLatentImage", "inputs": {"width": 768, "height": 1152}},
    "270": {"class_type": "CLIPTextEncode", "inputs": {"text": ["300", 0], "clip": ["266", 0]}},
    "263": {"class_type": "CLIPTextEncode", "inputs": {"text": ["301", 0], "clip": ["266", 0]}},
    "300": {"class_type": "StringConcat", "inputs": {"text_a": "prefix, ", "text_b": "old prompt"}},
    "301": {"class_type": "StringConcat", "inputs": {"text_a": "bad, ", "text_b": "old neg"}},
    "152": {"class_type": "KSampler", "inputs": {"seed": 1, "positive": ["270", 0], "negative": ["263", 0]}},
}
inj_c, stats_c = wf_mod.build_prompt_injections(struct_chain, "1girl", "lowres")
# 上游有两个字符串字段且都没占位符 -> 不猜，保持 0 命中（现在也不回溯了）
check("上游多字段且无占位符时不猜（保持 0 命中）",
      stats_c["positive"] == 0 and stats_c["negative"] == 0 and inj_c == [], str(stats_c))
check("多字段场景的诊断点名候选节点",
      "nodeId=300" in wf_mod.prompt_link_diagnosis(struct_chain, "positive"),
      wf_mod.prompt_link_diagnosis(struct_chain, "positive"))
check("多字段场景的诊断说明「机器无法判断该注入哪一个」",
      "多个文本字段" in wf_mod.prompt_link_diagnosis(struct_chain, "positive"),
      wf_mod.prompt_link_diagnosis(struct_chain, "positive"))

# 上游只有一个字符串字段 -> 唯一确定，直接注入上游
struct_one = {
    "270": {"class_type": "CLIPTextEncode", "inputs": {"text": ["300", 0]}},
    "263": {"class_type": "CLIPTextEncode", "inputs": {"text": ["301", 0]}},
    "300": {"class_type": "StringProcessor", "inputs": {"text": "old positive"}},
    "301": {"class_type": "StringProcessor", "inputs": {"text": "old negative"}},
    "152": {"class_type": "KSampler", "inputs": {"seed": 1, "positive": ["270", 0], "negative": ["263", 0]}},
}
inj_o, stats_o = wf_mod.build_prompt_injections(struct_one, "1girl", "lowres")
by_node_o = {(i["nodeId"], i["fieldName"]): i["fieldValue"] for i in inj_o}
# 契约变更：「上游只有一个字符串字段就认定唯一」= §3.5 明令禁止的拓扑假设，
# c5 fixture 的反例就是它：唯一字段其实是模板前缀，覆盖后模板被破坏。
check("契约变更：上游唯一字段也不再自动采用（§3.5 禁止）",
      stats_o["positive"] == 0 and stats_o["negative"] == 0 and inj_o == [], str(stats_o))
check("不再往上游 300.text 注入（避免覆盖模板前缀这类静默失效）",
      ("300", "text") not in by_node_o)

# 上游链更长（两级）同样不再回溯
struct_deep = {
    "270": {"class_type": "CLIPTextEncode", "inputs": {"text": ["300", 0]}},
    "300": {"class_type": "TextJoin", "inputs": {"value": ["310", 0]}},
    "310": {"class_type": "PrimitiveString", "inputs": {"value": "old"}},
    "152": {"class_type": "KSampler", "inputs": {"seed": 1, "positive": ["270", 0], "negative": ["263", 0]}},
}
inj_d, stats_d = wf_mod.build_prompt_injections(struct_deep, "deep pos", "")
check("契约变更：两级上游链也不再回溯", stats_d["positive"] == 0 and inj_d == [], str(stats_d))
check("不再往源头 310 注入", all(i["nodeId"] != "310" for i in inj_d))

# 占位符写在上游节点里 -> 主路径直接命中（用户最该用的写法）
struct_up = {
    "270": {"class_type": "CLIPTextEncode", "inputs": {"text": ["300", 0]}},
    "300": {"class_type": "StringConcat", "inputs": {"text_a": "masterpiece, {positive}", "text_b": "x"}},
    "152": {"class_type": "KSampler", "inputs": {"seed": 1, "positive": ["270", 0]}},
}
inj_u, stats_u = wf_mod.build_prompt_injections(struct_up, "1girl", "")
check("占位符写在上游时主路径命中", stats_u["positive"] == 1, str(stats_u))
check("上游占位符被替换且保留前缀",
      any(i["nodeId"] == "300" and i["fieldValue"] == "masterpiece, 1girl" for i in inj_u))

print("\n=== 29. 空提示词撞上云端占位符：必须报警而非静默 ===")
struct_ph = {
    "270": {"class_type": "CLIPTextEncode", "inputs": {"text": "{positive}"}},
    "152": {"class_type": "KSampler", "inputs": {"seed": 1, "positive": ["270", 0]}},
}
hits = wf_mod.placeholder_hits(struct_ph)
check("能统计出云端占位符数量", hits["positive"] == 1 and hits["negative"] == 0, str(hits))
check("空工作流结构时统计为 0", wf_mod.placeholder_hits({})["positive"] == 0)
check("_preview 压平空白并限长",
      _preview("  a\n  b  " + "x" * 100).startswith("a b") and _preview("x" * 100).endswith("…"))

# ---------------------------------------------------------------------------
print("\n=== 30. 音视频复合 latent（MiniMax H3）===")

from rhplugin.nodes import (
    RunningHubAVLatentMerge,
    RunningHubAVLatentSplit,
    _describe_samples,
)

video = torch.ones(1, 24, 3, 4, 4) * 0.5
audio = torch.ones(1, 32, 2, 10) * 0.25

tmpdir = tempfile.mkdtemp()
av_path = os.path.join(tmpdir, "MiniMaxH3_00001_.latent")
latent_mod.save_av_latent(av_path, video, audio, metadata={"prompt": "test"})
check("AV latent 文件已写出", os.path.exists(av_path))

with open(av_path, "rb") as fh:
    av_blob = fh.read()

parsed = latent_mod.parse(av_blob, av_path)
check("AV latent 被识别为复合 latent", latent_mod.is_nested(parsed["samples"]))
parts = list(parsed["samples"].unbind())
check("视频 latent 形状还原正确", tuple(parts[0].shape) == (1, 24, 3, 4, 4), str(tuple(parts[0].shape)))
check("音频 latent 形状还原正确", tuple(parts[1].shape) == (1, 32, 2, 10), str(tuple(parts[1].shape)))
check(
    "AV latent 不做 0.18215 补偿（H3 scale_factor 就是 1.0）",
    abs(float(parts[0].flatten()[0]) - 0.5) < 1e-6,
    str(float(parts[0].flatten()[0])),
)

# 普通 latent 不能被误判成 AV（回归保护）
plain_path = os.path.join(tmpdir, "plain.latent")
safetensors.torch.save_file(
    {"latent_tensor": torch.ones(1, 16, 4, 4), "latent_format_version_0": torch.tensor([])},
    plain_path,
)
with open(plain_path, "rb") as fh:
    plain_blob = fh.read()
parsed_plain = latent_mod.parse(plain_blob, plain_path)
check("普通 latent 仍走原路径（未被误判为复合）", not latent_mod.is_nested(parsed_plain["samples"]))
check("普通 latent 形状保持正确", tuple(parsed_plain["samples"].shape) == (1, 16, 4, 4))

check("识别 .h3latent 为 latent", _classify({"fileUrl": "https://x/a.h3latent"}) == "latent")
check("识别 .avlatent 为 latent", _classify({"fileUrl": "https://x/a.avlatent"}) == "latent")
check("带签名查询串的 .latent 仍识别", _classify({"fileUrl": "https://x/a.latent?token=1"}) == "latent")

split_node = RunningHubAVLatentSplit()
out_v, out_a = split_node.split(parsed)
check("拆分节点输出视频 latent", tuple(out_v["samples"].shape) == (1, 24, 3, 4, 4))
check("拆分节点输出音频 latent", tuple(out_a["samples"].shape) == (1, 32, 2, 10))
try:
    split_node.split({"samples": torch.zeros(1, 16, 4, 4)})
    check("非复合 latent 拆分必须报错（杜绝静默失效）", False)
except RHUserError:
    check("非复合 latent 拆分给出可读报错", True)

merge_node = RunningHubAVLatentMerge()
merged = merge_node.merge(out_v, out_a)[0]
check("合并节点还原复合 latent", latent_mod.is_nested(merged["samples"]))
check("合并时音频为空则原样返回", not latent_mod.is_nested(merge_node.merge(out_v)[0]["samples"]))

text = _describe_samples(parsed["samples"])
check("复合 latent 的形状回显同时含视频与音频", "视频" in text and "音频" in text, text)

# ---------------------------------------------------------------------------
print("\n=== 31. 新红线：填了提示词却没有落点 -> 报错而不是警告 ===")
from rhplugin.nodes import _require_prompt_targets, _submit_task

# ① 无占位符 + 映射里没引用 -> 必须抛 RHUserError（静默失效比报错更糟）
err = None
try:
    _require_prompt_targets(struct_link, {"positive": 0, "negative": 0}, "1girl", "lowres", "[]")
except RHUserError as exc:
    err = exc
check("无占位符且不填映射 -> 报错", err is not None, "没有抛异常")
if err is not None:
    text = str(err)
    check("报错文案给出可复制的映射模板",
          '{"nodeId": "<ID>", "fieldName": "text", "fieldValue": "{positive}"}' in text, text)
    check("报错文案提示用探查节点查 ID", "探查" in text, text)
    check("报错文案说明后果（提示词根本不会送上去）", "静默失效" in text, text)
    check("报错文案不携带任何密钥", "apiKey" not in text and "api_key" not in text)

# ② 映射里引用了 {positive} -> 视为已指定落点，不报错（判据必须看替换前的原文）
err2 = None
try:
    _require_prompt_targets(
        struct_link,
        {"positive": 0, "negative": 0},
        "1girl",
        "lowres",
        '[{"nodeId": "270", "fieldName": "text", "fieldValue": "{positive}"},'
        ' {"nodeId": "263", "fieldName": "text", "fieldValue": "{negative}"}]',
    )
except RHUserError as exc:
    err2 = exc
check("映射里指定了 {positive}/{negative} -> 不报错", err2 is None, str(err2))

# ③ 云端有占位符 -> 不报错
err3 = None
try:
    _require_prompt_targets(struct, {"positive": 1, "negative": 1}, "1girl", "bad", "[]")
except RHUserError as exc:
    err3 = exc
check("占位符命中时不报错", err3 is None, str(err3))

# ④ 结构拉不到时不能误报（无从判断，不能编造）
err4 = None
try:
    _require_prompt_targets({}, {"positive": 0, "negative": 0}, "1girl", "bad", "[]")
except RHUserError as exc:
    err4 = exc
check("结构缺失时不误报（沿用结构缺失提示）", err4 is None, str(err4))

# ⑤ 没填提示词就不该管
err5 = None
try:
    _require_prompt_targets(struct_link, {"positive": 0, "negative": 0}, "", "", "[]")
except RHUserError as exc:
    err5 = exc
check("没填提示词时不报错", err5 is None, str(err5))

print("\n=== 32. 803 逐条定位：10 条映射错 1 条要指名是哪一条 ===")


class _FakeClient803:
    """只模拟「提交即被 803 整包拒绝」的客户端。"""

    def create_task(self, workflow_id, node_info_list, instance_type=None):
        raise client_mod.RunningHubError(
            "RunningHub 调用失败 —— 参数注入不合法：请确认 nodeId / fieldName 与云端工作流完全一致",
            code=803,
        )


struct_803 = {
    "3": {"class_type": "KSampler", "inputs": {"seed": 1, "steps": 20, "positive": ["6", 0]}},
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "{positive}"}},
}
# 前 9 条合法（含字面量字段），第 10 条故意写错 fieldName
node_info_list = [
    {"nodeId": "3", "fieldName": "seed", "fieldValue": 1},
    {"nodeId": "3", "fieldName": "steps", "fieldValue": 20},
    {"nodeId": "6", "fieldName": "text", "fieldValue": "a"},
] * 3 + [{"nodeId": "6", "fieldName": "txt", "fieldValue": "typo"}]

err803 = None
try:
    _submit_task(_FakeClient803(), "wf", node_info_list, struct_803, False)
except RHUserError as exc:
    err803 = exc
check("803 被转成中文 RHUserError", err803 is not None)
if err803 is not None:
    msg = str(err803)
    check("先给结论：共几条、错了几条",
          "共提交 10 条映射" in msg and "1 条不合法" in msg, msg)
    check("点名第几条", "第 10 条" in msg, msg)
    check("点名 nodeId 与 fieldName", "nodeId=6" in msg and "fieldName=txt" in msg, msg)
    check("说明该节点可用字段（用户照着改即可）", "可用字段" in msg, msg)
    check("末尾给出修法指引", "修法" in msg and "探查" in msg, msg)
    check("803 文案不携带密钥", "apiKey" not in msg)
    check("只列有问题的那一条，不刷屏", msg.count("问题：") == 1, msg)

# 错的是连线字段时，要说明「被连线接管」而不是笼统说字段不存在
err_link = None
try:
    _submit_task(
        _FakeClient803(), "wf",
        [{"nodeId": "3", "fieldName": "positive", "fieldValue": "x"}],
        struct_803, False,
    )
except RHUserError as exc:
    err_link = exc
check("注入到连线字段时 803 文案说明「被连线接管」",
      err_link is not None and "连线" in str(err_link), str(err_link))

# 结构拿不到时不能编造逐条结论，必须如实说无法定位
err_nostruct = None
try:
    _submit_task(
        _FakeClient803(), "wf",
        [{"nodeId": "3", "fieldName": "seed", "fieldValue": 1}],
        {}, False,
    )
except RHUserError as exc:
    err_nostruct = exc
check("结构缺失时如实说无法逐条定位（不编造）",
      err_nostruct is not None and "无法逐条定位" in str(err_nostruct), str(err_nostruct))

# 非 803 的错误不能被误吞
class _FakeClient500:
    def create_task(self, workflow_id, node_info_list, instance_type=None):
        raise client_mod.RunningHubError("余额不足", code=402)


err_other = None
try:
    _submit_task(_FakeClient500(), "wf", [], struct_803, False)
except client_mod.RunningHubError as exc:
    err_other = exc
check("非 803 错误原样向上抛（不被误吞）",
      err_other is not None and getattr(err_other, "code", None) == 402, str(err_other))

print("\n=== 33. 探查节点：输出可直接复制的映射模板 ===")
templates = wf_mod.mapping_templates(struct_803)
check("字面量字段给出可复制片段",
      '{"nodeId": "6", "fieldName": "text", "fieldValue": "{positive}"}' in templates, templates)
check("片段是合法 JSON（复制即可用）",
      any(line.strip().startswith("{") and _json.loads(line.strip())["nodeId"] == "6"
          for line in templates.splitlines()))
check("连线字段不给模板且明确标注",
      "positive 是连线字段" in templates and "不能注入" in templates, templates)
check("连线字段标注了来源节点", "由 nodeId=6 提供" in templates, templates)
check("模板里区分了字面量与连线两类字段",
      '{"nodeId": "3", "fieldName": "steps", "fieldValue": 20}' in templates, templates)
check("空结构不炸且返回空串", wf_mod.mapping_templates({}) == "")

# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
if FAILED:
    print("失败项：" + ", ".join(FAILED))
    sys.exit(1)
print("全部通过")
