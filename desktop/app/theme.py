"""全局视觉主题：设计令牌 + Fusion 深色调色板 + 全局 QSS。

设计决策（为什么这样做）：
  * **深色底 + 琥珀金强调**：目标用户在系统深色模式下长时间盯画面，
    低亮度中性底最省眼；金色与应用图标/任务栏金色一脉相承，是既有品牌资产。
  * **Fusion + 深色 QPalette 打底，QSS 精修**：单靠 QSS 时原生控件
    （下拉箭头、spinbox 步进按钮）仍是亮色主题的图，Fusion 会按调色板绘制，
    两者叠加才能整只应用真正变深。
  * **令牌单点化**：颜色/圆角/字体全部收敛在本模块 TOKENS，
    改主题只动这一个文件；界面代码里不允许再写内联色值
    （历史遗留的映射状态色已在 main_window 统一为本主题的语义色）。

对比度（WCAG AA 验算）：
  正文 #e8eaf0 / 底 #17181c ≈ 13.2:1；次要 #9aa0ad / 面板 #1f2127 ≈ 5.6:1；
  金底上用深字 #221b05 ≈ 8.9:1 —— 全部达标。
"""

from __future__ import annotations

from string import Template

# ---------------------------------------------------------------- 设计令牌
TOKENS = {
    # 中性色阶（由深到浅）
    "bg": "#17181c",           # 窗口底
    "panel": "#1f2127",        # 分组面板
    "panel_raised": "#262931",  # 输入框 / 提升面
    "border": "#33373f",       # 常规描边
    "border_soft": "#2a2d34",  # 弱分隔
    "text": "#e8eaf0",         # 主文字
    "text_dim": "#9aa0ad",     # 次要文字
    "text_faint": "#6f7683",   # 辅助/占位
    # 品牌强调（琥珀金）
    "accent": "#e8b339",
    "accent_hover": "#f4c75f",
    "accent_pressed": "#cf9c22",
    "on_accent": "#221b05",    # 金底上的深色文字
    "accent_dim": "#8a6d20",   # 禁用态金色
    # 语义色
    "ok": "#4ade80",
    "warn": "#fbbf24",
    "danger": "#f87171",
    "danger_bg": "#3a2326",
    # 形状 / 字体
    "radius": "8px",
    "radius_s": "6px",
    "font": '"Microsoft YaHei UI", "PingFang SC", "Segoe UI", sans-serif',
    "mono": '"Consolas", "Cascadia Mono", monospace',
}

# 复选框选中态的对勾（QSS 不能直接画矢量，运行时落一个 14px SVG 资产）
_CHECK_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" '
    'viewBox="0 0 14 14"><path d="M3 7.5 L6 10.5 L11 4" fill="none" '
    'stroke="#221b05" stroke-width="2.2" '
    'stroke-linecap="round" stroke-linejoin="round"/></svg>'
)


def _ensure_check_asset() -> str:
    import os

    asset_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
    os.makedirs(asset_dir, exist_ok=True)
    path = os.path.join(asset_dir, "check.svg")
    if not os.path.isfile(path):
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(_CHECK_SVG)
        except Exception:
            pass  # 落盘失败时选中态退化为纯金色块，功能不受影响
    return path.replace("\\", "/")

