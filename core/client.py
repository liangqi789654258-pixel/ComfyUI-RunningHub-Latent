"""
RunningHub OpenAPI 客户端（纯 HTTP 层，不依赖 ComfyUI，可单独测试）。

官方「工作流 API」接口一览（已实测确认，见 README 的参考文档）：

    POST /task/openapi/create       提交任务        ->  data.taskId
    POST /task/openapi/outputs      轮询状态 + 取结果 ->  data: 结果列表
    POST /task/openapi/status       单独查状态      ->  data: {status, msg, code}
    POST /task/openapi/upload       上传输入文件    ->  data.fileName
    GET  /api/openapi/getJsonApiFormat              取工作流可改参数清单

outputs 接口的返回码语义（这是本插件的轮询核心，来自官方集成示例）：

    code == 0    任务已完成，data 为输出文件列表
    code == 804  任务运行中
    code == 813  任务排队中
    code == 805  任务失败，data.failedReason = {node_name, exception_message, traceback}

成功时 data 的元素结构：
    {"fileUrl": str, "fileType": "png|mp4|latent|...", "taskCostTime": str, "nodeId": str}

参数注入统一走 nodeInfoList，字段必须是 nodeId / fieldName / **fieldValue**
（注意不是 value，这是官方接口最容易踩的坑）。
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from . import errors as errors_mod

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

DEFAULT_BASE_URL = "https://www.runninghub.cn"

# outputs 接口的业务状态码
CODE_SUCCESS = 0
CODE_RUNNING = 804
CODE_QUEUED = 813
CODE_FAILED = 805
# 参数注入不合法（APIKEY_INVALID_NODE_INFO）：nodeInfoList 整包被拒。
# 这个码必须单独认得出来——它是唯一能在提交前精确定位到「第几条映射错了」的信号。
CODE_PARAM_INVALID = 803


# 状态码 -> 中文解释已收敛到 errors.CODE_HINTS（文案唯一出口）。
# 未收录的码会回落到服务端原始 msg。

# 接口路径 -> 失败环节的人话描述。报错里必须写明卡在哪一次调用上，
# 否则用户看到「调用失败」根本不知道是提交阶段还是轮询阶段出的问题。
_PATH_STAGES: Dict[str, str] = {
    "/task/openapi/create": "提交任务到云端（POST /task/openapi/create）",
    "/task/openapi/outputs": "查询云端任务结果（POST /task/openapi/outputs）",
    "/task/openapi/status": "查询云端任务状态（POST /task/openapi/status）",
    "/task/openapi/upload": "上传输入文件到云端（POST /task/openapi/upload）",
    "/api/openapi/getJsonApiFormat": "读取云端工作流结构（GET getJsonApiFormat）",
    "/uc/openapi/accountStatus": "查询账户积分（POST /uc/openapi/accountStatus）",
}


def _emit_retry(path: str, attempt: int, total: int) -> None:
    """重试留痕。等待期间界面上没有任何反馈最容易让人以为卡死了。"""
    print(f"[RunningHub-Latent] 请求 {path} 第 {attempt} 次失败，准备重试（最多 {total} 次）")


# --------------------------------------------------------------------------
# 异常体系（所有对外消息均为中文且不携带密钥）
# --------------------------------------------------------------------------


class RunningHubError(errors_mod.RHUserError):
    """RunningHub 相关异常的基类。所有消息面向终端用户，必须是中文。

    继承 `errors.RHUserError` 是为了让上层（nodes）能统一补上「节点名 /
    失败环节」再重新渲染，用户看到的就是一份完整诊断而不是一句报错。

    `code` 是平台返回的业务码（拿不到时为 None）。带上它是为了让上层能
    针对特定错误码做精确定位——最典型的就是 803：平台只说「整包不合法」，
    而上层手里有工作流结构，可以逐条指出是哪一条映射错了。
    """

    def __init__(self, message: str, code: Optional[int] = None, **kwargs: Any) -> None:
        if "category" not in kwargs:
            kwargs["category"] = errors_mod.code_category(code)
        super().__init__(message, **kwargs)
        self.code = code


class TaskFailedError(RunningHubError):
    """云端工作流执行失败（code 805）。"""

    def __init__(
        self,
        node_name: str = "",
        exception_message: str = "",
        traceback: str = "",
    ) -> None:
        self.node_name = node_name
        self.exception_message = exception_message
        self.traceback = traceback
        detail = exception_message or "未知错误"
        location = f"节点「{node_name}」" if node_name else "云端工作流"

        context: Dict[str, Any] = {"云端报错原文": errors_mod.truncate(detail, 400)}
        if node_name:
            context["云端报错节点"] = node_name
        # 完整堆栈太长，只留最后一行（通常就是那句真正的异常）
        tail = [ln.strip() for ln in str(traceback or "").splitlines() if ln.strip()]
        if tail:
            context["云端堆栈末行"] = errors_mod.truncate(tail[-1], 300)

        tmp = errors_mod.RHUserError.build(
            errors_mod.CAT_CLOUD,
            f"{location}执行失败：{errors_mod.truncate(detail, 200)}",
            context=context,
        )
        super().__init__(str(tmp), category=tmp.category, parts=tmp.parts)


class TaskTimeoutError(RunningHubError):
    """轮询超时。任务可能仍在云端排队，允许用户用 taskId 后续取回。"""

    def __init__(self, task_id: str, waited: float) -> None:
        self.task_id = task_id
        self.waited = waited
        tmp = errors_mod.RHUserError.build(
            errors_mod.CAT_NETWORK,
            f"等待云端任务超时：已等 {waited:.0f} 秒仍未出结果。",
            context={
                "taskId": task_id,
                "已等待": f"{waited:.0f} 秒",
                "说明": "任务可能仍在云端排队（不算失败，钱也不一定白花）。",
                "补救办法": "把 timeout 调大后重跑，"
                "或用「RH 按任务号取回」节点填这个 taskId 续取结果。",
            },
        )
        super().__init__(str(tmp), category=tmp.category, parts=tmp.parts)


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------


class RunningHubClient:
    """RunningHub OpenAPI 的轻量封装。

    设计取舍：
      * 只用 requests，避免引入 aiohttp 等重依赖，ComfyUI 环境几乎必然已有 requests。
      * 网络层异常自动重试；业务失败（如 805）立即抛出，不做重试——重试只会重复扣费。
      * 任何异常消息都经过 _mask 脱敏，杜绝 API Key 泄漏到 ComfyUI 前端。
      * **响应解析必须宽容**：同一套接口在不同版本/不同节点类型下返回过
        list、{"results": [...]}、带 status 字段等多种形态，这里全部兼容。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        max_retries: int = 3,
        retry_interval: float = 2.0,
        session: Optional[requests.Session] = None,
        structure_ttl: float = 300.0,
    ) -> None:
        # 允许用户粘贴带空格/换行的 key，避免最常见的无效输入
        self.api_key = (api_key or "").strip()
        # 容忍用户在 base_url 末尾多写一个斜杠
        self.base_url = (base_url or DEFAULT_BASE_URL).strip().rstrip("/")
        self.timeout = timeout
        self.max_retries = max(max_retries, 1)
        self.retry_interval = retry_interval
        self._session = session or requests.Session()
        # 工作流结构的进程内缓存：避免同一条工作流每次运行都多打一次接口。
        # TTL 不宜过长，否则用户改完云端工作流后本地还在用旧结构。
        self._structure_ttl = max(0.0, structure_ttl)
        self._structure_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        # Discovery 层快照缓存（S2），与结构缓存同源但内容更全，互不影响
        # 最近一次原始响应体，供排错时原样展示给用户
        self._last_body: Any = None
        # 最近一次「读工作流结构」的失败原因（成功时置空）
        self._last_structure_error: Optional[errors_mod.RHUserError] = None

    # ---------------- 内部工具 ----------------

    @staticmethod
    def _mask(text: Any) -> str:
        """把文本中可能残留的密钥片段打码，防止泄漏到 UI / 日志。"""
        return errors_mod.mask_secret(text)

    @staticmethod
    def _reason_parts(reason: Any) -> Tuple[str, str, str]:
        """拆解 data.failedReason，兼容 dict 与纯字符串两种形态。

        平台并不保证 failedReason 一定是对象——实测见过直接给一段字符串的情况，
        早先这里对字符串调 .get() 会抛 AttributeError，把真正的云端报错
        整个吞掉。三种形态全部兜住，保证云端原始报错一定带得出来。
        """
        if isinstance(reason, dict):
            return (
                str(reason.get("node_name") or ""),
                str(reason.get("exception_message") or ""),
                str(reason.get("traceback") or ""),
            )
        if isinstance(reason, str):
            return ("", reason, "")
        return ("", "", "")

    # ---------------- 网络层 ----------------

    def _request(
        self,
        method: str,
        url: str,
        *,
        payload: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        stage: str = "",
    ) -> Dict[str, Any]:
        """发一次 HTTP 请求并返回解析后的 JSON。

        把三类失败彻底分开——以前它们共用一句「无法连接 RunningHub」，
        用户根本分不清是该换 Key、查代理还是找插件作者：

          * 网络层（超时 / 连接失败 / SSL）  -> 网络异常
          * HTTP 层（401 / 403 / 404 / 5xx） -> 按状态码归类
          * 解析层（返回体不是合法 JSON）    -> 返回体解析失败

        抛出的错误带 `retryable` 标记：只有网络问题和 5xx 值得重试，
        4xx（Key 无效、路径错）重试纯属浪费时间。
        """
        headers = {
            "Content-Type": "application/json",
            "Host": requests.utils.urlparse(self.base_url).netloc,
        }
        try:
            if method == "post":
                resp = self._session.post(
                    url,
                    data=json.dumps(payload or {}, ensure_ascii=False).encode("utf-8"),
                    headers=headers,
                    timeout=self.timeout,
                )
            else:
                resp = self._session.get(url, params=params, timeout=self.timeout)
        except requests.exceptions.Timeout:
            err = errors_mod.RHUserError.build(
                errors_mod.CAT_NETWORK,
                f"请求超时：{self.timeout:.0f} 秒内没有收到云端响应。",
                stage=stage,
                context={"接口": url, "当前超时设置": f"{self.timeout:.0f} 秒"},
            )
            err.retryable = True
            raise err from None
        except requests.exceptions.SSLError as exc:
            err = errors_mod.RHUserError.build(
                errors_mod.CAT_NETWORK,
                f"SSL 握手失败：{self._mask(exc)}。"
                "常见于公司网络 / 代理做了 HTTPS 中间人拦截，或本机时间不准。",
                stage=stage,
                context={"接口": url},
            )
            err.retryable = True
            raise err from None
        except requests.exceptions.ConnectionError as exc:
            err = errors_mod.RHUserError.build(
                errors_mod.CAT_NETWORK,
                f"连不上服务器：{self._mask(exc)}。"
                "通常是代理 / VPN / 防火墙挡住了，或域名没解析。",
                stage=stage,
                context={"接口": url, "base_url": self.base_url},
            )
            err.retryable = True
            raise err from None
        except requests.exceptions.RequestException as exc:
            err = errors_mod.RHUserError.build(
                errors_mod.CAT_NETWORK,
                f"请求异常：{self._mask(exc)}",
                stage=stage,
                context={"接口": url},
            )
            err.retryable = True
            raise err from None

        status = int(getattr(resp, "status_code", 0) or 0)
        if status >= 400:
            err = errors_mod.RHUserError.build(
                errors_mod.http_category(status),
                errors_mod.http_reason(status),
                stage=stage,
                context={
                    "接口": url,
                    "HTTP 状态码": status,
                    "服务端返回": errors_mod.truncate(getattr(resp, "text", ""), 200),
                },
            )
            # 5xx 是服务端抖动，值得重试；4xx 是请求本身有问题，重试无意义
            err.retryable = 500 <= status < 600
            raise err

        try:
            return resp.json()
        except ValueError:
            err = errors_mod.RHUserError.build(
                errors_mod.CAT_RESPONSE,
                "云端返回的内容不是合法 JSON（可能被网关 / 代理替换成了 HTML 错误页）。",
                stage=stage,
                context={
                    "接口": url,
                    "HTTP 状态码": status,
                    "服务端原始返回": errors_mod.truncate(getattr(resp, "text", ""), 300),
                },
            )
            err.retryable = False
            raise err from None

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """带重试的 JSON POST。返回服务端 data 字段（dict 或 list）。"""
        url = f"{self.base_url}{path}"
        stage = _PATH_STAGES.get(path, f"调用 {path}")
        body: Dict[str, Any] = {}

        for attempt in range(1, self.max_retries + 1):
            try:
                body = self._request("post", url, payload=payload, stage=stage)
                break
            except errors_mod.RHUserError as exc:
                if not getattr(exc, "retryable", False) or attempt >= self.max_retries:
                    raise
                _emit_retry(path, attempt, self.max_retries)
                time.sleep(self.retry_interval * attempt)

        if not isinstance(body, dict):
            raise errors_mod.RHUserError.build(
                errors_mod.CAT_RESPONSE,
                f"云端返回体不是对象，无法读取业务状态码（实际类型：{type(body).__name__}）。",
                stage=stage,
                context={"服务端原始返回": errors_mod.dumps(body)},
            )

        code = body.get("code")
        msg = self._mask(body.get("msg", ""))
        data = body.get("data")

        if code == 0:
            return data if data is not None else {}

        # 业务失败：805 需要特殊处理以带出云端节点级报错，其余直接抛中文错误
        if code == CODE_FAILED or (isinstance(data, dict) and "failedReason" in data):
            node_name, detail, tb = self._reason_parts((data or {}).get("failedReason"))
            raise TaskFailedError(
                node_name=node_name,
                exception_message=self._mask(detail),
                traceback=self._mask(tb),
            )

        raise self._business_error(code, msg, stage, body)

    def _business_error(
        self, code: Any, msg: str, stage: str, body: Dict[str, Any]
    ) -> errors_mod.RHUserError:
        """把平台业务码包装成带分类 + 排查步骤的中文错误。

        803 单独走 nodes 层的逐条诊断（那里握着工作流结构，能点名到具体某一条
        映射），所以这里不覆盖它，只保证其余错误码也带得上码值和原文。
        """
        category = errors_mod.code_category(code)
        hint = errors_mod.CODE_HINTS.get(code, f"服务端返回码 {code}：{msg}")
        err = errors_mod.RHUserError.build(
            category,
            f"云端返回错误码 {code} —— {hint}",
            stage=stage,
            context={
                "错误码": code,
                "服务端原话": msg,
                "服务端原始返回": errors_mod.dumps(body),
            },
        )
        err.retryable = False
        return err

    # ---------------- 公开接口 ----------------

    def create_task(
        self,
        workflow_id: str,
        node_info_list: Optional[List[Dict[str, Any]]] = None,
        instance_type: Optional[str] = None,
    ) -> str:
        """提交任务，返回 taskId。"""
        if not self.api_key:
            raise errors_mod.RHUserError.build(
                errors_mod.CAT_AUTH,
                "没有拿到 API Key（节点输入框和 config.ini 里都是空的）。",
                stage=_PATH_STAGES["/task/openapi/create"],
                context={
                    "配置文件位置": "插件目录下的 config.ini / config.json",
                    "说明": "节点输入框留空时才会读配置文件；两处都空就无法调用。",
                },
            )
        if not workflow_id or not str(workflow_id).strip():
            raise errors_mod.RHUserError.build(
                errors_mod.CAT_WORKFLOW,
                "没有拿到 workflow_id（节点输入框和配置文件里都是空的）。",
                stage=_PATH_STAGES["/task/openapi/create"],
            )

        payload: Dict[str, Any] = {
            "apiKey": self.api_key,
            "workflowId": str(workflow_id).strip(),
        }
        if node_info_list:
            payload["nodeInfoList"] = node_info_list
        if instance_type:
            payload["instanceType"] = instance_type

        data = self._post("/task/openapi/create", payload)
        task_id = data.get("taskId") if isinstance(data, dict) else None
        if not task_id:
            raise errors_mod.RHUserError.build(
                errors_mod.CAT_RESPONSE,
                "提交接口返回成功，但响应里没有 taskId（无法轮询结果）。",
                stage=_PATH_STAGES["/task/openapi/create"],
                context={
                    "工作流 ID": workflow_id,
                    "服务端原始返回": errors_mod.dumps(data),
                },
            )
        return str(task_id)

    def last_raw_body(self) -> Any:
        """最近一次接口返回的原始响应体，用于排错时原样展示。"""
        return self._last_body

    def get_account_status(self) -> Dict[str, Any]:
        """查询账户积分 / 余额（官方 /uc/openapi/accountStatus 接口）。

        返回 data 原始 dict，典型字段：remainCoins（剩余积分，可能是字符串）、
        currentTaskCounts、remainMoney、currency。查询失败会抛分类错误，
        调用方（nodes 层）应自行容错——积分显示只是锦上添花，不能影响任务本身。
        """
        # 注意：这个接口的参数名是小写 apikey（与工作流 API 的 apiKey 不同）
        return self._post("/uc/openapi/accountStatus", {"apikey": self.api_key})

    def fetch_outputs(self, task_id: str) -> Dict[str, Any]:
        """查询一次任务结果。

        返回统一结构：{"state": "running|queued|success", "outputs": [...]}
        任务失败时抛 TaskFailedError。

        注意：state == "success" 时 outputs **可能为空列表**——对象存储的
        文件落地比状态更新慢几拍是正常现象，是否算失败由 wait_for_outputs 决定。
        """
        payload = {"apiKey": self.api_key, "taskId": task_id}
        url = f"{self.base_url}/task/openapi/outputs"
        stage = _PATH_STAGES["/task/openapi/outputs"]

        body = self._request("post", url, payload=payload, stage=stage)
        self._last_body = body
        if not isinstance(body, dict):
            raise errors_mod.RHUserError.build(
                errors_mod.CAT_RESPONSE,
                f"查询结果返回体不是对象，无法判断任务状态（实际类型：{type(body).__name__}）。",
                stage=stage,
                context={"taskId": task_id, "服务端原始返回": errors_mod.dumps(body)},
            )

        code = body.get("code")
        data = body.get("data")
        status = self._status_of(data, body)

        if code == CODE_FAILED or (isinstance(data, dict) and "failedReason" in data):
            node_name, detail, tb = self._reason_parts((data or {}).get("failedReason"))
            raise TaskFailedError(
                node_name=node_name,
                exception_message=self._mask(detail),
                traceback=self._mask(tb),
            )
        if status in ("FAILED", "FAIL", "ERROR"):
            node_name, detail, tb = self._reason_parts(
                (data or {}).get("failedReason")
                or (data or {}).get("msg")
                or (data or {}).get("error")
                or "云端任务失败"
            )
            raise TaskFailedError(
                node_name=node_name,
                exception_message=self._mask(detail or "云端任务失败"),
                traceback=self._mask(tb),
            )
        if code == CODE_RUNNING or status in ("RUNNING", "EXECUTING", "PROCESSING"):
            return {"state": "running", "outputs": []}
        if code == CODE_QUEUED or status in ("QUEUED", "PENDING", "WAITING"):
            return {"state": "queued", "outputs": []}
        if code == CODE_SUCCESS or status in ("SUCCESS", "SUCCEEDED", "COMPLETED", "FINISHED"):
            return {"state": "success", "outputs": self._extract_outputs(data)}

        # code 既不是 0 也不是已知状态码：只有当真的拿到了文件列表时才敢算成功。
        # 这条兜底是为了兼容部分版本「code 缺位、只给 data」的响应。
        if isinstance(data, list):
            return {"state": "success", "outputs": data}

        hint = errors_mod.CODE_HINTS.get(code, self._mask(body.get("msg", "")))
        err = errors_mod.RHUserError.build(
            errors_mod.code_category(code),
            f"查询任务结果失败，云端返回错误码 {code} —— {hint}",
            stage=stage,
            context={
                "taskId": task_id,
                "错误码": code,
                "服务端原始返回": errors_mod.dumps(body),
            },
        )
        err.retryable = False
        raise err

    @staticmethod
    def _status_of(data: Any, body: Dict[str, Any]) -> str:
        """从 data 或 body 里取状态字符串，统一成大写。"""
        for source in (data, body):
            if isinstance(source, dict):
                for key in ("status", "taskStatus", "state"):
                    value = source.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip().upper()
        return ""

    @staticmethod
    def _extract_outputs(data: Any) -> List[Dict[str, Any]]:
        """从多种响应形态里抽出输出文件列表。

        实测/用户反馈中见过的形态：
          * data 直接是 list
          * data = {"results": [...]}      <- 文档里提到的 results
          * data = {"outputs": [...]}
          * data = {"fileList": [...]}
          * data 本身就是单个文件 dict
        """
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            for key in ("results", "outputs", "files", "fileList", "list", "data"):
                value = data.get(key)
                if isinstance(value, list):
                    return [x for x in value if isinstance(x, dict)]
            if "fileUrl" in data or "url" in data:
                return [data]
        return []

    def wait_for_outputs(
        self,
        task_id: str,
        poll_interval: float = 5.0,
        timeout: float = 600.0,
        on_progress: Optional[Callable[[str, float], None]] = None,
        empty_retries: int = 1,
        empty_retry_delay: float = 2.0,
    ) -> List[Dict[str, Any]]:
        """轮询直到成功，返回输出文件列表。

        empty_retries：状态已成功但文件列表为空时的补查次数。
        对象存储的文件落地普遍比状态更新慢 1~3 秒，这是真实存在的竞态，
        直接抛错会让用户白白损失一次已付费的任务。
        """
        started = time.time()
        last_state = ""
        empty_seen = 0
        # 轮询期间的瞬时网络抖动不该直接判死：任务已经付费了，
        # 一次读超时就放弃等于白花钱。可重试的错误先本地重试几次。
        network_glitches = 0

        while True:
            try:
                result = self.fetch_outputs(task_id)
            except errors_mod.RHUserError as exc:
                if not getattr(exc, "retryable", False) or network_glitches >= 3:
                    # 任务可能还在云端跑着，报错里必须带上 taskId 让用户可以续取
                    raise exc.retitled(
                        extra_context={
                            "taskId": task_id,
                            "已等待": f"{time.time() - started:.0f} 秒",
                            "补救办法": "任务可能仍在云端运行，"
                            "用「RH 按任务号取回」节点填这个 taskId 可以续取结果，不必重跑。",
                        }
                    ) from exc
                network_glitches += 1
                time.sleep(min(poll_interval, 5.0))
                continue

            state = result["state"]
            outputs = result["outputs"]

            if state == "success":
                if outputs:
                    return outputs
                if empty_seen < empty_retries:
                    empty_seen += 1
                    if on_progress is not None:
                        on_progress("等待文件同步", time.time() - started)
                    time.sleep(empty_retry_delay)
                    continue
                # 补查后依然为空，才认为是真的没有输出
                return []

            if state != last_state and on_progress is not None:
                last_state = state
                label = "云端生成中" if state == "running" else "云端排队中"
                on_progress(label, time.time() - started)

            elapsed = time.time() - started
            if elapsed >= timeout:
                raise TaskTimeoutError(task_id, elapsed)

            time.sleep(min(poll_interval, max(0.5, timeout - elapsed)))

    def get_workflow_structure(
        self, workflow_id: str, force: bool = False
    ) -> Dict[str, Dict[str, Any]]:
        """拉取云端工作流结构，归一化成 {nodeId: {class_type, inputs}}。

        拿到结构才能定位提示词文本节点与采样器 seed 字段。拿不到时返回空 dict，
        由调用方决定如何降级。

        `force=True` 跳过进程内缓存重新拉取。用于「本地缓存可能是旧的、而结论
        又很关键」的场合——目前只有 803 逐条诊断用（平台已拒绝、本次不计费，
        多拉一次结构换一个可信结论很划算）。
        """
        key = str(workflow_id).strip()
        if not key:
            return {}

        if not force:
            hit = self._structure_cache.get(key)
            if hit is not None:
                stamp, value = hit
                if self._structure_ttl <= 0 or (time.time() - stamp) < self._structure_ttl:
                    return value

        raw = self._fetch_workflow_raw(key)
        from . import workflow as workflow_mod

        structure = workflow_mod.normalize(raw)
        if structure:
            self._structure_cache[key] = (time.time(), structure)
        return structure

    def _fetch_workflow_raw(self, workflow_id: str) -> Any:
        """实际发起请求。该接口在不同版本下 GET/POST 都能通，逐个试。

        失败时不再静默返回 None：把最后一次（最有诊断价值的）失败原因记在
        `self._last_structure_error` 上，上层就能说清「是 Key 无效还是 ID 不对」
        而不是笼统一句「没读到结构」。
        """
        url = f"{self.base_url}/api/openapi/getJsonApiFormat"
        stage = _PATH_STAGES["/api/openapi/getJsonApiFormat"]
        attempts = (
            ("post", {"apiKey": self.api_key, "workflowId": workflow_id}),
            ("get", {"workflowId": workflow_id, "apiKey": self.api_key}),
            ("get", {"workflowId": workflow_id}),
        )
        last_error: Optional[BaseException] = None

        for method, params in attempts:
            try:
                if method == "post":
                    body = self._request("post", url, payload=params, stage=stage)
                else:
                    body = self._request("get", url, params=params, stage=stage)
            except errors_mod.RHUserError as exc:
                # 认证 / 工作流类原因比纯网络抖动更有诊断价值，优先保留
                if last_error is None or getattr(
                    last_error, "category", ""
                ) == errors_mod.CAT_NETWORK:
                    last_error = exc
                continue

            if not isinstance(body, dict):
                continue

            code = body.get("code")
            if code is not None and str(code) not in ("0", ""):
                # 接口通了但业务码非零（Key 无效 / 工作流不存在）——这是最值钱的线索
                last_error = self._business_error(
                    code, self._mask(body.get("msg", "")), stage, body
                )
                continue

            self._last_body = body
            self._last_structure_error = None
            data = body.get("data", body)
            if isinstance(data, dict):
                # 部分版本把工作流塞在 data.prompt 的字符串里
                for inner_key in ("prompt", "workflow", "json", "api"):
                    inner = data.get(inner_key)
                    if isinstance(inner, (dict, str)):
                        return inner
            return data

        self._last_structure_error = (
            errors_mod.explain(last_error) if last_error is not None else None
        )
        return None

    def last_structure_error(self) -> Optional[errors_mod.RHUserError]:
        """最近一次「读云端工作流结构」的失败原因（成功时为 None）。

        用于把「没读到结构」这类曾经语焉不详的降级警告，改成带错误码和
        排查方向的精确提示。
        """
        return self._last_structure_error


__all__ = [
    "RunningHubClient",
    "RunningHubError",
    "TaskFailedError",
    "TaskTimeoutError",
    "DEFAULT_BASE_URL",
    "CODE_PARAM_INVALID",
]
