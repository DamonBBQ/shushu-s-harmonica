from __future__ import annotations

FOREGROUND_MATCH = '#A9D6FF'
FOREGROUND_MUTED = '#9AACBF'

_STATE_COLORS = {
    'playing': '#A7DBED',
    'finished': '#A7DBED',
    'paused': '#F4CE8B',
    'waiting_game': '#F4CE8B',
}


def state_color(state: str) -> str:
    return _STATE_COLORS.get(state, FOREGROUND_MUTED)


MAIN_WINDOW_STYLE = '''
    QMainWindow, QWidget#workspace { background-color: #0D1420; }
    QWidget { color: #DFEAF8; font-size: 13px; }
    QLabel { background-color: transparent; }
    QFrame#header {
        background: qlineargradient(x1: 0, y1: 0, x2: 1, y2: 0,
                                    stop: 0 #192B43, stop: 1 #141F30);
        border: 1px solid #30455E;
        border-radius: 12px;
    }
    QLabel#title { color: #F1F6FE; font-size: 25px; font-weight: 700; }
    QLabel#subtitle { color: #B4C6DB; font-size: 13px; }
    QLabel#info { color: #B8CBE2; font-size: 12px; }
    QLabel#hint { color: #A7BBD4; font-size: 12px; }
    QLabel#trackTitle { color: #E4EFFF; font-size: 15px; font-weight: 700; }
    QLabel#progress { color: #B4CBE6; font-size: 12px; }
    QGroupBox {
        background-color: #141F2E;
        border: 1px solid #2B3C53;
        border-radius: 10px;
        margin-top: 9px;
        font-weight: 600;
    }
    QGroupBox::title {
        color: #DFEBFA;
        background-color: #141F2E;
        subcontrol-origin: margin;
        subcontrol-position: top left;
        left: 12px;
        padding: 0 5px;
    }
    QGroupBox#playerCard { background-color: #162338; border-color: #3A526F; }
    QGroupBox#playerCard::title { color: #E3EFFF; background-color: #162338; }
    QGroupBox#logCard { background-color: #121D2B; }
    QGroupBox#logCard::title { background-color: #121D2B; }
    QFrame#trackSummary {
        background-color: #192A40;
        border: 1px solid #2F4864;
        border-radius: 6px;
    }
    QFrame#playbackSummary {
        background-color: #101A2A;
        border: 1px solid #314863;
        border-radius: 7px;
    }
    QProgressBar#trackProgress {
        background-color: #283D57;
        border: 0;
        border-radius: 2px;
        min-height: 5px;
        max-height: 5px;
    }
    QProgressBar#trackProgress::chunk { background-color: #86B6FF; border-radius: 2px; }
    QListWidget, QPlainTextEdit, QLineEdit, QSpinBox {
        background-color: #101A29;
        border: 1px solid #344960;
        border-radius: 6px;
        padding: 4px 7px;
        selection-background-color: #2F517A;
        selection-color: #F4F8FD;
    }
    QListWidget:focus, QPlainTextEdit:focus, QLineEdit:focus, QSpinBox:focus {
        border-color: #77A9E4;
    }
    QLineEdit:disabled, QSpinBox:disabled {
        color: #8DA1B8;
        background-color: #172231;
        border-color: #2B3C53;
    }
    QListWidget#scoreList { padding: 4px; outline: none; alternate-background-color: #142236; }
    QListWidget#scoreList::item {
        color: #C9D8EA;
        border: 1px solid transparent;
        border-radius: 5px;
        min-height: 24px;
        padding: 3px 8px;
        margin: 1px 0;
    }
    QListWidget#scoreList::item:hover { background-color: #1C2C42; }
    QListWidget#scoreList::item:selected {
        color: #F2F7FF;
        background-color: #284565;
        border: 1px solid #3E638F;
    }
    QListWidget#scoreList::item:disabled { color: #8499B2; }
    QListWidget#scoreList::item:selected:disabled { color: #B5C9E0; }
    QPlainTextEdit#logText {
        color: #B6C7DB;
        background-color: #0E1826;
        border-color: #263A52;
        padding: 5px 8px;
    }
    QPushButton {
        color: #DFEAFA;
        background-color: #223248;
        border: 1px solid #3B526E;
        border-radius: 6px;
        min-height: 17px;
        padding: 5px 10px;
    }
    QPushButton:hover { background-color: #2C4260; border-color: #5C7CA2; }
    QPushButton:pressed { background-color: #1B2A3E; }
    QPushButton:focus { border-color: #9ACCFF; }
    QPushButton:disabled { color: #859AB2; background-color: #192636; border-color: #2E435A; }
    QPushButton#primary:enabled {
        color: #0B1A2E;
        background-color: #86B6FF;
        border-color: #A0C8FF;
        font-weight: 700;
    }
    QPushButton#primary:hover { background-color: #A3CAFF; }
    QPushButton#primary:pressed { background-color: #70A3F0; }
    QPushButton#primary:focus { border-color: #E2EEFF; }
    QPushButton#importButton:enabled { color: #E1EEFF; background-color: #294364; border-color: #4A6B91; }
    QPushButton#importButton:hover { background-color: #35577E; border-color: #7296BC; }
    QPushButton#importButton:pressed { background-color: #233B59; }
    QPushButton#importButton:focus { border-color: #A0C8FF; }
    QPushButton#miniLauncher {
        color: #DBECFF;
        background-color: #263E5B;
        border-color: #496889;
        padding: 6px 12px;
    }
    QPushButton#miniLauncher:hover { background-color: #345372; border-color: #7296BC; }
    QPushButton#miniLauncher:pressed { background-color: #20364F; }
    QPushButton#miniLauncher:focus { border-color: #A0C8FF; }
    QPushButton#stopButton:enabled {
        color: #F1CAD2;
        background-color: #332A39;
        border-color: #614557;
    }
    QPushButton#stopButton:hover { background-color: #473244; border-color: #8E6178; }
    QPushButton#stopButton:pressed { background-color: #2B2230; }
    QPushButton#stopButton:focus { border-color: #F1CAD2; }
    QCheckBox { spacing: 7px; }
    QCheckBox:disabled { color: #9BAFC7; }
    QCheckBox::indicator {
        width: 8px;
        height: 8px;
        background-color: #111B2B;
        border: 3px solid #536D8B;
        border-radius: 4px;
    }
    QCheckBox::indicator:checked { background-color: #D5E7FF; border-color: #4974A8; }
    QCheckBox::indicator:focus, QCheckBox::indicator:hover { border-color: #94BCEB; }
    QCheckBox::indicator:disabled { background-color: #172437; border-color: #394D65; }
    QCheckBox::indicator:checked:disabled { background-color: #8CA3BE; }
    QSlider::groove:horizontal { height: 4px; background-color: #304660; border-radius: 2px; }
    QSlider::sub-page:horizontal { background-color: #6D9EDB; border-radius: 2px; }
    QSlider::handle:horizontal {
        width: 12px;
        margin: -5px 0;
        background-color: #C5DFFF;
        border: 1px solid #90B6E1;
        border-radius: 7px;
    }
    QSlider::handle:horizontal:hover, QSlider::handle:horizontal:focus {
        background-color: #E1EDFF;
        border-color: #9FC4F3;
    }
    QSlider::sub-page:horizontal:disabled { background-color: #354A62; }
    QSlider::handle:horizontal:disabled { background-color: #7891AF; border-color: #667F9A; }
    QScrollBar:vertical { width: 9px; background-color: #101A29; margin: 2px 0; }
    QScrollBar::handle:vertical { min-height: 20px; background-color: #415977; border-radius: 3px; }
    QScrollBar::handle:vertical:hover { background-color: #5C7A9F; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background-color: transparent; }
    QStatusBar { background-color: #101A29; border-top: 1px solid #293C53; }
    QStatusBar::item { border: 0; }
    QStatusBar QLabel { color: #A7B9CE; font-size: 12px; padding: 3px 7px; }
    QLabel#state { color: #9AACBF; font-weight: 600; }
    QToolTip { color: #EBF4FF; background-color: #20334C; border: 1px solid #506F92; padding: 4px; }
'''


