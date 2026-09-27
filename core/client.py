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
from typing import Any, Callable, Dict, List, Optional

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
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        max_retries: int = 3,
        retry_interval: float = 2.0,
        session: Optional[requests.Session] = None,
    ) -> None:
        # 允许用户粘贴带空格/换行的 key，避免最常见的无效输入
        self.api_key = (api_key or "").strip()
        # 容忍用户在 base_url 末尾多写一个斜杠
        self.base_url = (base_url or DEFAULT_BASE_URL).strip().rstrip("/")
        self.timeout = timeout
        self.max_retries = max(max_retries, 1)
        self.retry_interval = retry_interval
        self._session = session or requests.Session()

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

    def fetch_outputs(self, task_id: str) -> Dict[str, Any]:
        """查询一次任务结果。

        返回统一结构：{"state": "running|queued|success", "outputs": [...]}
        任务失败时抛 TaskFailedError。
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

        code = body.get("code")
        data = body.get("data")

        if code == CODE_RUNNING:
            return {"state": "running", "outputs": []}
        if code == CODE_QUEUED:
            return {"state": "queued", "outputs": []}
        if code == CODE_FAILED or (isinstance(data, dict) and "failedReason" in data):
            reason = (data or {}).get("failedReason") or {}
            raise TaskFailedError(
                node_name=reason.get("node_name", ""),
                exception_message=self._mask(reason.get("exception_message", "")),
                traceback=self._mask(reason.get("traceback", "")),
            )
        if code == CODE_SUCCESS and isinstance(data, list):
            return {"state": "success", "outputs": data}

        hint = _ERROR_HINTS.get(code, self._mask(body.get("msg", "")))
        raise RunningHubError(f"查询任务 {task_id} 失败 —— {hint}")

    def wait_for_outputs(
        self,
        task_id: str,
        poll_interval: float = 5.0,
        timeout: float = 600.0,
        on_progress: Optional[Callable[[str, float], None]] = None,
    ) -> List[Dict[str, Any]]:
        """轮询直到成功，返回输出文件列表。"""
        started = time.time()
        last_state = ""

        while True:
            result = self.fetch_outputs(task_id)
            state = result["state"]

            if state == "success":
                return result["outputs"]

            if state != last_state and on_progress is not None:
                last_state = state
                label = "云端执行中" if state == "running" else "云端排队中"
                on_progress(label, time.time() - started)

            elapsed = time.time() - started
            if elapsed >= timeout:
                raise TaskTimeoutError(task_id, elapsed)

            time.sleep(min(poll_interval, max(0.5, timeout - elapsed)))

    def get_workflow_params(self, workflow_id: str) -> Dict[str, Any]:
        """拉取云端工作流的可修改参数清单（getJsonApiFormat）。

        返回原始 JSON dict。失败时返回 {} 而不抛错——这是辅助功能，不应阻断主流程。
        """
        url = f"{self.base_url}/api/openapi/getJsonApiFormat"
        for params in ({"workflowId": workflow_id}, {"workflowId": workflow_id, "apiKey": self.api_key}):
            for getter in ("params", "json"):
                try:
                    if getter == "params":
                        resp = self._session.get(url, params=params, timeout=self.timeout)
                    else:
                        resp = self._session.post(
                            url,
                            data=json.dumps(params).encode("utf-8"),
                            headers={"Content-Type": "application/json"},
                            timeout=self.timeout,
                        )
                    resp.raise_for_status()
                    body = resp.json()
                    if isinstance(body, dict):
                        inner = body.get("data", body)
                        # 部分版本把工作流 JSON 放在 data.prompt 的字符串里
                        if isinstance(inner, dict) and "prompt" in inner:
                            return inner["prompt"]
                        return inner
                except Exception:
                    continue
        return {}


__all__ = [
    "RunningHubClient",
    "RunningHubError",
    "TaskFailedError",
    "TaskTimeoutError",
    "DEFAULT_BASE_URL",
]
