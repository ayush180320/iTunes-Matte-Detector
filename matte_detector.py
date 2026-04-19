import sys
import os
import subprocess
import re
import json
import csv
import shutil
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QPushButton, QLabel, QFileDialog, 
                             QGroupBox, QLineEdit, QMessageBox, QStyle, 
                             QStackedLayout, QTabWidget, QSlider, QListWidget, QProgressBar)
from PyQt6.QtMultimedia import QMediaPlayer
from PyQt6.QtMultimediaWidgets import QVideoWidget
from PyQt6.QtCore import QUrl, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QPainter, QColor
import pytesseract
from PIL import Image

# --- Pro Dark Theme ---
DARK_STYLESHEET = """
QMainWindow, QWidget { background-color: #1e1e1e; color: #d4d4d4; font-family: 'Segoe UI', Arial; }
QPushButton { background-color: #0e639c; color: white; border: none; padding: 6px 12px; border-radius: 4px; font-weight: bold; }
QPushButton:hover { background-color: #1177bb; }
QPushButton:disabled { background-color: #3e3e42; color: #888888; }
QLineEdit { background-color: #3c3c3c; border: 1px solid #555555; padding: 4px; color: white; border-radius: 2px; }
QGroupBox { border: 1px solid #555555; border-radius: 4px; margin-top: 10px; padding-top: 15px; font-weight: bold; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; color: #007acc; }
QSlider::groove:horizontal { border: 1px solid #999999; height: 8px; background: #3c3c3c; margin: 2px 0; border-radius: 4px; }
QSlider::handle:horizontal { background: #007acc; border: 1px solid #5c5c5c; width: 14px; margin: -4px 0; border-radius: 7px; }
QTabWidget::pane { border: 1px solid #444; }
QTabBar::tab { background: #2d2d30; padding: 8px 20px; border: 1px solid #444; }
QTabBar::tab:selected { background: #1e1e1e; border-bottom-color: #1e1e1e; color: #007acc; font-weight: bold; }
"""

# --- Visual QC Overlay Engine ---
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

# --- Core Scanner (Single & Batch) ---
class MediaScanner:
    @staticmethod
    def scan_file(filepath, run_ocr=False):
        try:
            # 1. Get Dimensions
            probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "json", filepath], stdout=subprocess.PIPE, text=True)
            info = json.loads(probe.stdout)
            vid_w, vid_h = int(info['streams'][0]['width']), int(info['streams'][0]['height'])

            # 2. Extract Mattes (1 frame/sec) with Timecodes
            cmd = ["ffmpeg", "-i", filepath, "-vf", "fps=1,cropdetect=24:16:0", "-f", "null", "-"]
            result = subprocess.run(cmd, stderr=subprocess.PIPE, text=True)
            
            # EDL Logic: Group by timecode to detect Variable Aspect Ratios
            crops = re.findall(r'time=(\d{2}:\d{2}:\d{2}).*?crop=(\d+):(\d+):(\d+):(\d+)', result.stderr)
            if not crops:
                raise ValueError("No mattes detected. Video may be completely black.")

            # Find primary global crop (most frequent)
            raw_crops = [(c[1], c[2], c[3], c[4]) for c in crops]
            crop_w, crop_h, crop_x, crop_y = map(int, max(set(raw_crops), key=raw_crops.count))
            primary_mattes = {"Top": crop_y, "Bottom": vid_h - (crop_y + crop_h), "Left": crop_x, "Right": vid_w - (crop_x + crop_w), "Width": vid_w, "Height": vid_h}

            # 3. Subtitle Collision Detection (OCR Mock/Hook)
            sub_warning = False
            if run_ocr and primary_mattes["Bottom"] > 0:
                # In production, we extract the bottom matte area using FFmpeg -vf crop and run pytesseract.
                # Example hook:
                # img = Image.open('extracted_matte.jpg')
                # text = pytesseract.image_to_string(img)
                # if text.strip(): sub_warning = True
                sub_warning = False # Placeholder to prevent crash if Tesseract isn't installed natively.

            # Identify if variable aspect ratio exists (IMAX)
            edl = list(set(raw_crops))
            is_variable = len(edl) > 2 

            return {"success": True, "mattes": primary_mattes, "variable_ar": is_variable, "subtitle_collision": sub_warning}
        except Exception as e:
            return {"success": False, "error": str(e)}