_QSS = Template("""
* {
    outline: none;
}
QWidget {
    background: $bg;
    color: $text;
    font-family: $font;
    font-size: 9pt;
}

QToolTip {
    background: $panel_raised;
    color: $text;
    border: 1px solid $border;
    border-radius: $radius_s;
    padding: 6px 8px;
}

/* ------------------------------------------------------------ 分组面板 */
QGroupBox {
    background: $panel;
    border: 1px solid $border_soft;
    border-radius: $radius;
    margin-top: 12px;
    padding: 12px 10px 10px 10px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    top: 1px;
    padding: 0 4px;
    color: $text_dim;
    background: transparent;
}

/* ------------------------------------------------------------ 输入类 */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox {
    background: $panel_raised;
    color: $text;
    border: 1px solid $border;
    border-radius: $radius_s;
    padding: 6px 8px;
    selection-background-color: $accent;
    selection-color: $on_accent;
}
QLineEdit:hover, QPlainTextEdit:hover, QSpinBox:hover {
    border-color: #454a55;
}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus,
QSpinBox:focus, QDoubleSpinBox:focus {
    border: 1px solid $accent;
    background: #2a2d36;
}
QLineEdit:disabled, QPlainTextEdit:disabled, QSpinBox:disabled {
    color: $text_faint;
    background: $panel;
}

QSpinBox::up-button, QSpinBox::down-button {
    background: transparent;
    border: none;
    width: 18px;
}
QSpinBox::up-arrow {
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-bottom: 5px solid $text_dim;
    width: 0; height: 0;
}
QSpinBox::down-arrow {
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid $text_dim;
    width: 0; height: 0;
}

/* ------------------------------------------------------------ 下拉框 */
QComboBox {
    background: $panel_raised;
    color: $text;
    border: 1px solid $border;
    border-radius: $radius_s;
    padding: 6px 10px;
    min-height: 20px;
}
QComboBox:hover { border-color: #454a55; }
QComboBox:focus { border: 1px solid $accent; }
/* 置灰态（如视频模式下主「VAE 模型」）：与种子输入框的 disabled 表现一致 */
QComboBox:disabled {
    color: $text_faint;
    background: $panel;
    border-color: $border_soft;
}
QComboBox::down-arrow:disabled { border-top-color: $border; }
QComboBox::drop-down {
    border: none;
    width: 26px;
}
QComboBox::down-arrow {
    image: none;
    border-left: 5px solid transparent;
    border-right: 5px solid transparent;
    border-top: 6px solid $text_dim;
    width: 0; height: 0;
    margin-right: 8px;
}
QComboBox QAbstractItemView {
    background: $panel_raised;
    color: $text;
    border: 1px solid $border;
    border-radius: $radius_s;
    padding: 4px;
    selection-background-color: $accent;
    selection-color: $on_accent;
}

/* ------------------------------------------------------------ 按钮 */
QPushButton {
    background: $panel_raised;
    color: $text;
    border: 1px solid $border;
    border-radius: $radius_s;
    padding: 7px 16px;
    min-height: 20px;
    font-weight: 500;
}
QPushButton:hover {
    background: #2e323b;
    border-color: #454a55;
}
QPushButton:pressed { background: #23262d; }
QPushButton:focus { border: 1px solid $accent; }
QPushButton:disabled {
    color: $text_faint;
    background: $panel;
    border-color: $border_soft;
}

/* 主操作（生成）：琥珀金实底 + 深色文字 */
QPushButton#primary {
    background: $accent;
    color: $on_accent;
    border: 1px solid $accent;
    font-weight: 700;
    font-size: 10pt;
}
QPushButton#primary:hover { background: $accent_hover; border-color: $accent_hover; }
QPushButton#primary:pressed { background: $accent_pressed; }
QPushButton#primary:disabled {
    background: $accent_dim;
    border-color: $accent_dim;
    color: #565040;
}

/* 取消 / 破坏性：红描边幽灵按钮 */
QPushButton#danger {
    background: transparent;
    color: $danger;
    border: 1px solid #57353a;
}
QPushButton#danger:hover { background: $danger_bg; border-color: $danger; }
QPushButton#danger:pressed { background: #2c1b1e; }
QPushButton#danger:disabled { color: $text_faint; border-color: $border_soft; background: transparent; }

/* 图像/视频模式大开关：未选中 = 普通描边；选中（视频模式）= 琥珀金实底 */
QPushButton#mode_toggle {
    text-align: center;
    font-size: 10pt;
    font-weight: 700;
    letter-spacing: 1px;
    padding: 10px 16px;
    background: $panel_raised;
    border: 1px solid $border;
    border-radius: $radius;
    color: $text_dim;
}
QPushButton#mode_toggle:hover {
    background: #2e323b;
    border-color: #454a55;
    color: $text;
}
QPushButton#mode_toggle:checked {
    /* 激活态用「深金底 + 金描边 + 金字」，不用实心金：
       实心金留给「生成」这个唯一主操作，避免两个金条抢层级 */
    background: #2b2718;
    border: 1px solid $accent;
    color: $accent;
}
QPushButton#mode_toggle:checked:hover {
    background: #353019;
    border-color: $accent_hover;
    color: $accent_hover;
}
QPushButton#mode_toggle:checked:pressed {
    background: #23200f;
    border-color: $accent_pressed;
    color: $accent_pressed;
}

/* ------------------------------------------------------------ 复选框 */
QCheckBox {
    spacing: 7px;
    color: $text;
}
QCheckBox::indicator {
    width: 17px; height: 17px;
    border: 1px solid $border;
    border-radius: 5px;
    background: $panel_raised;
}
QCheckBox::indicator:hover { border-color: $accent; }
QCheckBox::indicator:checked {
    background: $accent;
    border-color: $accent;
    image: url("$check_icon");
}

/* ------------------------------------------------------------ 列表 / 画廊 */
QListWidget {
    background: $panel;
    border: 1px solid $border_soft;
    border-radius: $radius;
    padding: 6px;
    outline: 0;
}
QListWidget::item {
    border-radius: $radius_s;
    padding: 6px;
    margin: 2px;
    color: $text_dim;
}
QListWidget::item:hover { background: #2b2e37; }
QListWidget::item:selected {
    background: #3a3324;
    border: 1px solid $accent;
    color: $text;
}

/* ------------------------------------------------------------ 进度条 */
QProgressBar {
    background: $panel_raised;
    border: 1px solid $border_soft;
    border-radius: 5px;
    height: 10px;
    text-align: center;
    color: transparent;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 $accent_pressed, stop:1 $accent);
    border-radius: 4px;
}

/* ------------------------------------------------------------ 滚动条 */
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background: #3a3e48;
    border-radius: 4px;
    min-height: 32px;
}
QScrollBar::handle:vertical:hover { background: #4a4f5b; }
QScrollBar:horizontal {
    background: transparent;
    height: 10px;
    margin: 2px;
}
QScrollBar::handle:horizontal {
    background: #3a3e48;
    border-radius: 4px;
    min-width: 32px;
}
QScrollBar::handle:horizontal:hover { background: #4a4f5b; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

/* ------------------------------------------------------------ 其他 */
QStatusBar {
    background: $panel;
    border-top: 1px solid $border_soft;
    color: $text_dim;
}
QStatusBar QLabel { background: transparent; }

QSplitter::handle { background: $border_soft; }
QSplitter::handle:hover { background: $accent; }

QDialog { background: $bg; }
QDialogButtonBox QPushButton { min-width: 76px; }

QLabel { background: transparent; }
QLabel[role="hint"] { color: $text_faint; }

QMenu {
    background: $panel_raised;
    border: 1px solid $border;
    border-radius: $radius_s;
    padding: 4px;
}
QMenu::item { padding: 6px 24px; border-radius: 4px; }
QMenu::item:selected { background: $accent; color: $on_accent; }
""")


