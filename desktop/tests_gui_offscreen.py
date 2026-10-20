"""GUI 离屏构建测试：不开窗口，验证 MainWindow 能完整构造、预设/画廊/VAE 列表加载。"""

import faulthandler
import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"
faulthandler.enable()  # 段错误时打印 Python 栈
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.gui.main_window import SettingsDialog, MainWindow  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

win = MainWindow()
win.show()
assert win.combo_vae.count() > 0, "VAE 列表为空"
print(f"[OK] 主窗口构建成功（VAE 列表 {win.combo_vae.count()} 项）")

dlg = SettingsDialog(win)
assert hasattr(dlg, "btn_clear_thumbs"), "设置里缺少缩略图缓存清理按钮"
assert dlg.btn_clear_thumbs.text(), "清理按钮没有文案"
print("[OK] 设置对话框构建成功（含缩略图缓存清理）")

# 预设下拉（可能为空，但不允许崩溃）
print(f"[OK] 预设下拉 {win.combo_preset.count()} 项")

# 图像/视频模式大按钮：视频模式 → 视频组可见 + 主 VAE 置灰
win.btn_video_mode.setChecked(True)
assert win.gb_video.isHidden() is False, "视频模式下视频组应可见"
assert win.combo_vae.isEnabled() is False, "视频模式下「VAE 模型」应置灰"
assert win.btn_video_mode.text() == "视频模式", f"按钮文案异常：{win.btn_video_mode.text()}"
print("[OK] 视频模式：视频组可见、主 VAE 置灰、按钮文案=视频模式")

# 图像模式 → 视频组收起 + 主 VAE 恢复
win.btn_video_mode.setChecked(False)
assert win.gb_video.isHidden() is True, "图像模式下视频组应收起"
assert win.combo_vae.isEnabled() is True, "图像模式下「VAE 模型」应恢复可用"
assert win.btn_video_mode.text() == "图像模式", f"按钮文案异常：{win.btn_video_mode.text()}"
print("[OK] 图像模式：视频组收起、主 VAE 可用、按钮文案=图像模式")

# 视频组本身不再带勾选框（标题栏无 checkbox）
assert win.gb_video.isCheckable() is False, "视频解码组不应再是勾选组"
print("[OK] 视频解码组已去掉勾选框")

win.btn_video_mode.setChecked(True)  # 复原默认（视频模式）
win.close()
print("GUI 离屏测试通过")

# 收尾：退出事件循环再让解释器退出，避免 QThread/QApplication
# 在解释器销毁阶段被回收（Linux 上表现为退出码 139）
app.quit()
sys.stdout.flush()