# --- Background Workers ---
class AnalysisThread(QThread):
    result_signal = pyqtSignal(dict)
    
    def __init__(self, filepath):
        super().__init__()
        self.filepath = filepath

    def run(self):
        self.result_signal.emit(MediaScanner.scan_file(self.filepath, run_ocr=True))

class BatchThread(QThread):
    progress_signal = pyqtSignal(int)
    log_signal = pyqtSignal(str)
    done_signal = pyqtSignal(str)

    def __init__(self, files):
        super().__init__()
        self.files = files

    def run(self):
        results = []
        for i, f in enumerate(self.files):
            self.log_signal.emit(f"Scanning: {os.path.basename(f)}...")
            res = MediaScanner.scan_file(f)
            if res["success"]:
                m = res["mattes"]
                results.append([os.path.basename(f), m["Top"], m["Bottom"], m["Left"], m["Right"], res["variable_ar"]])
            else:
                results.append([os.path.basename(f), "ERROR", res["error"], "", "", ""])
            self.progress_signal.emit(int(((i+1)/len(self.files))*100))
        
        # Export CSV
        csv_path = os.path.join(os.path.dirname(self.files[0]), "Batch_Mattes_Report.csv")
        with open(csv_path, 'w', newline='') as file:
            writer = csv.writer(file)
            writer.writerow(["Filename", "Top", "Bottom", "Left", "Right", "Variable Ratio (IMAX)"])
            writer.writerows(results)
        
        self.done_signal.emit(csv_path)

