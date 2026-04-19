import sys
import os
import subprocess
import re
import json

# --- CRITICAL: Inject Embedded DLLs before importing MPV ---
def get_base_path():
    """Finds the hidden temp folder where PyInstaller extracts the EXE contents."""
    if hasattr(sys, '_MEIPASS'):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))

if os.name == 'nt':
    # Force Python to find mpv-2.dll in the embedded temp folder
    os.add_dll_directory(get_base_path())

import mpv
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QPushButton, QLabel, QFileDialog, 
                             QGroupBox, QLineEdit, QMessageBox, 
                             QStackedLayout, QTabWidget, QSlider, QFrame)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QPainter, QColor

def get_ext_path(binary_name):
    return os.path.join(get_base_path(), binary_name)

# --- Pro Dark Theme ---
DARK_STYLESHEET = """
QMainWindow, QWidget { background-color: #1e1e1e; color: #d4d4d4; font-family: 'Segoe UI', Arial; }
QPushButton { background-color: #0e639c; color: white; border: none; padding: 6px 12px; border-radius: 4px; font-weight: bold; }
QPushButton:hover { background-color: #1177bb; }
QPushButton:disabled { background-color: #3e3e42; color: #888888; }
QLineEdit { background-color: #3c3c3c; border: 1px solid #555555; padding: 4px; color: white; border-radius: 2px; }
QGroupBox { border: 1px solid #555555; border-radius: 4px; margin-top: 10px; padding-top: 15px; font-weight: bold; }
QSlider::groove:horizontal { border: 1px solid #999999; height: 8px; background: #3c3c3c; margin: 2px 0; border-radius: 4px; }
QSlider::handle:horizontal { background: #007acc; border: 1px solid #5c5c5c; width: 14px; margin: -4px 0; border-radius: 7px; }
"""

class MatteOverlay(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.mattes = {"Top": 0, "Bottom": 0, "Left": 0, "Right": 0}
        self.vid_w, self.vid_h = 1920, 1080

    def update_overlay(self, mattes, vid_w, vid_h):
        self.mattes = mattes
        self.vid_w, self.vid_h = max(1, vid_w), max(1, vid_h)
        self.update()

    def paintEvent(self, event):
        if not self.isVisible() or self.vid_w == 0: return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(255, 0, 0, 110))
        painter.setPen(Qt.PenStyle.NoPen)

        scale = min(self.width() / self.vid_w, self.height() / self.vid_h)
        drawn_w, drawn_h = self.vid_w * scale, self.vid_h * scale
        offset_x, offset_y = (self.width() - drawn_w) / 2, (self.height() - drawn_h) / 2

        t, b = self.mattes.get("Top", 0) * scale, self.mattes.get("Bottom", 0) * scale
        l, r = self.mattes.get("Left", 0) * scale, self.mattes.get("Right", 0) * scale

        if t > 0: painter.drawRect(int(offset_x), int(offset_y), int(drawn_w), int(t))
        if b > 0: painter.drawRect(int(offset_x), int(offset_y + drawn_h - b), int(drawn_w), int(b))
        if l > 0: painter.drawRect(int(offset_x), int(offset_y), int(l), int(drawn_h))
        if r > 0: painter.drawRect(int(offset_x + drawn_w - r), int(offset_y), int(r), int(drawn_h))

