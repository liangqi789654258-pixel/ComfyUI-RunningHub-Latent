"""作品栏视频首帧缩略图测试。

覆盖：首帧抽取、磁盘缓存命中、坏文件兜底、画廊回填图标。
生成测试视频用本项目的 video.save_video（不依赖用户的真实产物）。
"""

import os
import sys
import tempfile
import time

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app import appconfig, thumbs  # noqa: E402
from app.engine import video as video_mod  # noqa: E402


def make_clip(path: str, n: int = 10, h: int = 240, w: int = 426) -> str:
    """造一段「首帧纯红、后续变绿」的视频，便于验证取的是第一帧。"""
    frames = []
    for i in range(n):
        frame = np.zeros((h, w, 3), dtype=np.float32)
        t = i / max(1, n - 1)
        frame[..., 0] = 1.0 - t   # 红 → 0
        frame[..., 1] = t         # 绿 → 1
        frames.append(frame)
    return video_mod.save_video(frames, path, fps=8, crf=18)


tmp = tempfile.mkdtemp(prefix="rh_thumb_")
clip = make_clip(os.path.join(tmp, "clip_first_frame.mp4"))
print(f"[OK] 测试视频生成：{os.path.getsize(clip)} 字节")

# 1) 首帧抽取
t0 = time.perf_counter()
data = thumbs.thumb_bytes(clip)
cost = (time.perf_counter() - t0) * 1000
assert data, "首帧抽取返回空"
assert data[:2] == b"\xff\xd8", "不是 JPEG 数据"
print(f"[OK] 首帧抽取成功：{len(data)} 字节，耗时 {cost:.0f} ms")

# 2) 缓存落盘 + 二次命中（应显著更快）
cp = thumbs.cache_path(clip)
assert os.path.isfile(cp), "缓存文件未落盘"
t0 = time.perf_counter()
data2 = thumbs.thumb_bytes(clip)
cost2 = (time.perf_counter() - t0) * 1000
assert data2 == data, "缓存内容与首次不一致"
assert cost2 < 50, f"缓存命中太慢：{cost2:.0f} ms"
print(f"[OK] 缓存命中：{cost2:.1f} ms，缓存文件 {os.path.basename(cp)}")

# 3) 文件内容变了 → key 变化（不会拿到旧图）
os.utime(clip, (time.time() + 5, time.time() + 5))
assert thumbs.cache_path(clip) != cp, "mtime 变了但缓存 key 没变"
print("[OK] 文件变更后缓存 key 自动失效")

# 4) 坏文件兜底
broken = os.path.join(tmp, "broken.mp4")
with open(broken, "wb") as fh:
    fh.write(b"not a video")
assert thumbs.thumb_bytes(broken) is None, "坏文件应返回 None"
print("[OK] 坏文件兜底返回 None（界面保留占位图标）")

# 5) 画廊回填：把扫描目录指到临时目录（内存内打补丁，不动用户配置）
appconfig.output_dir = lambda: tmp   # type: ignore[assignment]
app = QApplication.instance() or QApplication(sys.argv)
from app.gui.main_window import MainWindow  # noqa: E402

win = MainWindow()
# 临时目录里还有一个 broken.mp4（坏文件也要能列出条目，只是保留占位图）
assert win.list_gallery.count() == 2, f"画廊条数异常：{win.list_gallery.count()}"
item = next(
    win.list_gallery.item(i)
    for i in range(win.list_gallery.count())
    if win.list_gallery.item(i).data(Qt.UserRole) == clip
)
assert not item.icon().isNull(), "占位图标缺失"
placeholder_key = item.icon().cacheKey()
print("[OK] 画廊条目已创建（占位图标）")

# 等后台线程回填首帧（图标 cacheKey 变化即视为已换成首帧）
deadline = time.time() + 15
ready = False
while time.time() < deadline:
    app.processEvents()
    if item.icon().cacheKey() != placeholder_key:
        ready = True
        break
    time.sleep(0.05)
assert ready, "首帧未回填到画廊"
print("[OK] 首帧已回填到画廊条目（图标已替换）")

# 播放角标 / 占位图能画出来
pix = win._with_play_badge(win._video_placeholder_icon().pixmap(160, 160))
assert not pix.isNull() and pix.width() == 160
print("[OK] 播放角标绘制正常")


def wait_worker(seconds: float = 20) -> None:
    """等后台抽帧/解码线程自然收尾（含 deleteLater 之后 Python 引用被清掉）。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        th = win._thumb_thread
        if th is None:
            return
        try:
            if not th.isRunning():
                return
        except RuntimeError:
            return
        time.sleep(0.05)


wait_worker()

# 6) 回归：新出一张作品后刷新必须能看见（线上真实故障）
#    故障根因是线程 deleteLater 之后 self._thumb_thread 变成已销毁的 C++ 对象，
#    下一次 _reload_gallery 一碰它就抛异常 → clear() 走不到 → 列表卡在旧内容。
new_clip = make_clip(os.path.join(tmp, "clip_second.mp4"), n=6)
newer = time.time() + 10
os.utime(new_clip, (newer, newer))  # 保证 mtime 最新
win._reload_gallery()
assert win.list_gallery.count() == 3, f"刷新后条数异常：{win.list_gallery.count()}"
first = win.list_gallery.item(0)
assert first.text() == "clip_second.mp4", f"新作品没排在最前：{first.text()}"
print("[OK] 新增作品刷新后立刻出现在首位（原故障已修复）")

# 7) 连续刷新不失效（死线程引用回归）
wait_worker()
for i in range(3):
    win._reload_gallery()
    assert win.list_gallery.count() == 3, f"第 {i + 2} 次刷新条数异常"
print("[OK] 连续 4 次刷新均正常（死线程引用已清理）")

# 8) 图片也要能被后台解码回填（不能因为等视频而把图片漏掉）
img_path = os.path.join(tmp, "pic.png")
from PySide6.QtGui import QColor, QImage  # noqa: E402

src = QImage(640, 480, QImage.Format_RGB32)
src.fill(QColor(200, 120, 60))
assert src.save(img_path), "测试图片保存失败"
win._reload_gallery()
img_item = next(
    win.list_gallery.item(i)
    for i in range(win.list_gallery.count())
    if win.list_gallery.item(i).data(Qt.UserRole) == img_path
)
placeholder_key = img_item.icon().cacheKey()
deadline = time.time() + 15
while time.time() < deadline:
    app.processEvents()
    if img_item.icon().cacheKey() != placeholder_key:
        break
    time.sleep(0.05)
assert img_item.icon().cacheKey() != placeholder_key, "图片缩略图未回填"
print("[OK] 图片缩略图后台回填正常")

win._stop_thumb_worker(wait_ms=5000)
win.close()
print("视频缩略图测试全部通过")