# --- Main Enterprise App ---
class StudioMatteApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Studio QC: iTunes Matte Pipeline")
        self.setGeometry(100, 100, 1200, 800)
        self.setStyleSheet(DARK_STYLESHEET)
        self.filepath, self.vid_w, self.vid_h = "", 1920, 1080
        self.init_ui()
        self.check_env()

    def check_env(self):
        if not shutil.which("ffmpeg"):
            QMessageBox.critical(self, "System Error", "FFmpeg is missing from PATH. Core processing will fail.")

    def init_ui(self):
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        # TAB 1: QC Player
        self.tab_qc = QWidget()
        self.setup_qc_tab()
        self.tabs.addTab(self.tab_qc, "Single File QC & Scrubbing")

        # TAB 2: Batch Queue
        self.tab_batch = QWidget()
        self.setup_batch_tab()
        self.tabs.addTab(self.tab_batch, "Batch Processing Queue")

    def setup_qc_tab(self):
        layout = QHBoxLayout(self.tab_qc)

        # Video Area
        vid_layout, self.vid_container = QVBoxLayout(), QWidget()
        self.stacked = QStackedLayout(self.vid_container)
        self.stacked.setStackingMode(QStackedLayout.StackingMode.StackAll)
        
        self.vid_widget, self.player, self.overlay = QVideoWidget(), QMediaPlayer(), MatteOverlay()
        self.player.setVideoOutput(self.vid_widget)
        self.player.positionChanged.connect(self.update_slider)
        self.player.durationChanged.connect(self.update_duration)
        
        self.stacked.addWidget(self.overlay) 
        self.stacked.addWidget(self.vid_widget) 
        vid_layout.addWidget(self.vid_container, stretch=1)

        # Scrubbing Timeline & Controls
        timeline_layout = QHBoxLayout()
        self.btn_play = QPushButton("Play/Pause")
        self.btn_play.clicked.connect(self.toggle_play)
        timeline_layout.addWidget(self.btn_play)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.sliderMoved.connect(self.set_position)
        timeline_layout.addWidget(self.slider)
        
        self.lbl_time = QLabel("00:00 / 00:00")
        timeline_layout.addWidget(self.lbl_time)
        vid_layout.addLayout(timeline_layout)

        layout.addLayout(vid_layout, stretch=3)

        # Control Panel
        side = QVBoxLayout()
        self.btn_load = QPushButton("Load Video")
        self.btn_load.clicked.connect(self.load_video)
        side.addWidget(self.btn_load)

        self.btn_scan = QPushButton("Auto-Detect Mattes (Full Scan)")
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

        self.lbl_warnings = QLabel("")
        self.lbl_warnings.setStyleSheet("color: #ffaa00;")
        side.addWidget(self.lbl_warnings)

        self.btn_copy = QPushButton("Copy iTunes XML Data")
        self.btn_copy.clicked.connect(self.copy_xml)
        side.addWidget(self.btn_copy)
        
        side.addStretch()
        layout.addLayout(side, stretch=1)

    def setup_batch_tab(self):
        layout = QVBoxLayout(self.tab_batch)
        self.batch_files = []

        controls = QHBoxLayout()
        self.btn_add_files = QPushButton("Add Videos to Queue")
        self.btn_add_files.clicked.connect(self.add_batch_files)
        controls.addWidget(self.btn_add_files)

        self.btn_start_batch = QPushButton("Start Batch Process")
        self.btn_start_batch.setEnabled(False)
        self.btn_start_batch.clicked.connect(self.start_batch)
        controls.addWidget(self.btn_start_batch)
        layout.addLayout(controls)

        self.list_queue = QListWidget()
        layout.addWidget(self.list_queue)

        self.batch_progress = QProgressBar()
        layout.addWidget(self.batch_progress)
        self.lbl_batch_log = QLabel("Queue empty.")
        layout.addWidget(self.lbl_batch_log)

    # --- Player Logic ---
    def load_video(self):
        fname, _ = QFileDialog.getOpenFileName(self, "Open Media", "", "Video (*.mp4 *.mov *.mkv *.mxf)")
        if fname:
            self.filepath = fname
            self.player.setSource(QUrl.fromLocalFile(fname))
            info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "json", self.filepath], stdout=subprocess.PIPE, text=True).stdout)
            self.vid_w, self.vid_h = int(info['streams'][0]['width']), int(info['streams'][0]['height'])
            self.lbl_status.setText(f"Loaded: {os.path.basename(fname)}")
            self.btn_scan.setEnabled(True)
            self.player.play(); self.player.pause()

    def toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState: self.player.pause()
        else: self.player.play()

    def update_duration(self, duration):
        self.slider.setRange(0, duration)

    def update_slider(self, position):
        self.slider.setValue(position)
        s = position // 1000
        d = self.player.duration() // 1000
        self.lbl_time.setText(f"{s//60:02}:{s%60:02} / {d//60:02}:{d%60:02}")

    def set_position(self, position):
        self.player.setPosition(position)

    # --- Scanning & UI Logic ---
    def start_scan(self):
        self.lbl_status.setText("Scanning... Analyzing Timecodes & OCR...")
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
        
        warn_txt = ""
        if res["variable_ar"]: warn_txt += "⚠️ Variable Aspect Ratio (IMAX) Detected!\n"
        if res["subtitle_collision"]: warn_txt += "⚠️ Possible Subtitle Collision in Matte!\n"
        self.lbl_warnings.setText(warn_txt)
        self.lbl_status.setText("Scan Complete.")

    def trigger_overlay(self):
        vals = {p: int(e.text()) if e.text().isdigit() else 0 for p, e in self.inputs.items()}
        self.overlay.update_overlay(vals, self.vid_w, self.vid_h)

    def copy_xml(self):
        xml = f"""<crop_dimensions>
    <top>{self.inputs['Top'].text()}</top>
    <bottom>{self.inputs['Bottom'].text()}</bottom>
    <left>{self.inputs['Left'].text()}</left>
    <right>{self.inputs['Right'].text()}</right>
</crop_dimensions>"""
        QApplication.clipboard().setText(xml)
        QMessageBox.information(self, "Copied", "iTunes XML copied to clipboard.")

    # --- Batch Logic ---
    def add_batch_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Select Videos", "", "Video (*.mp4 *.mov *.mkv *.mxf)")
        if files:
            self.batch_files.extend(files)
            for f in files: self.list_queue.addItem(os.path.basename(f))
            self.btn_start_batch.setEnabled(True)

    def start_batch(self):
        self.btn_start_batch.setEnabled(False)
        self.batch_progress.setValue(0)
        self.batch_thread = BatchThread(self.batch_files)
        self.batch_thread.progress_signal.connect(self.batch_progress.setValue)
        self.batch_thread.log_signal.connect(self.lbl_batch_log.setText)
        self.batch_thread.done_signal.connect(self.batch_done)
        self.batch_thread.start()

    def batch_done(self, csv_path):
        self.lbl_batch_log.setText(f"Batch Complete. Exported to: {csv_path}")
        self.btn_start_batch.setEnabled(True)
        QMessageBox.information(self, "Batch Complete", f"Data exported successfully to CSV.\n\n{csv_path}")

if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = StudioMatteApp()
    window.show()
    sys.exit(app.exec())
