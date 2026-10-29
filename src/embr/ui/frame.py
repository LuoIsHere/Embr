"""Frameless main window shell: top bar, three resizable columns, command bar (design doc
§6.1 "布局"). The panels themselves live in their own modules."""

from collections.abc import Callable

from PySide6.QtCore import QByteArray, QEvent, QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QMouseEvent, QResizeEvent
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSplitter,
    QSplitterHandle,
    QVBoxLayout,
    QWidget,
)

from embr.ui import icons
from embr.ui.theme import Palette
from embr.ui.widgets import button, label, set_prop, tag

MIN_WIDTH, MIN_HEIGHT = 1100, 700
SIDEBAR = (160, 236, 420)
"""min, default, max width."""
RIGHT = (280, 330, 560)
AUTO_HIDE_WIDTH = 1180
"""Below this window width the sidebar folds away on its own."""
EDGE = 6
"""Resize grip width at the window border."""
LOGO_SIZE = 20


class ResetHandle(QSplitterHandle):
    """Splitter handle; double-click restores the default sizes."""

    def __init__(self, orientation: Qt.Orientation, parent: "ResettableSplitter") -> None:
        super().__init__(orientation, parent)
        self._owner = parent

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        self._owner.reset_requested.emit(self._owner.indexOf(self))


class ResettableSplitter(QSplitter):
    reset_requested = Signal(int)
    """Index of the handle that was double-clicked."""

    def createHandle(self) -> QSplitterHandle:
        return ResetHandle(self.orientation(), self)


class TopBar(QWidget):
    """Menu and sidebar buttons, name and versions, window controls. Dragging an empty spot
    moves the window; double-click maximizes or restores."""

    def __init__(self, window: QMainWindow, versions: str) -> None:
        super().__init__()
        self.setObjectName("topbar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self._window = window
        self.menu_button = button("", "ibtn", tooltip="Menu")
        self.sidebar_button = button("", "ibtn", tooltip="Hide sidebar (Ctrl+B)")
        self.brand = QLabel("Embr")
        self.brand.setObjectName("brand")
        self.min_button = self._wc("Minimize")
        self.max_button = self._wc("Maximize")
        self.close_button = self._wc("Close", close=True)
        self.logo = QLabel()
        self.logo.setPixmap(icons.logo_pixmap(LOGO_SIZE))
        self.logo.setFixedSize(LOGO_SIZE, LOGO_SIZE)
        self.logo.setToolTip("Embr")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.logo)
        layout.addSpacing(4)
        for widget in (self.menu_button, self.sidebar_button):
            widget.setFixedSize(26, 26)
            layout.addWidget(widget)
        layout.addSpacing(4)
        layout.addWidget(self.brand)
        layout.addWidget(tag(versions, "gray"))
        layout.addStretch(1)
        for widget in (self.min_button, self.max_button, self.close_button):
            layout.addWidget(widget)
        self.setFixedHeight(38)
        self.min_button.clicked.connect(window.showMinimized)
        self.max_button.clicked.connect(self.toggle_maximized)
        self.close_button.clicked.connect(window.close)

    def _wc(self, tip: str, close: bool = False) -> QPushButton:
        widget = button("", "wc-close" if close else "wc", tooltip=tip)
        widget.setFixedHeight(38)
        widget.setCursor(Qt.CursorShape.ArrowCursor)
        return widget

    def apply_palette(self, p: Palette) -> None:
        self.menu_button.setIcon(icons.icon("menu", p.fg_2, hover_color=p.fg))
        self.sidebar_button.setIcon(icons.icon("panel-left", p.fg_2, hover_color=p.fg))
        self.min_button.setIcon(icons.icon("minimize", p.fg_2, 10))
        self.close_button.setIcon(icons.icon("close", p.fg_2, 10, hover_color="#ffffff"))
        self._palette = p
        self.update_max_icon()

    def update_max_icon(self) -> None:
        maximized = self._window.isMaximized()
        name = "restore" if maximized else "maximize"
        self.max_button.setIcon(icons.icon(name, self._palette.fg_2, 10))
        self.max_button.setToolTip("Restore" if maximized else "Maximize")

    def toggle_maximized(self) -> None:
        if self._window.isMaximized():
            self._window.showNormal()
        else:
            self._window.showMaximized()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._window.windowHandle().startSystemMove()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle_maximized()