MINI_WINDOW_STYLE = '''
    QWidget#miniWindow { background-color: #141F2E; border: 1px solid #4B6688; }
    QLabel { color: #DFEAF8; background-color: transparent; font-size: 12px; }
    QLabel#miniTitle { color: #F1F6FE; font-size: 15px; font-weight: 700; }
    QLabel#miniStatus { color: #A7DBED; font-weight: 600; }
    QLabel#miniProgress, QLabel#miniOpacityLabel, QLabel#miniOpacityValue { color: #BCCFE6; }
    QPushButton {
        color: #DFEAFA;
        background-color: #223248;
        border: 1px solid #3B526E;
        border-radius: 5px;
        min-height: 16px;
        padding: 4px 7px;
        font-size: 12px;
    }
    QPushButton:hover { background-color: #2C4260; border-color: #5C7CA2; }
    QPushButton:pressed { background-color: #1B2A3E; }
    QPushButton:disabled { color: #859AB2; background-color: #192636; border-color: #2E435A; }
    QPushButton#miniAction:enabled {
        color: #0B1A2E;
        background-color: #86B6FF;
        border-color: #A0C8FF;
        font-weight: 700;
    }
    QPushButton#miniAction:hover { background-color: #A3CAFF; }
    QPushButton#miniAction:pressed { background-color: #70A3F0; }
    QPushButton#miniStop:enabled { color: #F1CAD2; background-color: #332A39; border-color: #614557; }
    QPushButton#miniStop:hover { background-color: #473244; border-color: #8E6178; }
    QPushButton#miniStop:pressed { background-color: #2B2230; }
    QPushButton#miniRestore { padding: 2px 7px; }
    QPushButton#miniChoose:checked { background-color: #284565; border-color: #6088B5; }
    QListWidget#miniScoreList {
        background-color: #101A29;
        color: #DFEAF8;
        border: 1px solid #344960;
        border-radius: 6px;
        padding: 3px;
        outline: none;
        font-size: 12px;
    }
    QListWidget#miniScoreList::item { padding: 4px 7px; border-radius: 4px; }
    QListWidget#miniScoreList::item:hover { background-color: #223B59; }
    QListWidget#miniScoreList::item:selected { color: #F2F7FF; background-color: #284565; }
    QScrollBar:vertical { width: 8px; background-color: #101A29; }
    QScrollBar::handle:vertical { min-height: 20px; background-color: #415977; border-radius: 3px; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background-color: transparent; }
    QSlider::groove:horizontal { height: 4px; background-color: #354C67; border-radius: 2px; }
    QSlider::sub-page:horizontal { background-color: #6D9EDB; border-radius: 2px; }
    QSlider::handle:horizontal { width: 12px; margin: -4px 0; border-radius: 6px; background-color: #C5DFFF; }
    QSlider::handle:horizontal:hover { background-color: #E1EDFF; }
    QToolTip { color: #EBF4FF; background-color: #20334C; border: 1px solid #506F92; padding: 4px; }
'''
