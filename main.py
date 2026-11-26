import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from ui.main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName('Delta Harmonica Player')
    window = MainWindow()
    window.closed.connect(app.quit, Qt.QueuedConnection)
    window.show()
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
