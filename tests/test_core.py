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
check(
    "极简版只有这两个输入框（多一个都算违背零配置定位）",
    set(req.keys()) == {"api_key", "workflow_id"},
    f"实际字段：{sorted(req.keys())}",
)
check("极简版不含 node_overrides", "node_overrides" not in req)
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
print("\n=== 27. 连线跟随兜底：结构里看不到占位符时也能注入 ===")

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
check("兜底：正向经连线命中", stats_l["positive"] >= 1, str(stats_l))
check("兜底：负向经连线命中", stats_l["negative"] >= 1, str(stats_l))
by_node_l = {(i["nodeId"], i["fieldName"]): i["fieldValue"] for i in inj_l}
check("兜底：注入打到 270.text",
      by_node_l.get(("270", "text")) == "1girl", str(by_node_l))
check("兜底：注入打到 263.text",
      by_node_l.get(("263", "text")) == "lowres", str(by_node_l))
check("兜底：不会污染非提示词节点", ("266", "text") not in by_node_l)
check("兜底：同一字段不重复注入（两个采样器共享同一编码节点）",
      sum(1 for k in by_node_l if k == ("270", "text")) == 1)

# 连线指向的节点不在结构里（部分返回缺节点）-> 兜底放弃，命中数保持 0
struct_missing = {
    "3": {"class_type": "KSampler", "inputs": {"seed": 1, "positive": ["999", 0], "negative": ["998", 0]}},
}
inj_m, stats_m = wf_mod.build_prompt_injections(struct_missing, "a", "b")
check("兜底：连线源缺失时安全放弃", stats_m["positive"] == 0 and stats_m["negative"] == 0 and inj_m == [])

# 占位符已命中时，兜底不应画蛇添足
inj_p, stats_p = wf_mod.build_prompt_injections(struct, "1girl", "bad")
check("占位符主路径优先，不触发兜底",
      all(i["nodeId"] in ("6", "7") for i in inj_p) and len(inj_p) == 2)

# 正负连线指向同一个节点：不产生互相覆盖的双注入
struct_same = {
    "270": {"class_type": "CLIPTextEncode", "inputs": {"text": "same node"}},
    "3": {"class_type": "KSampler", "inputs": {"seed": 1, "positive": ["270", 0], "negative": ["270", 0]}},
}
inj_s, stats_s = wf_mod.build_prompt_injections(struct_same, "pos", "neg")
check("正负同节点时 positive 优先认领", stats_s["positive"] == 1 and stats_s["negative"] == 0, str(stats_s))
check("正负同节点时只注入一条", len(inj_s) == 1 and inj_s[0]["fieldValue"] == "pos")

# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
if FAILED:
    print("失败项：" + ", ".join(FAILED))
    sys.exit(1)
print("全部通过")
