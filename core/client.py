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

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

DEFAULT_BASE_URL = "https://www.runninghub.cn"

# outputs 接口的业务状态码
CODE_SUCCESS = 0
CODE_RUNNING = 804
CODE_QUEUED = 813
CODE_FAILED = 805

_TERMINAL_CODES = (CODE_SUCCESS, CODE_FAILED)

# 状态码 -> 中文解释。未收录的码会回落到服务端原始 msg。
_ERROR_HINTS: Dict[int, str] = {
    803: (
        "参数注入不合法：请确认 nodeId / fieldName 与云端工作流完全一致，"
        "并且新值必须放在 fieldValue 字段（不是 value）。"
        "可用「RH 工作流参数探查」节点拉取真实清单。"
    ),
    805: "云端工作流执行失败（详见下方云端报错）。",
    813: "任务仍在队列排队中，云端算力繁忙，可稍后再试。",
    804: "云端任务正在执行。",
    412: "API Key 无效或已失效，请到 RunningHub 控制台重新生成。",
    812: "鉴权失败：API Key 不正确、已过期，或该 Key 无权调用工作流 API。",
    421: "并发任务数已达上限（消费级会员通常为 1），请等当前任务结束后重试。",
    402: "余额不足，请充值后重试。",
    400: "请求参数格式有误。",
}


# --------------------------------------------------------------------------
# 异常体系（所有对外消息均为中文且不携带密钥）
# --------------------------------------------------------------------------


class RunningHubError(RuntimeError):
    """RunningHub 相关异常的基类。所有消息面向终端用户，必须是中文。"""


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
        super().__init__(f"{location}执行失败：{detail}")


