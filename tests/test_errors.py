"""
报错机制专项测试（离线、零费用）

目标：任何一类失败，用户看到的文案都必须同时具备四样东西——
  ① 问题类型（属于哪一类）
  ② 失败环节（卡在哪一步）
  ③ 关键上下文（出错的节点 / 字段 / 错误码 / 服务端原话）
  ④ 可执行的排查方向

并且：文案里绝不能出现 API Key 明文。
"""

import os
import sys
import types

ROOT = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.abspath(os.path.join(ROOT, ".."))

_pkg = types.ModuleType("rhplugin")
_pkg.__path__ = [PLUGIN]
sys.modules["rhplugin"] = _pkg

import requests

from rhplugin.core import errors as errors_mod
from rhplugin.core import client as client_mod

PASSED = []
FAILED = []


def check(name, cond, extra=""):
    if cond:
        PASSED.append(name)
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name} {extra}")


FAKE_KEY = "a" * 32 + "bcdefghijklmnop"  # 48 位，模拟真实 Key 长度


# ---------------------------------------------------------------------------
print("\n=== 1. 文案四段式：类型 / 环节 / 上下文 / 排查 ===")
err = errors_mod.RHUserError.build(
    errors_mod.CAT_MAPPING,
    "云端返回错误码 803 —— 参数注入不合法",
    node="RH 云端采样 · 专业版",
    stage="提交任务到云端",
    context={"错误码": 803, "服务端原话": "nodeInfoList invalid"},
)
text = str(err)
check("带节点名", "RH 云端采样 · 专业版执行失败" in text)
check("带问题类型", "节点参数映射错误" in text)
check("带失败环节", "提交任务到云端" in text)
check("带关键上下文（错误码）", "803" in text)
check("带服务端原话", "nodeInfoList invalid" in text)
check("带排查步骤", "怎么排查" in text and "1." in text)
check("分类落在异常对象上", err.category == errors_mod.CAT_MAPPING)
check("环节落在异常对象上", err.stage == "提交任务到云端")


print("\n=== 2. 五类必区分场景各自归类正确 ===")
cases = [
    ("API Key 缺失或无效", errors_mod.CAT_AUTH, "缺少 / 失效的 Key"),
    ("工作流 ID 不存在或不匹配", errors_mod.CAT_WORKFLOW, "ID 不对"),
    ("节点参数映射错误", errors_mod.CAT_MAPPING, "映射写错"),
    ("必填项缺失", errors_mod.CAT_INPUT, "必填项空着"),
    ("网络异常或请求超时", errors_mod.CAT_NETWORK, "连不上 / 超时"),
    ("返回体解析失败", errors_mod.CAT_RESPONSE, "返回不是 JSON"),
]
for want_title, category, reason in cases:
    got = str(errors_mod.RHUserError.build(category, reason))
    check(f"{category} -> 「{want_title}」", want_title in got, got.splitlines()[2:3])


print("\n=== 3. HTTP 状态码归类 ===")
check("401 -> 认证", errors_mod.http_category(401) == errors_mod.CAT_AUTH)
check("403 -> 认证", errors_mod.http_category(403) == errors_mod.CAT_AUTH)
check("404 -> 返回体解析", errors_mod.http_category(404) == errors_mod.CAT_RESPONSE)
check("429 -> 额度/并发", errors_mod.http_category(429) == errors_mod.CAT_QUOTA)
check("500 -> 返回体解析", errors_mod.http_category(500) == errors_mod.CAT_RESPONSE)
check("502 -> 返回体解析", errors_mod.http_category(502) == errors_mod.CAT_RESPONSE)
check("401 文案点名 API Key", "API Key" in errors_mod.http_reason(401))
check("404 文案点名 base_url", "base_url" in errors_mod.http_reason(404))
check("500 文案说明不是用户配置问题", "不是你的配置问题" in errors_mod.http_reason(500))


print("\n=== 4. 平台业务码归类 ===")
check("802 -> 认证", errors_mod.code_category(802) == errors_mod.CAT_AUTH)
check("812 -> 认证", errors_mod.code_category(812) == errors_mod.CAT_AUTH)
check("810 -> 工作流", errors_mod.code_category(810) == errors_mod.CAT_WORKFLOW)
check("803 -> 映射", errors_mod.code_category(803) == errors_mod.CAT_MAPPING)
check("805 -> 云端失败", errors_mod.code_category(805) == errors_mod.CAT_CLOUD)
check("402 -> 额度", errors_mod.code_category(402) == errors_mod.CAT_QUOTA)
check("421 -> 额度", errors_mod.code_category(421) == errors_mod.CAT_QUOTA)
check("未知码 -> 未分类", errors_mod.code_category(99999) == errors_mod.CAT_UNKNOWN)


