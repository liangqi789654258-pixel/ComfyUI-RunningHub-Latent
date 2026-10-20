"""作品栏缩略图的后台解码线程。

图片要读 3000x4000 的大 PNG（单张 30ms 量级，200 张就是 6 秒），视频还要起
ffmpeg 抽首帧（100ms 量级）。这些全部丢到 QThread 里做，主线程只负责把结果
塞成 QIcon——列表立刻出现，图标随后填满，界面全程不卡。

注意：QImage 可以跨线程创建和传递，QPixmap 不行（GUI 线程专属），
所以这里只返回 QImage / JPEG 字节，转成 QPixmap 的事留给主窗口。
"""

from __future__ import annotations

from typing import Iterable, List, Optional

from PySide6.QtCore import QObject, QSize, Signal, Slot
from PySide6.QtGui import QImage, QImageReader

from .. import thumbs

THUMB_BOX = 320  # 缩略图边长（列表 iconSize 是 160，给 2 倍供 hidpi）


def image_thumb(path: str, box: int = THUMB_BOX) -> Optional[QImage]:
    """按「刚好盖住 box×box」的尺寸解码图片（避免读全图再缩，省内存也快）。"""
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    size = reader.size()
    if size.isValid() and size.width() > 0 and size.height() > 0:
        factor = max(box / float(size.width()), box / float(size.height()))
        reader.setScaledSize(
            QSize(
                max(1, round(size.width() * factor)),
                max(1, round(size.height() * factor)),
            )
        )
    img = reader.read()
    return None if img.isNull() else img


class ThumbWorker(QObject):
    """逐个产出缩略图：视频 → JPEG 字节，图片 → QImage。"""

    ready = Signal(str, object)  # (文件路径, QImage 或 JPEG bytes)
    finished = Signal()

    def __init__(self, paths: Iterable[str], parent=None) -> None:
        super().__init__(parent)
        self._paths: List[str] = list(paths)
        self._stop = False

    def stop(self) -> None:
        """请求中止（正在跑的 ffmpeg 会被 kill，图片解码在当前这张后停）。"""
        self._stop = True

    @Slot()
    def run(self) -> None:
        try:
            for path in self._paths:
                if self._stop:
                    break
                if thumbs.is_video(path):
                    payload = thumbs.thumb_bytes(path, should_stop=lambda: self._stop)
                else:
                    try:
                        payload = image_thumb(path)
                    except Exception:
                        payload = None  # 单张坏图/无权限跳过，不影响其他条目
                if payload is not None:
                    self.ready.emit(path, payload)
        finally:
            self.finished.emit()