class MediaScanner:
    @staticmethod
    def scan_file(filepath):
        try:
            ffprobe_exe = get_ext_path("ffprobe.exe")
            ffmpeg_exe = get_ext_path("ffmpeg.exe")

            probe_cmd = [ffprobe_exe, "-v", "error", "-show_entries", "format=duration:stream=width,height", "-of", "json", filepath]
            info = json.loads(subprocess.run(probe_cmd, stdout=subprocess.PIPE, text=True, creationflags=subprocess.CREATE_NO_WINDOW).stdout)
            
            vid_w = int(info['streams'][0]['width'])
            vid_h = int(info['streams'][0]['height'])
            duration = float(info['format']['duration'])

            # Fast Sampling Engine
            samples = [duration * 0.25, duration * 0.50, duration * 0.75]
            raw_crops = []

            for ts in samples:
                cmd = [ffmpeg_exe, "-ss", str(ts), "-i", filepath, "-vframes", "3", "-vf", "cropdetect=24:16:0", "-f", "null", "-"]
                result = subprocess.run(cmd, stderr=subprocess.PIPE, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
                crops = re.findall(r'crop=(\d+):(\d+):(\d+):(\d+)', result.stderr)
                if crops:
                    raw_crops.extend([(int(c[0]), int(c[1]), int(c[2]), int(c[3])) for c in crops])

            if not raw_crops:
                raise ValueError("No mattes detected. Video may be completely black.")

            crop_w, crop_h, crop_x, crop_y = max(set(raw_crops), key=raw_crops.count)
            primary_mattes = {"Top": crop_y, "Bottom": vid_h - (crop_y + crop_h), "Left": crop_x, "Right": vid_w - (crop_x + crop_w)}

            return {"success": True, "mattes": primary_mattes, "variable_ar": len(set(raw_crops)) > 1}
        except Exception as e:
            return {"success": False, "error": str(e)}

class AnalysisThread(QThread):
    result_signal = pyqtSignal(dict)
    def __init__(self, filepath):
        super().__init__()
        self.filepath = filepath
    def run(self):
        self.result_signal.emit(MediaScanner.scan_file(self.filepath))

class StudioMatteApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Studio QC: Standalone MPV Pipeline")
        self.setGeometry(100, 100, 1200, 800)
        self.setStyleSheet(DARK_STYLESHEET)
        self.filepath, self.vid_w, self.vid_h = "", 1920, 1080
        
        self.init_ui()
        self.player = mpv.MPV(wid=str(int(self.video_frame.winId())), keep_open=True, profile='fast')
        
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.update_ui_timer)

    def init_ui(self):
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self.tab_qc = QWidget()
        self.setup_qc_tab()
        self.tabs.addTab(self.tab_qc, "Single File QC")

    def setup_qc_tab(self):
        layout = QHBoxLayout(self.tab_qc)

        vid_layout, self.vid_container = QVBoxLayout(), QWidget()
        self.stacked = QStackedLayout(self.vid_container)
        self.stacked.setStackingMode(QStackedLayout.StackingMode.StackAll)
        
        self.video_frame = QFrame()
        self.video_frame.setStyleSheet("background-color: black;")
        self.video_frame.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
        self.video_frame.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        
        self.overlay = MatteOverlay()
        self.stacked.addWidget(self.overlay) 
        self.stacked.addWidget(self.video_frame) 
        vid_layout.addWidget(self.vid_container, stretch=1)

        timeline_layout = QHBoxLayout()
        self.btn_play = QPushButton("Play/Pause")
        self.btn_play.clicked.connect(self.toggle_play)
        timeline_layout.addWidget(self.btn_play)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.sliderMoved.connect(self.set_position)
        timeline_layout.addWidget(self.slider)
        
        self.lbl_time = QLabel("00:00 / 00:00")
        timeline_layout.addWidget(self.lbl_time)
        vid_layout.addLayout(timeline_layout)

        layout.addLayout(vid_layout, stretch=3)

        side = QVBoxLayout()
        self.btn_load = QPushButton("Load Video")
        self.btn_load.clicked.connect(self.load_video)
        side.addWidget(self.btn_load)

        self.btn_scan = QPushButton("Auto-Detect Mattes (Fast Scan)")
        self.btn_scan.setEnabled(False)
        self.btn_scan.clicked.connect(self.start_scan)
        side.addWidget(self.btn_scan)

        self.lbl_status = QLabel("Ready")
        side.addWidget(self.lbl_status)

        self.grp_mattes = QGroupBox("Matte Overrides")
        m_layout = QVBoxLayout()
        self.inputs = {}
        for p in ["Top", "Bottom", "Left", "Right"]:
            r = QHBoxLayout()
            r.addWidget(QLabel(f"{p}:"))
            e = QLineEdit("0")
            e.textChanged.connect(self.trigger_overlay)
            r.addWidget(e)
            self.inputs[p] = e
            m_layout.addLayout(r)
        self.grp_mattes.setLayout(m_layout)
        side.addWidget(self.grp_mattes)
        
        side.addStretch()
        layout.addLayout(side, stretch=1)

    def load_video(self):
        fname, _ = QFileDialog.getOpenFileName(self, "Open Media", "", "Video (*.mp4 *.mov *.mkv *.mxf *.avi)")
        if fname:
            self.filepath = fname
            self.player.play(self.filepath)
            self.player.pause = True
            
            ffprobe_exe = get_ext_path("ffprobe.exe")
            info = json.loads(subprocess.run([ffprobe_exe, "-v", "error", "-show_entries", "stream=width,height", "-of", "json", self.filepath], stdout=subprocess.PIPE, text=True, creationflags=subprocess.CREATE_NO_WINDOW).stdout)
            self.vid_w, self.vid_h = int(info['streams'][0]['width']), int(info['streams'][0]['height'])
            
            self.lbl_status.setText(f"Loaded: {os.path.basename(fname)}")
            self.btn_scan.setEnabled(True)
            self.timer.start()

    def toggle_play(self):
        self.player.pause = not self.player.pause

    def set_position(self, position):
        if self.player.duration:
            self.player.time_pos = (position / 1000) * self.player.duration

    def update_ui_timer(self):
        if getattr(self.player, 'time_pos', None) is not None and getattr(self.player, 'duration', None) is not None:
            pos = self.player.time_pos
            dur = self.player.duration
            if dur > 0:
                self.slider.blockSignals(True)
                self.slider.setValue(int((pos / dur) * 1000))
                self.slider.blockSignals(False)
                self.lbl_time.setText(f"{int(pos)//60:02}:{int(pos)%60:02} / {int(dur)//60:02}:{int(dur)%60:02}")

    def start_scan(self):
        self.lbl_status.setText("Running Fast Scan...")
        self.btn_scan.setEnabled(False)
        self.thread = AnalysisThread(self.filepath)
        self.thread.result_signal.connect(self.on_scan_done)
        self.thread.start()

    def on_scan_done(self, res):
        self.btn_scan.setEnabled(True)
        if not res["success"]:
            QMessageBox.critical(self, "Error", res["error"])
            return

        m = res["mattes"]
        for p in ["Top", "Bottom", "Left", "Right"]: self.inputs[p].setText(str(m[p]))
        self.lbl_status.setText("Fast Scan Complete.")

    def trigger_overlay(self):
        vals = {p: int(e.text()) if e.text().isdigit() else 0 for p, e in self.inputs.items()}
        self.overlay.update_overlay(vals, self.vid_w, self.vid_h)

if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = StudioMatteApp()
    window.show()
    sys.exit(app.exec())
