"""RH 出图器入口。"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication

from app import paths, theme
from app.gui.main_window import MainWindow


def main() -> int:
    paths.ensure_dirs()
    # 首次启动自动在桌面创建快捷方式（幂等；失败静默）
    try:
        from app.shortcuts import ensure_desktop_shortcut

        ensure_desktop_shortcut()
    except Exception:
        pass
    app = QApplication(sys.argv)
    app.setApplicationName("RH-Standalone")
    theme.apply(app)  # 深色主题：Fusion + 调色板 + QSS（见 app/theme.py）
    icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app", "icon.ico")
    if os.path.isfile(icon_path):
        from PySide6.QtGui import QIcon

        app.setWindowIcon(QIcon(icon_path))
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
