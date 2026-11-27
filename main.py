import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from core.version import __version__
from ui.main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName('Delta Harmonica Player')
    app.setApplicationVersion(__version__)
    window = MainWindow()
    window.setWindowTitle(f'{window.windowTitle()} v{app.applicationVersion()}')
    window.closed.connect(app.quit, Qt.QueuedConnection)
    window.show()
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