class CommandBar(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("cmdbar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self._full = ""
        self.text = label("", mono=True)
        self.text.setObjectName("cmdtext")
        self.text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.copy_button = button("Copy", "ibtn")
        self.copy_button.setFixedWidth(72)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 5, 12, 5)
        layout.setSpacing(8)
        layout.addWidget(label("Equivalent command", tone="3"))
        layout.addWidget(self.text, 1)
        layout.addWidget(self.copy_button)
        self.copy_button.clicked.connect(self._copy)
        self._reset = QTimer(self)
        self._reset.setSingleShot(True)
        self._reset.setInterval(1500)
        self._reset.timeout.connect(self._copied_done)

    def set_command(self, text: str) -> None:
        self._full = text
        self.text.setToolTip(text)
        self._elide()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        metrics = self.text.fontMetrics()
        width = max(self.text.width(), 50)
        self.text.setText(metrics.elidedText(self._full, Qt.TextElideMode.ElideRight, width))

    def _copy(self) -> None:
        clipboard = QGuiApplication.clipboard()
        clipboard.setText(self._full)
        self.copy_button.setText("Copied")
        set_prop(self.copy_button, "copied", "1")
        self._reset.start()

    def _copied_done(self) -> None:
        self.copy_button.setText("Copy")
        set_prop(self.copy_button, "copied", "0")


class FrameWindow(QMainWindow):
    """The window shell. Panels are set with set_panels(); the owner wires the rest."""

    sidebar_toggled = Signal(bool)
    """True when visible."""

    def __init__(self, versions: str) -> None:
        super().__init__()
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setMinimumSize(MIN_WIDTH, MIN_HEIGHT)
        self.setMouseTracking(True)
        root = QWidget()
        root.setObjectName("window")
        root.setMouseTracking(True)
        self.topbar = TopBar(self, versions)
        self.columns = ResettableSplitter(Qt.Orientation.Horizontal)
        self.columns.setHandleWidth(6)
        self.columns.setChildrenCollapsible(False)
        self.cmdbar = CommandBar()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.topbar)
        layout.addWidget(self.columns, 1)
        layout.addWidget(self.cmdbar)
        self.setCentralWidget(root)
        self._user_hid_sidebar = False
        self._auto_hidden = False
        self.columns.reset_requested.connect(self._reset_column)
        self.topbar.sidebar_button.clicked.connect(self.toggle_sidebar)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)  # resize grips at the frameless border
        self.native_event_hook: Callable[[bytes, int], None] | None = None

    def set_panels(self, sidebar: QWidget, center: QWidget, right: QWidget) -> None:
        self.sidebar, self.center, self.right = sidebar, center, right
        for widget in (sidebar, center, right):
            self.columns.addWidget(widget)
        sidebar.setMinimumWidth(SIDEBAR[0])
        sidebar.setMaximumWidth(SIDEBAR[2])
        right.setMinimumWidth(RIGHT[0])
        right.setMaximumWidth(RIGHT[2])
        self.columns.setStretchFactor(1, 1)
        self.reset_columns()

    def reset_columns(self) -> None:
        total = max(self.columns.width(), MIN_WIDTH)
        self.columns.setSizes([SIDEBAR[1], total - SIDEBAR[1] - RIGHT[1], RIGHT[1]])

    def _reset_column(self, handle_index: int) -> None:
        sizes = self.columns.sizes()
        if handle_index == 1:
            sizes[1] += sizes[0] - SIDEBAR[1]
            sizes[0] = SIDEBAR[1]
        else:
            sizes[1] += sizes[2] - RIGHT[1]
            sizes[2] = RIGHT[1]
        self.columns.setSizes(sizes)

    @property
    def sidebar_visible(self) -> bool:
        return self.sidebar.isVisible()

    def toggle_sidebar(self) -> None:
        show = not self.sidebar.isVisible()
        self._user_hid_sidebar = not show
        self._auto_hidden = False
        self._set_sidebar(show)

    def _set_sidebar(self, show: bool) -> None:
        self.sidebar.setVisible(show)
        self.topbar.sidebar_button.setToolTip(
            "Hide sidebar (Ctrl+B)" if show else "Show sidebar (Ctrl+B)"
        )
        self.sidebar_toggled.emit(show)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        narrow = self.width() < AUTO_HIDE_WIDTH
        if narrow and self.sidebar.isVisible() and not self._user_hid_sidebar:
            self._auto_hidden = True
            self._set_sidebar(False)
        elif not narrow and self._auto_hidden:
            self._auto_hidden = False
            self._set_sidebar(True)

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.WindowStateChange:
            self.topbar.update_max_icon()
        super().changeEvent(event)

    def nativeEvent(
        self, eventType: QByteArray | bytes | bytearray | memoryview, message: int, /
    ) -> tuple[bool, int]:
        if self.native_event_hook is not None:
            kind = bytes(eventType.data() if isinstance(eventType, QByteArray) else eventType)
            self.native_event_hook(kind, int(message))
        return False, 0

    # ---- resizing a frameless window from its border ----------------------------------

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if not isinstance(watched, QWidget) or watched.window() is not self or self.isMaximized():
            return False
        if event.type() == QEvent.Type.MouseMove and isinstance(event, QMouseEvent):
            if event.buttons() == Qt.MouseButton.NoButton:
                edges = self._edges(self.mapFromGlobal(event.globalPosition().toPoint()))
                self._set_cursor(edges)
        elif event.type() == QEvent.Type.MouseButtonPress and isinstance(event, QMouseEvent):
            edges = self._edges(self.mapFromGlobal(event.globalPosition().toPoint()))
            if edges and event.button() == Qt.MouseButton.LeftButton:
                self.windowHandle().startSystemResize(edges)
                return True
        return False

    def _edges(self, pos: QPoint) -> Qt.Edge:
        edges = Qt.Edge(0)
        if pos.x() <= EDGE:
            edges |= Qt.Edge.LeftEdge
        elif pos.x() >= self.width() - EDGE:
            edges |= Qt.Edge.RightEdge
        if pos.y() <= EDGE:
            edges |= Qt.Edge.TopEdge
        elif pos.y() >= self.height() - EDGE:
            edges |= Qt.Edge.BottomEdge
        return edges

    def _set_cursor(self, edges: Qt.Edge) -> None:
        horizontal = bool(edges & (Qt.Edge.LeftEdge | Qt.Edge.RightEdge))
        vertical = bool(edges & (Qt.Edge.TopEdge | Qt.Edge.BottomEdge))
        if horizontal and vertical:
            main = bool(edges & Qt.Edge.LeftEdge) == bool(edges & Qt.Edge.TopEdge)
            shape = Qt.CursorShape.SizeFDiagCursor if main else Qt.CursorShape.SizeBDiagCursor
        elif horizontal:
            shape = Qt.CursorShape.SizeHorCursor
        elif vertical:
            shape = Qt.CursorShape.SizeVerCursor
        else:
            self.unsetCursor()
            return
        self.setCursor(shape)
