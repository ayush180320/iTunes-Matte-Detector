import sys
import subprocess
import re
import json
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QPushButton, QLabel, QFileDialog, 
                             QGroupBox, QLineEdit, QMessageBox, QStyle, QStackedLayout)
from PyQt6.QtMultimedia import QMediaPlayer
from PyQt6.QtMultimediaWidgets import QVideoWidget
from PyQt6.QtCore import QUrl, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QPainter, QColor

# --- Transparent Overlay Engine for Visualizing Mattes ---
class MatteOverlay(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        
        self.mattes = {"Top": 0, "Bottom": 0, "Left": 0, "Right": 0}
        self.vid_w = 1920
        self.vid_h = 1080

    def update_overlay(self, mattes, vid_w, vid_h):
        self.mattes = mattes
        self.vid_w = max(1, vid_w)
        self.vid_h = max(1, vid_h)
        self.update()

    def paintEvent(self, event):
        if not self.isVisible() or self.vid_w == 0:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        overlay_color = QColor(255, 0, 0, 100) 
        painter.setBrush(overlay_color)
        painter.setPen(Qt.PenStyle.NoPen)

        widget_w = self.width()
        widget_h = self.height()
        
        scale = min(widget_w / self.vid_w, widget_h / self.vid_h)
        
        drawn_w = self.vid_w * scale
        drawn_h = self.vid_h * scale
        
        offset_x = (widget_w - drawn_w) / 2
        offset_y = (widget_h - drawn_h) / 2

        top_h = self.mattes.get("Top", 0) * scale
        bottom_h = self.mattes.get("Bottom", 0) * scale
        left_w = self.mattes.get("Left", 0) * scale
        right_w = self.mattes.get("Right", 0) * scale

        if top_h > 0:
            painter.drawRect(int(offset_x), int(offset_y), int(drawn_w), int(top_h))
        if bottom_h > 0:
            painter.drawRect(int(offset_x), int(offset_y + drawn_h - bottom_h), int(drawn_w), int(bottom_h))
        if left_w > 0:
            painter.drawRect(int(offset_x), int(offset_y), int(left_w), int(drawn_h))
        if right_w > 0:
            painter.drawRect(int(offset_x + drawn_w - right_w), int(offset_y), int(right_w), int(drawn_h))

# --- Background Thread for Full Video Analysis ---
class AnalysisThread(QThread):
    result_signal = pyqtSignal(dict)
    error_signal = pyqtSignal(str)

    def __init__(self, filepath):
        super().__init__()
        self.filepath = filepath

    def run(self):
        try:
            cmd_probe = ["ffprobe", "-v", "error", "-select_streams", "v:0",
                         "-show_entries", "stream=width,height", "-of", "json", self.filepath]
            probe_result = subprocess.run(cmd_probe, stdout=subprocess.PIPE, text=True)
            info = json.loads(probe_result.stdout)
            vid_w = int(info['streams'][0]['width'])
            vid_h = int(info['streams'][0]['height'])

            cmd_ffmpeg = ["ffmpeg", "-i", self.filepath, "-vf", "fps=1,cropdetect=24:16:0", "-f", "null", "-"]
            ffmpeg_result = subprocess.run(cmd_ffmpeg, stderr=subprocess.PIPE, text=True)
            
            crop_matches = re.findall(r'crop=(\d+):(\d+):(\d+):(\d+)', ffmpeg_result.stderr)
            if not crop_matches:
                self.error_signal.emit("Could not detect mattes.")
                return

            most_common_crop = max(set(crop_matches), key=crop_matches.count)
            crop_w, crop_h, crop_x, crop_y = map(int, most_common_crop)

            mattes = {
                "Top": crop_y,
                "Bottom": vid_h - (crop_y + crop_h),
                "Left": crop_x,
                "Right": vid_w - (crop_x + crop_w),
                "Width": vid_w,
                "Height": vid_h
            }
            self.result_signal.emit(mattes)
        except Exception as e:
            self.error_signal.emit(str(e))

# --- Main Application UI ---
class MatteDetectorPro(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Pro Matte Verifier")
        self.setGeometry(100, 100, 1000, 700)

        self.filepath = ""
        self.vid_w = 1920
        self.vid_h = 1080
        self.init_ui()

    def init_ui(self):
        central_widget = QWidget(self)
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)

        # --- LEFT PANEL: Video Player + Overlay ---
        video_layout = QVBoxLayout()
        
        self.video_container = QWidget()
        self.stacked_layout = QStackedLayout(self.video_container)
        self.stacked_layout.setStackingMode(QStackedLayout.StackingMode.StackAll)

        self.video_widget = QVideoWidget()
        self.media_player = QMediaPlayer()
        self.media_player.setVideoOutput(self.video_widget)
        
        self.overlay = MatteOverlay()

        self.stacked_layout.addWidget(self.overlay) 
        self.stacked_layout.addWidget(self.video_widget) 
        
        video_layout.addWidget(self.video_container, stretch=1)

        control_layout = QHBoxLayout()
        self.btn_play = QPushButton()
        self.btn_play.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.btn_play.clicked.connect(self.toggle_playback)
        control_layout.addWidget(self.btn_play)
        
        self.lbl_status = QLabel("Ready")
        control_layout.addWidget(self.lbl_status)
        video_layout.addLayout(control_layout)

        main_layout.addLayout(video_layout, stretch=3)

        # --- RIGHT PANEL: Controls & Data ---
        side_layout = QVBoxLayout()

        self.btn_load = QPushButton("1. Load Video")
        self.btn_load.clicked.connect(self.load_video)
        side_layout.addWidget(self.btn_load)

        self.btn_scan = QPushButton("2. Auto-Detect Mattes")
        self.btn_scan.setEnabled(False)
        self.btn_scan.clicked.connect(self.start_scan)
        side_layout.addWidget(self.btn_scan)

        self.group_mattes = QGroupBox("Matte Values (Manual Override)")
        matte_layout = QVBoxLayout()
        self.inputs = {}
        
        for pos in ["Top", "Bottom", "Left", "Right"]:
            row = QHBoxLayout()
            row.addWidget(QLabel(f"{pos}:"))
            entry = QLineEdit("0")
            entry.textChanged.connect(self.trigger_overlay_update)
            row.addWidget(entry)
            self.inputs[pos] = entry
            matte_layout.addLayout(row)
            
        self.group_mattes.setLayout(matte_layout)
        side_layout.addWidget(self.group_mattes)

        self.btn_copy = QPushButton("Copy Values to Clipboard")
        self.btn_copy.clicked.connect(self.copy_values)
        side_layout.addWidget(self.btn_copy)

        side_layout.addStretch()
        main_layout.addLayout(side_layout, stretch=1)

    def load_video(self):
        file_name, _ = QFileDialog.getOpenFileName(self, "Open Video", "", "Video Files (*.mp4 *.mov *.mkv *.avi)")
        if file_name:
            self.filepath = file_name
            self.media_player.setSource(QUrl.fromLocalFile(file_name))
            
            cmd_probe = ["ffprobe", "-v", "error", "-select_streams", "v:0",
                         "-show_entries", "stream=width,height", "-of", "json", self.filepath]
            probe_result = subprocess.run(cmd_probe, stdout=subprocess.PIPE, text=True)
            info = json.loads(probe_result.stdout)
            self.vid_w = int(info['streams'][0]['width'])
            self.vid_h = int(info['streams'][0]['height'])

            self.lbl_status.setText(f"Loaded: {file_name.split('/')[-1]}")
            self.btn_scan.setEnabled(True)
            self.media_player.play()
            self.media_player.pause()

    def toggle_playback(self):
        if self.media_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.media_player.pause()
            self.btn_play.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        else:
            self.media_player.play()
            self.btn_play.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPause))

    def start_scan(self):
        self.lbl_status.setText("Scanning full video... Please wait.")
        self.btn_scan.setEnabled(False)
        self.thread = AnalysisThread(self.filepath)
        self.thread.result_signal.connect(self.on_scan_complete)
        self.thread.error_signal.connect(self.on_scan_error)
        self.thread.start()

    def on_scan_complete(self, mattes):
        self.vid_w = mattes["Width"]
        self.vid_h = mattes["Height"]
        
        self.inputs["Top"].setText(str(mattes["Top"]))
        self.inputs["Bottom"].setText(str(mattes["Bottom"]))
        self.inputs["Left"].setText(str(mattes["Left"]))
        self.inputs["Right"].setText(str(mattes["Right"]))
        
        self.lbl_status.setText(f"Scan Complete. Original Res: {self.vid_w}x{self.vid_h}")
        self.btn_scan.setEnabled(True)

    def on_scan_error(self, error_msg):
        QMessageBox.critical(self, "Error", f"Scan failed: {error_msg}")
        self.lbl_status.setText("Scan failed.")
        self.btn_scan.setEnabled(True)

    def trigger_overlay_update(self):
        current_mattes = {}
        for pos, entry in self.inputs.items():
            try:
                val = int(entry.text())
            except ValueError:
                val = 0
            current_mattes[pos] = val
        
        self.overlay.update_overlay(current_mattes, self.vid_w, self.vid_h)

    def copy_values(self):
        text = f"Top: {self.inputs['Top'].text()}\nBottom: {self.inputs['Bottom'].text()}\nLeft: {self.inputs['Left'].text()}\nRight: {self.inputs['Right'].text()}"
        QApplication.clipboard().setText(text)
        QMessageBox.information(self, "Success", "Values copied to clipboard.")

if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = MatteDetectorPro()
    window.show()
    sys.exit(app.exec())