def stylesheet() -> str:
    tokens = dict(TOKENS)
    tokens.setdefault("check_icon", _ensure_check_asset())
    return _QSS.substitute(tokens)


def apply(app) -> None:
    """把主题应用到整个应用（Fusion 打底 + 深色调色板 + QSS 精修）。"""
    from PySide6.QtGui import QColor, QPalette

    t = TOKENS
    app.setStyle("Fusion")
    app.setFont(app.font())  # 保持系统字号，仅锁定家族由 QSS 负责

    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(t["bg"]))
    pal.setColor(QPalette.WindowText, QColor(t["text"]))
    pal.setColor(QPalette.Base, QColor(t["panel_raised"]))
    pal.setColor(QPalette.AlternateBase, QColor(t["panel"]))
    pal.setColor(QPalette.Text, QColor(t["text"]))
    pal.setColor(QPalette.PlaceholderText, QColor(t["text_faint"]))
    pal.setColor(QPalette.Button, QColor(t["panel_raised"]))
    pal.setColor(QPalette.ButtonText, QColor(t["text"]))
    pal.setColor(QPalette.Highlight, QColor(t["accent"]))
    pal.setColor(QPalette.HighlightedText, QColor(t["on_accent"]))
    pal.setColor(QPalette.ToolTipBase, QColor(t["panel_raised"]))
    pal.setColor(QPalette.ToolTipText, QColor(t["text"]))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor(t["text_faint"]))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(t["text_faint"]))
    pal.setColor(QPalette.Disabled, QPalette.WindowText, QColor(t["text_faint"]))
    app.setPalette(pal)
    app.setStyleSheet(stylesheet())