class TaskTimeoutError(RunningHubError):
    """轮询超时。任务可能仍在云端排队，允许用户用 taskId 后续取回。"""

    def __init__(self, task_id: str, waited: float) -> None:
        self.task_id = task_id
        self.waited = waited
        super().__init__(
            f"等待云端任务超时（已等待 {waited:.0f} 秒）。"
            f"任务可能仍在排队，请记下 taskId 以便稍后用本插件的"
            f"「按任务号取回」节点取回结果：{task_id}"
        )


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
        self._snapshot_cache: Dict[str, Tuple[float, Any]] = {}
        # 最近一次原始响应体，供排错时原样展示给用户
        self._last_body: Any = None

    # ---------------- 内部工具 ----------------

    @staticmethod
    def _mask(text: str) -> str:
        """把文本中可能残留的密钥片段打码，防止泄漏到 UI / 日志。"""
        import re

        return re.sub(r"[A-Za-z0-9]{24,}", lambda m: m.group()[:4] + "****", str(text))

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """带重试的 JSON POST。返回服务端 data 字段（dict 或 list）。"""
        url = f"{self.base_url}{path}"
        last_exc: Optional[Exception] = None

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self._session.post(
                    url,
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Host": requests.utils.urlparse(self.base_url).netloc,
                    },
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                body = resp.json()
            except Exception as exc:  # 网络层/解析层异常 -> 重试
                last_exc = exc
                if attempt < self.max_retries:
                    time.sleep(self.retry_interval * attempt)
                    continue
                raise RunningHubError(
                    f"无法连接 RunningHub（{self.base_url}）：{self._mask(exc)}。"
                    f"请检查网络代理设置，或到 runninghub.cn 确认服务可用。"
                ) from exc

            code = body.get("code")
            msg = self._mask(body.get("msg", ""))
            data = body.get("data")

            if code == 0:
                return data if data is not None else {}

            # 业务失败：805 需要特殊处理以带出云端节点级报错，其余直接抛中文错误
            if code == CODE_FAILED or (isinstance(data, dict) and "failedReason" in data):
                reason = (data or {}).get("failedReason") or {}
                raise TaskFailedError(
                    node_name=reason.get("node_name", ""),
                    exception_message=self._mask(reason.get("exception_message", "")),
                    traceback=self._mask(reason.get("traceback", "")),
                )

            hint = _ERROR_HINTS.get(code, f"服务端返回码 {code}：{msg}")
            raise RunningHubError(f"RunningHub 调用失败 —— {hint}")

        raise RunningHubError(f"重试 {self.max_retries} 次后仍失败：{self._mask(last_exc)}")

    # ---------------- 公开接口 ----------------

    def create_task(
        self,
        workflow_id: str,
        node_info_list: Optional[List[Dict[str, Any]]] = None,
        instance_type: Optional[str] = None,
    ) -> str:
        """提交任务，返回 taskId。"""
        if not self.api_key:
            raise RunningHubError(
                "缺少 RunningHub API Key。请在节点参数里填写，"
                "或在本插件目录新建 config.json 写入 api_key。"
            )
        if not workflow_id or not str(workflow_id).strip():
            raise RunningHubError("缺少 workflow_id（RunningHub 工作流 ID）。")

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
            raise RunningHubError(
                f"提交任务未返回 taskId，服务端返回：{self._mask(data)}"
            )
        return str(task_id)

    def last_raw_body(self) -> Any:
        """最近一次接口返回的原始响应体，用于排错时原样展示。"""
        return self._last_body

    def fetch_outputs(self, task_id: str) -> Dict[str, Any]:
        """查询一次任务结果。

        返回统一结构：{"state": "running|queued|success", "outputs": [...]}
        任务失败时抛 TaskFailedError。

        注意：state == "success" 时 outputs **可能为空列表**——对象存储的
        文件落地比状态更新慢几拍是正常现象，是否算失败由 wait_for_outputs 决定。
        """
        payload = {"apiKey": self.api_key, "taskId": task_id}
        url = f"{self.base_url}/task/openapi/outputs"

        resp = self._session.post(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Host": requests.utils.urlparse(self.base_url).netloc,
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        self._last_body = body

        code = body.get("code")
        data = body.get("data")
        status = self._status_of(data, body)

        if code == CODE_FAILED or (isinstance(data, dict) and "failedReason" in data):
            reason = (data or {}).get("failedReason") or {}
            raise TaskFailedError(
                node_name=reason.get("node_name", ""),
                exception_message=self._mask(reason.get("exception_message", "")),
                traceback=self._mask(reason.get("traceback", "")),
            )
        if status in ("FAILED", "FAIL", "ERROR"):
            raise TaskFailedError(
                exception_message=self._mask(
                    (data or {}).get("failedReason")
                    or (data or {}).get("msg")
                    or (data or {}).get("error")
                    or "云端任务失败"
                )
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

        hint = _ERROR_HINTS.get(code, self._mask(body.get("msg", "")))
        raise RunningHubError(f"查询任务 {task_id} 失败 —— {hint}")

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

        while True:
            result = self.fetch_outputs(task_id)
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

    def get_workflow_structure(self, workflow_id: str) -> Dict[str, Dict[str, Any]]:
        """拉取云端工作流结构，归一化成 {nodeId: {class_type, inputs}}。

        这是极简版「零配置」依赖的能力：拿到结构才能自动定位提示词文本节点
        与采样器 seed 字段。拿不到时返回空 dict，由调用方决定如何降级。
        """
        key = str(workflow_id).strip()
        if not key:
            return {}

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

    def get_workflow_snapshot(self, workflow_id: str) -> Optional[Any]:
        """拉取云端工作流结构并返回 Discovery 层快照（S2）。

        比 `get_workflow_structure` 多三样东西：每字段的 `value_kind`
        （字面量 / 连线）、节点执行态、以及连线边列表与指纹，供绑定漂移检测使用。
        拿不到结构时返回空快照（含 warning），不抛异常——由调用方决定是否报错。
        """
        from . import snapshot as snapshot_mod

        key = str(workflow_id).strip()
        if not key:
            return None

        hit = self._snapshot_cache.get(key)
        if hit is not None:
            stamp, value = hit
            if self._structure_ttl <= 0 or (time.time() - stamp) < self._structure_ttl:
                return value

        snap = snapshot_mod.build_snapshot(self._fetch_workflow_raw(key), workflow_id=key)
        if snap.nodes:
            self._snapshot_cache[key] = (time.time(), snap)
        return snap

    def _fetch_workflow_raw(self, workflow_id: str) -> Any:
        """实际发起请求。该接口在不同版本下 GET/POST 都能通，逐个试。"""
        url = f"{self.base_url}/api/openapi/getJsonApiFormat"
        attempts = (
            ("post", {"apiKey": self.api_key, "workflowId": workflow_id}),
            ("get", {"workflowId": workflow_id, "apiKey": self.api_key}),
            ("get", {"workflowId": workflow_id}),
        )
        for method, params in attempts:
            try:
                if method == "post":
                    resp = self._session.post(
                        url,
                        data=json.dumps(params, ensure_ascii=False).encode("utf-8"),
                        headers={
                            "Content-Type": "application/json",
                            "Host": requests.utils.urlparse(self.base_url).netloc,
                        },
                        timeout=self.timeout,
                    )
                else:
                    resp = self._session.get(url, params=params, timeout=self.timeout)
                resp.raise_for_status()
                body = resp.json()
            except Exception:
                continue

            if not isinstance(body, dict):
                continue
            self._last_body = body
            data = body.get("data", body)
            if isinstance(data, dict):
                # 部分版本把工作流塞在 data.prompt 的字符串里
                for inner_key in ("prompt", "workflow", "json", "api"):
                    inner = data.get(inner_key)
                    if isinstance(inner, (dict, str)):
                        return inner
            return data
        return None

    def get_workflow_params(self, workflow_id: str) -> Dict[str, Any]:
        """旧接口名，保留兼容。返回归一化后的结构。"""
        return self.get_workflow_structure(workflow_id)  # type: ignore[return-value]


__all__ = [
    "RunningHubClient",
    "RunningHubError",
    "TaskFailedError",
    "TaskTimeoutError",
    "DEFAULT_BASE_URL",
]
