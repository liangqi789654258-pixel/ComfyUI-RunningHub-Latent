"""生成线程：把同步的 GenerationRunner 包成 QThread，通过信号推 UI。"""

from __future__ import annotations

import traceback
from typing import Optional

from PySide6.QtCore import QThread, Signal

from ..engine import presets as presets_mod, runner as runner_mod
from ..engine.runner import GenerationParams

_runner_instance: Optional[runner_mod.GenerationRunner] = None


def shared_runner() -> runner_mod.GenerationRunner:
    global _runner_instance
    if _runner_instance is None:
        from .. import i18n

        _runner_instance = runner_mod.GenerationRunner(t=i18n.tr)
    return _runner_instance


class PresetCreateThread(QThread):
    """拉取云端结构生成预设（网络调用不阻塞 UI）。"""

    done = Signal(dict)
    failed = Signal(str)

    def __init__(self, workflow_id: str, parent=None) -> None:
        super().__init__(parent)
        self._workflow_id = workflow_id

    def run(self) -> None:
        try:
            preset = presets_mod.create_preset_from_workflow(self._workflow_id)
            self.done.emit(preset)
        except Exception as exc:
            self.failed.emit(str(exc))


class GenerateThread(QThread):
    progress = Signal(str)
    finished_ok = Signal(list, str)   # 文件列表, 摘要
    failed = Signal(str)

    def __init__(self, params: GenerationParams, parent=None,
                 resume_task_id: str = "") -> None:
        super().__init__(parent)
        self._params = params
        self._resume_task_id = resume_task_id
        self._cancel = False
        self.task_id = ""   # 生成结束后回填，供「按 taskId 续取」做默认值

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        runner = shared_runner()
        from .. import i18n

        try:
            if self._resume_task_id:
                result = runner.resume(
                    self._resume_task_id,
                    self._params,
                    progress=self.progress.emit,
                    cancel_check=lambda: self._cancel,
                    t=i18n.tr,
                )
            else:
                result = runner.run(
                    self._params,
                    progress=self.progress.emit,
                    cancel_check=lambda: self._cancel,
                    t=i18n.tr,
                )
            coins = (
                f"{result.coins_used:.0f}" if result.coins_used is not None else "?"
            )
            summary = i18n.tr(
                "summary_line",
                n=len(result.files), coins=coins,
                elapsed=f"{result.elapsed_s:.0f}", task_id=result.task_id,
            )
            self.task_id = result.task_id
            self.finished_ok.emit(result.files, summary)
        except Exception as exc:
            if self._cancel:
                self.failed.emit(i18n.tr("cancel_short"))
            else:
                traceback.print_exc()
                self.failed.emit(str(exc))