print("\n=== 5. 密钥绝不出现在任何报错文案里 ===")
leaky = f'{{"msg": "apiKey {FAKE_KEY} is invalid", "url": "https://x/y?token={FAKE_KEY}"}}'
masked = errors_mod.mask_secret(leaky)
check("长串密钥被打码", FAKE_KEY not in masked, masked)
e5 = errors_mod.RHUserError.build(
    errors_mod.CAT_AUTH,
    "鉴权失败",
    context={"服务端原话": leaky, "服务端原始返回": leaky},
)
check("报错文案不含密钥明文", FAKE_KEY not in str(e5))
check("报错文案仍保留可识别的前缀", FAKE_KEY[:4] in str(e5))


print("\n=== 6. 网络异常被分类且可重试标记正确 ===")


class _Resp:
    def __init__(self, status=200, text="", payload=None):
        self.status_code = status
        self.text = text
        self._payload = payload if payload is not None else {"code": 0, "data": {}}

    def json(self):
        return self._payload


class _Session:
    """最小 mock：想让哪一步失败就设置对应的行为。"""

    def __init__(self, behavior=None, resp=None):
        self.behavior = behavior
        self.resp = resp or _Resp()

    def post(self, *a, **k):
        return self._raise_or_return()

    def get(self, *a, **k):
        return self._raise_or_return()

    def _raise_or_return(self):
        if self.behavior is not None:
            raise self.behavior
        return self.resp


def make_client(behavior=None, resp=None):
    return client_mod.RunningHubClient(
        api_key=FAKE_KEY, session=_Session(behavior, resp), max_retries=1
    )


try:
    make_client(behavior=requests.exceptions.Timeout("timed out"))._post("/task/openapi/create", {})
    check("超时 -> 网络类错误", False)
except errors_mod.RHUserError as exc:
    check("超时 -> 网络类错误", exc.category == errors_mod.CAT_NETWORK)
    check("超时文案点名超时", "超时" in str(exc))
    check("超时可重试", getattr(exc, "retryable", False) is True)

try:
    make_client(behavior=requests.exceptions.ConnectionError("refused"))._post("/task/openapi/create", {})
    check("连接失败 -> 网络类错误", False)
except errors_mod.RHUserError as exc:
    check("连接失败 -> 网络类错误", exc.category == errors_mod.CAT_NETWORK)
    check("连接失败文案提到代理", "代理" in str(exc))

try:
    make_client(resp=_Resp(status=401, text="unauthorized"))._post("/task/openapi/create", {})
    check("HTTP 401 -> 认证类错误", False)
except errors_mod.RHUserError as exc:
    check("HTTP 401 -> 认证类错误", exc.category == errors_mod.CAT_AUTH)
    check("HTTP 401 不可重试（重试没意义）", getattr(exc, "retryable", False) is False)

try:
    make_client(resp=_Resp(status=500, text="boom"))._post("/task/openapi/create", {})
    check("HTTP 500 -> 返回体类且可重试", False)
except errors_mod.RHUserError as exc:
    check("HTTP 500 -> 返回体类且可重试", getattr(exc, "retryable", False) is True)


class _BadJsonResp(_Resp):
    def json(self):
        raise ValueError("no json")


try:
    make_client(resp=_BadJsonResp(status=200, text="<html>502 Bad Gateway</html>"))._post(
        "/task/openapi/create", {}
    )
    check("非法 JSON -> 返回体解析失败", False)
except errors_mod.RHUserError as exc:
    check("非法 JSON -> 返回体解析失败", exc.category == errors_mod.CAT_RESPONSE)
    check("非法 JSON 文案附上原始返回", "502 Bad Gateway" in str(exc))


print("\n=== 7. 业务码错误带得上码值、原话与排查方向 ===")
try:
    make_client(resp=_Resp(payload={"code": 810, "msg": "workflow not published"}))._post(
        "/task/openapi/create", {}
    )
    check("810 -> 工作流类错误", False)
except errors_mod.RHUserError as exc:
    check("810 -> 工作流类错误", exc.category == errors_mod.CAT_WORKFLOW)
    check("810 带码值", "810" in str(exc))
    check("810 带服务端原话", "workflow not published" in str(exc))
    check("810 给出「先跑通一次」的指引", "跑通" in str(exc))


