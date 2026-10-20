"""
统一的错误分类与中文诊断文案。

## 为什么单独拆一个模块

插件里会调 RunningHub 的节点有 5 个，每个都要走完「读配置 → 拉云端结构 →
组参数映射 → 提交任务 → 轮询 → 下载 latent → 解析 latent」七八个环节。
以前每个环节各抛各的异常，落到 ComfyUI 前端只显示一句「节点在执行过程中
发生错误」，用户只能把完整日志翻出来才能定位。

现在所有失败统一走这里：**先分类，再按分类给出「原因 + 关键上下文 +
排查步骤」**，保证不查日志也能判断卡在哪一步、下一步该干什么。

## 分类的依据是「用户下一步要做什么」，不是异常的技术类型

    认证问题   -> 去换 Key
    工作流问题 -> 去查 ID / 去平台跑通一次
    映射问题   -> 去改参数映射 JSON
    网络问题   -> 查代理 / 调大超时 / 续取
    返回解析   -> 把原始返回发给插件作者
    云端失败   -> 去云端网页端看那个节点
    下载失败   -> 链接过期就重跑
    latent 解析 -> 检查云端末端接的是不是「保存Latent」
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------
# 分类常量
# --------------------------------------------------------------------------

CAT_AUTH = "auth"
CAT_WORKFLOW = "workflow"
CAT_MAPPING = "mapping"
CAT_INPUT = "input"
CAT_QUOTA = "quota"
CAT_NETWORK = "network"
CAT_RESPONSE = "response"
CAT_CLOUD = "cloud"
CAT_DOWNLOAD = "download"
CAT_PARSE = "parse"
CAT_LOCAL = "local"
CAT_UNKNOWN = "unknown"

CAT_TITLES: Dict[str, str] = {
    CAT_AUTH: "API Key 缺失或无效",
    CAT_WORKFLOW: "工作流 ID 不存在或不匹配",
    CAT_MAPPING: "节点参数映射错误",
    CAT_INPUT: "必填项缺失",
    CAT_QUOTA: "额度 / 并发受限",
    CAT_NETWORK: "网络异常或请求超时",
    CAT_RESPONSE: "返回体解析失败",
    CAT_CLOUD: "云端工作流执行失败",
    CAT_DOWNLOAD: "结果文件下载失败",
    CAT_PARSE: "latent 文件解析失败",
    CAT_LOCAL: "本地环境问题",
    CAT_UNKNOWN: "未分类错误",
}

# 每个分类的排查步骤。写成「动词开头、能直接照做」的短句，
# 不写「请检查配置是否正确」这种正确的废话。
ACTIONS: Dict[str, Tuple[str, ...]] = {
    CAT_AUTH: (
        "打开 RunningHub 控制台 → 账号设置 → API Key，确认这个 Key 没有被禁用或删除；",
        "重新生成一个 Key，并**同时**更新节点输入框和 config.ini 两处"
        "（节点输入框留空时才会读配置文件，填了就以输入框为准）；",
        "确认该账号开通了「工作流 API」调用权限——只有网页端权限的 Key 调不通 openapi 接口。",
    ),
    CAT_WORKFLOW: (
        "确认 workflow_id 是纯数字 ID（从 runninghub.cn/workflow/<ID> 的地址栏里取），"
        "不要把整段 URL 粘进来；",
        "该工作流必须先在平台**手动运行成功过一次**并保存，草稿状态 / 没跑通过的 API 调不动；",
        "确认这个工作流属于当前 API Key 的账号——别人分享的链接不能直接用你的 Key 调用；",
        "用「RH 工作流参数探查」节点验证：能拉出节点清单即 ID 正确，拉不出来就是 ID 或权限有问题。",
    ),
    CAT_MAPPING: (
        "用「RH 工作流参数探查」节点重新拉一份真实的 nodeId / fieldName 清单；",
        "新值必须写在 fieldValue 字段里（不是 value）——官方接口只认 fieldValue；",
        "连线接管的字段（探查结果里标 × 的）不能注入，请改注它上游的字面量字段；",
        "换过云端工作流就要重拉一次清单，旧映射会整条失效（这就是最常见的 803 来源）。",
    ),
    CAT_INPUT: (
        "把上面点名的必填项补上再运行；",
        "不确定格式就悬停看输入框的 tooltip，或看节点自带的 DESCRIPTION。",
    ),
    CAT_QUOTA: (
        "到 RunningHub 控制台确认余额 / 会员并发数（消费级通常同时只能跑 1 个任务）；",
        "等当前任务跑完再提交，或把节点的 timeout 调大让它排队等；",
        "余额不足就充值后重试。",
    ),
    CAT_NETWORK: (
        "用浏览器打开 www.runninghub.cn 确认能访问；",
        "如果你挂着代理 / VPN，确认 ComfyUI 进程也走代理"
        "（给启动环境设置 HTTP_PROXY / HTTPS_PROXY 环境变量）；",
        "只是任务慢的话：把 timeout 调大，或用「RH 按任务号取回」节点拿已有 taskId 续取；",
        "公司网络 / 防火墙拦截时，可尝试手机热点排除网络策略。",
    ),
    CAT_RESPONSE: (
        "重试一次——偶发的网关错误重试通常就好；",
        "确认 config.ini 里的 base_url 没有被改成非官方地址（默认 https://www.runninghub.cn）；",
        "把上面「服务端原始返回」整段发给插件作者，我会把它加进兼容列表。",
    ),
    CAT_CLOUD: (
        "到 RunningHub 网页端打开这个工作流手动跑一次，云端会高亮真正报错的节点；",
        "重点看下面「云端报错节点」的名字——本地改不了云端缺的模型 / 节点，得在云端补齐；",
        "如果是 H3 / 视频类工作流，确认云端末端接的是「保存Latent」类节点，"
        "并且它在本次执行路径上（没有被下游短路）。",
    ),
    CAT_DOWNLOAD: (
        "云端文件有有效期（通常 14 天），过期了只能重跑一次任务；",
        "先按上面 NETWORK 的办法确认网络与代理可用；",
        "如果 URL 看着正常却一直下不下来，把完整下载地址发给插件作者。",
    ),
    CAT_PARSE: (
        "确认云端工作流末端接的是「保存Latent」/「MiniMax H3 Save AV Latent」，"
        "不是「保存图像」或「保存视频」——后者下载到的是 png/mp4，解析必然失败；",
        "确认该保存节点在本次执行路径上真的被执行到了；",
        "H3 音视频 latent 必须是 video/audio 双键的单个文件；"
        "若你在云端把它拆成了两个保存节点，请在云端改回一个（本节点只有一个 latent 输出口）。",
    ),
    CAT_LOCAL: (
        "按上面提示补依赖 / 检查目录权限与磁盘剩余空间；",
        "改完依赖需要**重启 ComfyUI** 才会生效。",
    ),
    CAT_UNKNOWN: (
        "先看上面「失败环节」判断卡在哪一步，再对照对应分类的办法处理；",
        "仍无法解决时，把 ComfyUI 控制台里 [RunningHub-Latent] 开头的完整日志发给插件作者。",
    ),
}

_SEP = "─" * 46


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------


def mask_secret(text: Any) -> str:
    """把文本里可能残留的密钥片段打码，杜绝 API Key 泄漏到前端 / 日志。"""
    return re.sub(r"[A-Za-z0-9]{24,}", lambda m: m.group()[:4] + "****", str(text))


def _clean(value: Any) -> str:
    """把任意值压成单行字符串并**顺手脱敏**，空值返回空串。

    脱敏放在这里而不是各个调用点：上下文里随时可能混进服务端回显的
    URL / 请求体，里面带着 apiKey 或签名 token，漏一次就是泄漏。
    """
    if value is None:
        return ""
    text = mask_secret(str(value)).strip()
    if not text or text in ("[]", "{}", "()"):
        return ""
    return " ".join(text.split())


def _context_lines(context: Optional[Dict[str, Any]]) -> List[str]:
    """把上下文字典渲染成「键：值」行；空值自动跳过（不刷无意义的空行）。"""
    lines: List[str] = []
    for key, value in (context or {}).items():
        text = _clean(value)
        if not text:
            continue
        # 长文本（原始返回体）单独一行起，避免和短字段挤在一起
        if len(text) > 80:
            lines.append(f"{key}：")
            lines.append(f"    {text}")
        else:
            lines.append(f"{key}：{text}")
    return lines


def truncate(text: Any, limit: int = 400) -> str:
    """截断并脱敏，用于展示服务端原始返回。"""
    flat = _clean(mask_secret(text))
    if len(flat) <= limit:
        return flat
    return flat[:limit] + f"…（已截断，共 {len(flat)} 字符）"


# --------------------------------------------------------------------------
# 异常类型
# --------------------------------------------------------------------------


class RHUserError(RuntimeError):
    """面向用户的中文错误：自带分类、失败环节、关键上下文与排查步骤。

    ComfyUI 会把 str(exc) 原样显示给用户，所以文案必须能独立看懂——
    不依赖任何日志上下文。
    """

    def __init__(
        self,
        message: str,
        *,
        category: str = CAT_UNKNOWN,
        node: str = "",
        stage: str = "",
        parts: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.node = node
        self.stage = stage
        # 保留原始组成要素，供上层补上节点名 / 失败环节后重新渲染
        self.parts: Dict[str, Any] = parts or {}

    # -- 构造 ----------------------------------------------------------

    @classmethod
    def _render(
        cls, category: str, parts: Dict[str, Any], node: str, stage: str
    ) -> str:
        lines: List[str] = []
        lines.append(f"{node or '本节点'}执行失败")
        lines.append(_SEP)
        lines.append(f"问题类型：{CAT_TITLES.get(category, CAT_TITLES[CAT_UNKNOWN])}")
        if stage:
            lines.append(f"失败环节：{stage}")
        lines.append(_SEP)
        lines.append(f"原因：{parts.get('reason', '')}")
        lines.extend(_context_lines(parts.get("context")))
        lines.append(_SEP)

        steps = parts.get("actions") or ACTIONS.get(category, ACTIONS[CAT_UNKNOWN])
        if steps:
            lines.append("怎么排查：")
            for index, step in enumerate(steps, 1):
                lines.append(f"  {index}. {step}")
        return "\n".join(lines)

    @classmethod
    def build(
        cls,
        category: str,
        reason: str,
        *,
        node: str = "",
        stage: str = "",
        context: Optional[Dict[str, Any]] = None,
        actions: Optional[Sequence[str]] = None,
    ) -> "RHUserError":
        """按分类拼出完整诊断文案。这是本模块唯一的文案出口。"""
        parts: Dict[str, Any] = {
            "reason": mask_secret(reason),
            "context": dict(context or {}),
            "actions": list(actions) if actions is not None else None,
        }
        message = cls._render(category, parts, node, stage)
        return cls(message, category=category, node=node, stage=stage, parts=parts)

    def retitled(
        self,
        node: Optional[str] = None,
        stage: Optional[str] = None,
        extra_context: Optional[Dict[str, Any]] = None,
    ) -> "RHUserError":
        """补上（或覆盖）节点名 / 失败环节后重新渲染。

        下层模块（client / cache）抛错时不知道用户点的是哪个节点、卡在哪一环，
        由最靠近 UI 的 nodes 层补上，用户才看得出「是哪个节点、哪一步」。
        不是按本模块格式生成的异常（parts 为空）原样返回，绝不破坏已有文案。
        """
        if not self.parts:
            return self
        new_node = node or self.node
        new_stage = stage or self.stage
        if new_node == self.node and new_stage == self.stage and not extra_context:
            return self

        parts = dict(self.parts)
        context = dict(parts.get("context") or {})
        context.update(extra_context or {})
        parts["context"] = context
        parts["reason"] = parts.get("reason", "")

        clone = RHUserError(
            self._render(self.category, parts, new_node, new_stage),
            category=self.category,
            node=new_node,
            stage=new_stage,
            parts=parts,
        )
        return clone


# --------------------------------------------------------------------------
# 分类
# --------------------------------------------------------------------------

_KNOWN_EXC_CATEGORY: Dict[str, str] = {
    "Timeout": CAT_NETWORK,
    "ConnectTimeout": CAT_NETWORK,
    "ReadTimeout": CAT_NETWORK,
    "ConnectionError": CAT_NETWORK,
    "SSLError": CAT_NETWORK,
    "ProxyError": CAT_NETWORK,
    "TooManyRedirects": CAT_NETWORK,
    "ChunkedEncodingError": CAT_NETWORK,
    "JSONDecodeError": CAT_RESPONSE,
    "LatentParseError": CAT_PARSE,
}


def classify_exception(exc: BaseException) -> str:
    """按异常本身猜一个分类，用于兜底包装（拿不到服务端码时）。"""
    if isinstance(exc, RHUserError):
        return exc.category

    name = type(exc).__name__
    if name in _KNOWN_EXC_CATEGORY:
        return _KNOWN_EXC_CATEGORY[name]

    module = type(exc).__module__ or ""
    if module.startswith("requests") or module.startswith("urllib3"):
        return CAT_NETWORK
    if isinstance(exc, TimeoutError):
        return CAT_NETWORK
    if isinstance(exc, (KeyError, TypeError, ValueError, AttributeError)):
        # 我们自己的代码在数据结构上翻车，多半是平台返回形态变了
        return CAT_RESPONSE
    if isinstance(exc, (OSError, IOError)):
        return CAT_LOCAL
    return CAT_UNKNOWN


def http_category(status: int) -> str:
    """HTTP 状态码 -> 分类。"""
    if status in (401, 403):
        return CAT_AUTH
    if status == 402:
        return CAT_QUOTA
    if status == 404:
        return CAT_RESPONSE
    if status == 429:
        return CAT_QUOTA
    if 500 <= status < 600:
        return CAT_RESPONSE
    return CAT_NETWORK


def http_reason(status: int) -> str:
    """HTTP 状态码 -> 一句人话。"""
    if status in (401, 403):
        return f"服务端拒绝了请求（HTTP {status}）：API Key 无效、已过期，或没有该接口的权限"
    if status == 402:
        return f"HTTP 402：账号额度不足"
    if status == 404:
        return (
            f"HTTP 404：接口地址不存在。若你在 config.ini 改过 base_url 请改回 "
            f"https://www.runninghub.cn；也可能是平台接口改版"
        )
    if status == 429:
        return f"HTTP 429：请求过于频繁，被限流了"
    if 500 <= status < 600:
        return f"HTTP {status}：RunningHub 服务端故障（不是你的配置问题），稍后重试即可"
    return f"HTTP {status}：请求未成功"


def code_category(code: Any) -> str:
    """RunningHub 业务码 -> 分类。"""
    try:
        value = int(code)
    except (TypeError, ValueError):
        return CAT_UNKNOWN
    if value in (802, 812):
        return CAT_AUTH
    if value in (402, 421):
        return CAT_QUOTA
    if value == 810:
        return CAT_WORKFLOW
    if value == 803:
        return CAT_MAPPING
    if value == 805:
        return CAT_CLOUD
    if value in (400, 412):
        return CAT_RESPONSE
    return CAT_UNKNOWN


# 业务码 -> 中文解释。未收录的码会回落到服务端原始 msg。
# （文案的唯一出口在本模块；client 层只引用，不再自带一份。）
CODE_HINTS: Dict[int, str] = {
    803: (
        "参数注入不合法：请确认 nodeId / fieldName 与云端工作流完全一致，"
        "并且新值必须放在 fieldValue 字段（不是 value）。"
        "可用「RH 工作流参数探查」节点拉取真实清单。"
    ),
    805: "云端工作流执行失败（详见下方云端报错）。",
    813: "任务仍在队列排队中，云端算力繁忙，可稍后再试。",
    804: "云端任务正在执行。",
    810: "该工作流还没在平台跑通 / 未保存：请先在 RunningHub 网页端手动运行成功一次。",
    802: "API Key 已失效，请到 RunningHub 控制台重新生成。",
    812: "鉴权失败：API Key 不正确、已过期，或该 Key 无权调用工作流 API。",
    421: "并发任务数已达上限（消费级会员通常为 1），请等当前任务结束后重试。",
    402: "余额不足，请充值后重试。",
    412: "API 路径拼写错误（官方定义）：请确认 config.ini 的 base_url 未被改动。",
    400: "请求参数格式有误。",
}


def explain(
    exc: BaseException,
    *,
    node: str = "",
    stage: str = "",
    context: Optional[Dict[str, Any]] = None,
    actions: Optional[Sequence[str]] = None,
) -> RHUserError:
    """把任意异常包成带分类的中文错误；已经是 RHUserError 的原样返回。

    兜底路径必须保留原始异常信息，否则「细分报错」反而会把线索弄丢。
    """
    if isinstance(exc, RHUserError):
        return exc
    category = classify_exception(exc)
    reason = truncate(exc, 300) or f"{type(exc).__name__}（无详细信息）"
    merged: Dict[str, Any] = {"原始异常类型": type(exc).__name__}
    merged.update(context or {})
    return RHUserError.build(
        category, reason, node=node, stage=stage, context=merged, actions=actions
    )


# --------------------------------------------------------------------------
# 结构化辅助
# --------------------------------------------------------------------------


def dumps(value: Any, limit: int = 300) -> str:
    """把 dict / list 渲染成紧凑 JSON 用于回显，失败时退回 repr。"""
    try:
        return truncate(json.dumps(value, ensure_ascii=False, default=str), limit)
    except Exception:  # pragma: no cover
        return truncate(value, limit)


def first_nonempty(*values: Any) -> str:
    """按顺序取第一个非空字符串，用于「多个候选字段里挑一个」的场景。"""
    for value in values:
        text = _clean(value)
        if text:
            return text
    return ""


__all__ = [
    "RHUserError",
    "mask_secret",
    "truncate",
    "classify_exception",
    "http_category",
    "http_reason",
    "code_category",
    "CODE_HINTS",
    "explain",
    "dumps",
    "first_nonempty",
    "CAT_TITLES",
    "ACTIONS",
] + [name for name in dir() if name.startswith("CAT_")]