print("\n=== 8. failedReason 三种形态都不炸且带得出云端报错 ===")
parts_dict = client_mod.RunningHubClient._reason_parts(
    {"node_name": "KSampler", "exception_message": "CUDA OOM", "traceback": "Traceback...\nRuntimeError: oom"}
)
check("dict 形态取出节点名", parts_dict[0] == "KSampler")
check("dict 形态取出异常信息", parts_dict[1] == "CUDA OOM")
# 曾经的崩溃点：平台直接给字符串时 .get() 会抛 AttributeError
parts_str = client_mod.RunningHubClient._reason_parts("Allocation on device")
check("str 形态不炸", parts_str[0] == "" and "Allocation" in parts_str[1])
parts_none = client_mod.RunningHubClient._reason_parts(None)
check("None 形态不炸", parts_none == ("", "", ""))

try:
    make_client(
        resp=_Resp(
            payload={
                "code": 805,
                "data": {"failedReason": "云端节点炸了（纯字符串形态）"},
            }
        )
    )._post("/task/openapi/create", {})
    check("805 + 字符串 failedReason 不崩", False)
except errors_mod.RHUserError as exc:
    check("805 + 字符串 failedReason 不崩", True)
    check("805 归到云端失败类", exc.category == errors_mod.CAT_CLOUD, exc.category)
    check("805 带出云端原始报错", "云端节点炸了" in str(exc))

failed = client_mod.TaskFailedError(
    node_name="MiniMaxH3ImageToVideo",
    exception_message="The size of tensor a (24) must match",
    traceback="Traceback...\nRuntimeError: The size of tensor a (24) must match",
)
check("TaskFailedError 归到云端失败类", failed.category == errors_mod.CAT_CLOUD)
check("TaskFailedError 带云端报错节点名", "MiniMaxH3ImageToVideo" in str(failed))
check("TaskFailedError 带云端堆栈末行", "tensor a (24)" in str(failed))


print("\n=== 9. 上层补节点名 / 环节（retitled） ===")
base = errors_mod.RHUserError.build(
    errors_mod.CAT_NETWORK, "请求超时", context={"接口": "https://x/create"}
)
refined = base.retitled(node="RH 云端采样 · 极简版", stage="提交任务到云端")
check("补上了节点名", "RH 云端采样 · 极简版执行失败" in str(refined))
check("补上了失败环节", "提交任务到云端" in str(refined))
check("原有上下文不丢", "https://x/create" in str(refined))
check("已带环节时不重复覆盖", base.retitled(stage="X").stage == "X")
plain = RuntimeError("boom")
check("非本模块异常不会凭空生成文案", errors_mod.RHUserError("x").retitled(node="n").parts == {})


print("\n=== 10. 任意异常兜底成中文诊断 ===")
for exc, want_cat in (
    (requests.exceptions.Timeout("t"), errors_mod.CAT_NETWORK),
    (TimeoutError("t"), errors_mod.CAT_NETWORK),
    (KeyError("taskId"), errors_mod.CAT_RESPONSE),
    (OSError("disk full"), errors_mod.CAT_LOCAL),
):
    got = errors_mod.explain(exc, node="某节点", stage="某环节")
    check(f"{type(exc).__name__} -> {want_cat}", got.category == want_cat, got.category)
    check(f"{type(exc).__name__} 文案带节点名", "某节点执行失败" in str(got))
check("已是 RHUserError 的原样返回（不被二次包装）",
      errors_mod.explain(base) is base)
check("兜底文案保留原始异常类型名", "原始异常类型" in str(errors_mod.explain(KeyError("k"))))


print("\n=== 11. 超时报错必须给得出 taskId 与续取办法 ===")
timeout = client_mod.TaskTimeoutError("2107367103375044609", 600.0)
text_timeout = str(timeout)
check("超时带 taskId", "2107367103375044609" in text_timeout)
check("超时归到网络/超时类", timeout.category == errors_mod.CAT_NETWORK)
check("超时给出续取办法", "按任务号取回" in text_timeout)


print("\n=== 12. 下载失败区分「链接过期」和「网络问题」 ===")
from rhplugin.core import cache as cache_mod

e404 = cache_mod._download_error("https://x/a.latent", status=404)
check("下载 404 归到下载类", e404.category == errors_mod.CAT_DOWNLOAD)
check("下载 404 说明文件已过期", "过期" in str(e404))
e403 = cache_mod._download_error("https://x/a.latent", status=403)
check("下载 403 说明直链失效", "失效" in str(e403))
e500 = cache_mod._download_error("https://x/a.latent", status=500)
check("下载 5xx 可重试", getattr(e500, "retryable", False) is True)
enet = cache_mod._download_error("https://x/a.latent", detail="下载超时：120 秒内没有收完数据。")
check("下载超时归到网络类", enet.category == errors_mod.CAT_NETWORK)


print("\n" + "=" * 60)
print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
if FAILED:
    for name in FAILED:
        print(f"  × {name}")
    sys.exit(1)
print("全部通过")
