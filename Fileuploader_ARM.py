#!/usr/bin/env python3
# fileuploader_arm64.py
"""
ARM64-aware rewrite of the Fileuploader application.
Key changes:
 - Detects ARM64 and logs it.
 - More robust venv bootstrap (simpler & tolerant).
 - PyQt5 fallback to PySide6 (auto-select whichever is available).
 - If external ServerFileuploader isn't present, run the internal Flask `app`.
 - Offloads image downloads from UI thread.
"""

import os
import sys
import platform
import threading
import sqlite3
import mimetypes
import shutil
import requests
from threading import Thread
from typing import List

# -------------------- Architecture notice -------------------- #
MACHINE = platform.machine().lower()
IS_ARM64 = MACHINE in ("aarch64", "arm64")
if IS_ARM64:
    print("Notice: running on ARM64 (%s). The code will attempt runtime-friendly fallbacks." % MACHINE)
else:
    print("Platform: %s" % MACHINE)

# -------------------- Optional venv bootstrap (simple) -------------------- #
# This is intentionally conservative: it won't re-exec aggressively on failure.
def ensure_venv_simple():
    """
    Create a per-user venv if missing, and try to install minimal runtime packages.
    Conservative: errors are ignored to avoid breaking execution.
    """
    try:
        import venv
        import subprocess

        home = os.path.expanduser("~")
        venv_dir = os.path.join(home, ".fileuploader_venv")
        if os.name == "nt":
            venv_python = os.path.join(venv_dir, "Scripts", "python.exe")
        else:
            venv_python = os.path.join(venv_dir, "bin", "python")

        required = ["Flask", "requests", "packaging"]

        if not os.path.exists(venv_python):
            try:
                venv.create(venv_dir, with_pip=True, clear=False)
            except Exception:
                return
            # try to upgrade pip and install minimal packages
            try:
                subprocess.run([venv_python, "-m", "pip", "install", "--disable-pip-version-check", "-U", "pip", "setuptools", "wheel"], check=False)
                subprocess.run([venv_python, "-m", "pip", "install", "--disable-pip-version-check"] + required, check=False)
            except Exception:
                pass
        # do not force reexec in this conservative bootstrap
    except Exception:
        pass

if os.environ.get("FILEUPLOADER_SKIP_BOOTSTRAP") != "1":
    ensure_venv_simple()

# -------------------- Flask app (shared) -------------------- #
from flask import Flask, request, jsonify, send_from_directory, abort
from werkzeug.utils import secure_filename
import io
import zipfile
from packaging.version import Version  # packaging is lightweight and helpful

app = Flask(__name__)

UPLOAD_FOLDER_HTTP = os.path.join(os.path.dirname(__file__), "uploaded_folders")
os.makedirs(UPLOAD_FOLDER_HTTP, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER_HTTP

# Minimal in-memory "API" for demonstration - this matches your client expectations.
# In production you'd replace with a real persistent API.
_db_files = {}   # id -> dict
_next_file_id = 1
_db_lock = threading.Lock()


def _store_file_bytes(filename: str, content_type: str, data: bytes):
    global _next_file_id
    with _db_lock:
        fid = _next_file_id
        _next_file_id += 1
        _db_files[fid] = {
            "id": fid,
            "original_filename": filename,
            "content_type": content_type,
            "data": data,
        }
    # Also optionally save to UPLOAD_FOLDER_HTTP for inspection
    try:
        dest = os.path.join(app.config["UPLOAD_FOLDER"], secure_filename(filename))
        with open(dest, "wb") as fh:
            fh.write(data)
    except Exception:
        pass
    return fid


@app.route("/api/upload", methods=["POST"])
def api_upload():
    files = request.files.getlist("files")
    saved = 0
    for f in files:
        if not f or f.filename == "":
            continue
        data = f.read()
        content_type = f.content_type or "application/octet-stream"
        _store_file_bytes(f.filename, content_type, data)
        saved += 1
    return jsonify(saved=saved), 201


@app.route("/api/files", methods=["GET"])
def api_files():
    with _db_lock:
        files = [dict(id=v["id"], original_filename=v["original_filename"], content_type=v["content_type"]) for v in _db_files.values()]
    return jsonify(files=files)


@app.route("/files/<int:file_id>/download", methods=["GET"])
def api_download(file_id: int):
    item = _db_files.get(file_id)
    if not item:
        abort(404)
    return (item["data"], 200, {
        "Content-Type": item.get("content_type", "application/octet-stream"),
        "Content-Disposition": f'attachment; filename="{item["original_filename"]}"'
    })


@app.route("/files/<int:file_id>/delete", methods=["POST"])
def api_delete(file_id: int):
    if file_id in _db_files:
        with _db_lock:
            _db_files.pop(file_id, None)
        return jsonify(success=True)
    return jsonify(success=False), 404

# -------------------- GUI toolkit selection (PyQt5 / PySide6) -------------------- #
# Try PyQt5 first (to preserve your existing code). If it's not available, try PySide6.
qt_backend = None
QT_VERSION_STR = "0.0.0"
try:
    from PyQt5.QtWidgets import QApplication  # quick test import
    import PyQt5
    from PyQt5.QtCore import QT_VERSION_STR as QT_VERSION_STR_PYQT
    QT_VERSION_STR = getattr(PyQt5.QtCore, "QT_VERSION_STR", QT_VERSION_STR_PYQT)
    qt_backend = "PyQt5"
except Exception:
    try:
        from PySide6.QtWidgets import QApplication
        import PySide6
        QT_VERSION_STR = getattr(PySide6.QtCore, "QT_VERSION_STR", QT_VERSION_STR)
        # For PySide6, names are slightly different for enums; we'll adapt at usage time.
        qt_backend = "PySide6"
    except Exception:
        qt_backend = None

if qt_backend is None:
    print("Error: neither PyQt5 nor PySide6 is available. Install one with pip (PySide6 often installs on ARM easily).")
    # We keep running since user might only want the server parts.
else:
    print(f"Using Qt backend: {qt_backend}, QT_VERSION_STR={QT_VERSION_STR}")

# -------------------- Qt imports (guarded) -------------------- #
if qt_backend is not None:
    if qt_backend == "PyQt5":
        from PyQt5.QtWidgets import (
            QMenuBar, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
            QGridLayout, QFrame, QPushButton, QFileDialog, QListWidget,
            QListWidgetItem, QLabel, QMessageBox, QAbstractItemView, QCheckBox, QSpinBox, QStyle,
            QAction, QSizePolicy
        )
        from PyQt5.QtCore import Qt, QEvent
        from PyQt5.QtGui import QDragEnterEvent, QDropEvent, QIcon, QPixmap, QPalette, QColor
    else:  # PySide6
        from PySide6.QtWidgets import (
            QMenuBar, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
            QGridLayout, QFrame, QPushButton, QFileDialog, QListWidget,
            QListWidgetItem, QLabel, QMessageBox, QAbstractItemView, QCheckBox, QSpinBox, QStyle,
            QAction, QSizePolicy
        )
        from PySide6.QtCore import Qt, QEvent
        from PySide6.QtGui import QDragEnterEvent, QDropEvent, QIcon, QPixmap, QPalette, QColor

    # local shorthand for style enums that changed between PyQt5/PySide6
    try:
        SP_BrowserReload = QStyle.StandardPixmap.SP_BrowserReload
        SP_DialogOpenButton = QStyle.StandardPixmap.SP_DialogOpenButton
        SP_TrashIcon = QStyle.StandardPixmap.SP_TrashIcon
        SP_DialogSaveButton = QStyle.StandardPixmap.SP_DialogSaveButton
        SP_FileIcon = QStyle.StandardPixmap.SP_FileIcon
    except Exception:
        # Some bindings expose enums differently; fallback numeric constants are risky,
        # but we'll try to fetch from QStyle instance when used.
        SP_BrowserReload = None
        SP_DialogOpenButton = None
        SP_TrashIcon = None
        SP_DialogSaveButton = None
        SP_FileIcon = None

    # -------------------- Helper: threaded image loader -------------------- #
    def load_pixmap_from_url_async(url, callback):
        """
        Download image data in a background thread and call `callback(pixmap_or_None)` on the UI thread.
        We keep it simple: callback will be executed via QApplication.postEvent using a small wrapper.
        """
        def worker():
            try:
                r = requests.get(url, timeout=10)
                if r.status_code == 200:
                    pix = QPixmap()
                    if pix.loadFromData(r.content):
                        QApplication.instance().postEvent(QApplication.instance(), _CallableEvent(lambda: callback(pix)))
                        return
            except Exception:
                pass
            QApplication.instance().postEvent(QApplication.instance(), _CallableEvent(lambda: callback(None)))

        Thread(target=worker, daemon=True).start()

    # -------------------- Small event wrapper to run lambdas in UI thread -------------------- #
    class _CallableEvent(QEvent):
        def __init__(self, fn):
            super().__init__(QEvent.Type.User)
            self.fn = fn
        def execute(self):
            try:
                self.fn()
            except Exception:
                pass

    # -------------------- UI implementations (adapted from original) -------------------- #
    class AutorWindow(QWidget):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setWindowTitle("Autor")
            self.setMinimumSize(400, 350)
            layout = QVBoxLayout(self)

            image_url = "https://images.unsplash.com/photo-1506744038136-46273834b3fb?auto=format&fit=crop&w=400&q=80"
            image_label = QLabel()
            image_label.setAlignment(Qt.AlignmentFlag.AlignCenter if hasattr(Qt, "AlignmentFlag") else Qt.AlignCenter)
            layout.addWidget(image_label)

            def on_loaded(pix):
                if pix:
                    scaled = pix.scaledToWidth(320, Qt.TransformationMode.SmoothTransformation if hasattr(Qt, "TransformationMode") else Qt.SmoothTransformation)
                    image_label.setPixmap(scaled)
                else:
                    image_label.setText("[Bild konnte nicht geladen werden]")

            # async load
            load_pixmap_from_url_async(image_url, on_loaded)

            label = QLabel(
                "<h2>Max Krebs</h2>"
                "<p><b>E-Mail:</b> melvis@posteo.de</p>"
                "<p>© Release 25.06.2025</p>"
                "<p>Mit Liebe gecodet durch Max Krebs</p>"
            )
            label.setAlignment(Qt.AlignmentFlag.AlignCenter if hasattr(Qt, "AlignmentFlag") else Qt.AlignCenter)
            layout.addWidget(label)
            self.setLayout(layout)

    class DragDropWidget(QFrame):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setAcceptDrops(True)
            self.setFixedHeight(150)
            self.on_files_dropped = None
            self.setStyleSheet(
                """
                QFrame {
                    border: 2px solid #a3a3a3;
                    border-radius: 12px;
                    background-color: #fafafa;
                    padding:10px;
                }
                QLabel {
                    color: #6b7280;
                    font-size: 18px;
                    font-weight: 600;
                }
            """
            )
            self._layout = QVBoxLayout(self)
            label = QLabel("Dateien für Drag & Drop hier ablegen")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter if hasattr(Qt, "AlignmentFlag") else Qt.AlignCenter)
            self._layout.addWidget(label)

        def dragEnterEvent(self, a0: QDragEnterEvent):
            md = a0.mimeData()
            if md is not None and md.hasUrls():
                a0.acceptProposedAction()
            else:
                a0.ignore()

        def dragMoveEvent(self, a0: QDragEnterEvent):
            md = a0.mimeData()
            if md is not None and md.hasUrls():
                a0.acceptProposedAction()
            else:
                a0.ignore()

        def dropEvent(self, a0: QDropEvent):
            md = a0.mimeData()
            if md is not None and md.hasUrls():
                local_files = [
                    url.toLocalFile() for url in md.urls() if url.isLocalFile()
                ]
                if callable(self.on_files_dropped):
                    self.on_files_dropped(local_files)
                a0.acceptProposedAction()
            else:
                a0.ignore()

    class FileListWidget(QWidget):
        def __init__(self, parent=None):
            super().__init__(parent)
            main_layout = QVBoxLayout(self)
            frame = QFrame(self)
            frame.setFrameShape(QFrame.StyledPanel)
            frame.setFrameShadow(QFrame.Raised)
            frame.setAutoFillBackground(True)
            frame_pal = frame.palette()
            frame_pal.setColor(QPalette.Window, QColor(255, 255, 255))
            frame.setPalette(frame_pal)
            frame_layout = QVBoxLayout(frame)
            frame_layout.setContentsMargins(20, 20, 20, 20)
            frame_layout.setSpacing(20)
            main_layout.setContentsMargins(0, 0, 0, 0)
            main_layout.addWidget(frame)
            self.btn_refresh = QPushButton()
            style = QApplication.style() if hasattr(QApplication, 'style') else None
            if style and SP_BrowserReload is not None:
                try:
                    self.btn_refresh.setIcon(style.standardIcon(SP_BrowserReload))
                except Exception:
                    pass
                self.btn_refresh.setToolTip("Dateiliste aktualisieren")
            header_label = QLabel("Gespeicherte Dateien")
            header_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            header_label.setStyleSheet(
                """
                QLabel {
                    padding: 6px 12px;
                    color: white;
                    background-color: grey;
                    border: 1px solid black;
                    border-radius:15px;
                    font-weight: 600;
                }
                """
            )
            header_layout = QHBoxLayout()
            header_layout.setContentsMargins(0, 0, 0, 0)
            header_layout.addWidget(header_label)
            header_layout.addStretch()
            header_layout.addWidget(self.btn_refresh)
            frame_layout.addLayout(header_layout)
            self.list_widget = QListWidget()
            self.list_widget.setSelectionMode(QAbstractItemView.SingleSelection)
            self.list_widget.setStyleSheet(
                """
                QListWidget {
                    border-radius: 15px;
                    font-size: 14px;
                    color: #374151;
                    background-color: #ffffff;
                    margin: 2px 4px;
                    padding: 8px 12px;
                }
                QListWidget::item {
                    padding: 8px 12px;
                    border-radius: 8px;
                    margin: 2px 4px;
                }
                QListWidget::item:selected {
                    background-color: green;
                }
            """
            )
            frame_layout.addWidget(self.list_widget)
            btn_layout = QHBoxLayout()
            self.btn_add = QPushButton()
            if style and SP_DialogOpenButton is not None:
                try:
                    self.btn_add.setIcon(style.standardIcon(SP_DialogOpenButton))
                except Exception:
                    pass
            self.btn_add.setToolTip("Dateien hinzufügen")
            self.btn_delete = QPushButton()
            if style and SP_TrashIcon is not None:
                try:
                    self.btn_delete.setIcon(style.standardIcon(SP_TrashIcon))
                except Exception:
                    pass
            self.btn_delete.setToolTip("Ausgewählte Datei löschen")
            self.btn_download = QPushButton()
            if style and SP_DialogSaveButton is not None:
                try:
                    self.btn_download.setIcon(style.standardIcon(SP_DialogSaveButton))
                except Exception:
                    pass
            self.btn_download.setToolTip("Ausgewählte Datei herunterladen")
            btn_layout.addWidget(self.btn_add)
            btn_layout.addWidget(self.btn_delete)
            btn_layout.addWidget(self.btn_download)
            btn_layout.addStretch()
            frame_layout.addLayout(btn_layout)

        def clear_list(self):
            self.list_widget.clear()

        def add_file_item(self, file_id: int, filename: str, filetype: str):
            item = QListWidgetItem()
            item.setText(f"{filename} ({filetype})")
            item.setData(Qt.ItemDataRole.UserRole, file_id)
            self.list_widget.addItem(item)

        def selected_file_id(self):
            item = self.list_widget.currentItem()
            return item.data(Qt.ItemDataRole.UserRole) if item else None

        @staticmethod
        def _icon_for_type(mime: str) -> QIcon:
            style = QApplication.style() if hasattr(QApplication, 'style') else None
            if mime.startswith("image/"):
                try:
                    return QIcon.fromTheme("image-x-generic") or (style.standardIcon(SP_FileIcon) if style and SP_FileIcon is not None else QIcon())
                except Exception:
                    return QIcon()
            if "pdf" in mime:
                try:
                    return QIcon.fromTheme("application-pdf") or (style.standardIcon(SP_FileIcon) if style and SP_FileIcon is not None else QIcon())
                except Exception:
                    return QIcon()
            if "zip" in mime or "compressed" in mime:
                try:
                    return QIcon.fromTheme("package-x-generic") or (style.standardIcon(SP_FileIcon) if style and SP_FileIcon is not None else QIcon())
                except Exception:
                    return QIcon()
            if mime.startswith("text/") or mime in (
                "application/msword",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ):
                try:
                    return QIcon.fromTheme("x-office-document") or (style.standardIcon(SP_FileIcon) if style and SP_FileIcon is not None else QIcon())
                except Exception:
                    return QIcon()
            return QIcon()

    class ButtonUploadtoServer(QPushButton):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setText("Upload to Server")
            self.setFixedHeight(48)
            self.setStyleSheet(
                """
                QPushButton {
                    background-color: #1f2937;
                    color: white;
                    border-radius: 12px;
                    font-weight: 600;
                    font-size: 16px;
                    padding: 12px 20px;
                }
                QPushButton:hover { background-color: #4b5563; }
                QPushButton:pressed { background-color: #111827; }
            """
            )

    # -------------------- MainWindow -------------------- #
    DB_NAME = "file_manager.db"
    SERVER_BASE = "http://127.0.0.1:5000"
    UPLOAD_ENDPOINT = f"{SERVER_BASE}/api/upload"
    FILES_ENDPOINT = f"{SERVER_BASE}/api/files"
    DOWNLOAD_ENDPOINT_TEMPLATE = f"{SERVER_BASE}/files/{{id}}/download"
    DELETE_ENDPOINT_TEMPLATE = f"{SERVER_BASE}/files/{{id}}/delete"

    class MainWindow(QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("Fileuploader")
            # Try to set a window icon if present next to the script
            try:
                self.setWindowIcon(QIcon(os.path.join(os.path.dirname(__file__), "icon1.png")))
            except Exception:
                pass
            self.setMinimumSize(800, 600)
            self.current_db_path = DB_NAME
            self.settings_window = None
            self.server_timeout = 30

            self._create_menu()

            # DB connection
            self.conn = sqlite3.connect(self.current_db_path)
            self._init_db()

            # Main layout
            self._setup_ui()

            # Init list
            self.load_files()

        def show_settings_window(self) -> None:
            if not hasattr(self, 'settings_window') or self.settings_window is None or not self.settings_window.isVisible():
                self.settings_window = SettingsWindow(self)
                self.settings_window.show()
            self.settings_window.raise_()
            self.settings_window.activateWindow()

        def upload_to_server(self, files):
            sent_files = []
            file_handles = []
            try:
                for path in files:
                    if not os.path.isfile(path):
                        continue
                    mime, _ = mimetypes.guess_type(path)
                    mime = mime or "application/octet-stream"
                    fh = open(path, "rb")
                    file_handles.append(fh)
                    sent_files.append(("files", (os.path.basename(path), fh, mime)))

                if not sent_files:
                    self.run_on_ui_thread(lambda: QMessageBox.warning(self, "Upload", "No valid files to upload."))
                    return

                resp = requests.post(UPLOAD_ENDPOINT, files=sent_files, timeout=self.server_timeout)
                resp.raise_for_status()
                try:
                    data = resp.json()
                    saved = data.get("saved", 0)
                except Exception:
                    saved = 0

                self.run_on_ui_thread(
                    lambda: (
                        QMessageBox.information(self, "Upload Complete", f"Successfully uploaded {saved or len(sent_files)} file(s) to server."),
                        self.load_files()
                    )
                )
            except requests.RequestException as exc:
                self.run_on_ui_thread(
                    lambda: QMessageBox.warning(self, "Upload Failed", f"Server error during upload:\n{exc}")
                )
            finally:
                for fh in file_handles:
                    try:
                        fh.close()
                    except Exception:
                        pass

        def run_on_ui_thread(self, fn):
            QApplication.instance().postEvent(self, _CallableEvent(fn))

        def _setup_ui(self):
            self.central_widget = QWidget(self)
            self.setCentralWidget(self.central_widget)

            grid = QGridLayout(self.central_widget)
            grid.setContentsMargins(24, 24, 24, 24)
            grid.setSpacing(24)

            left_vbox = QVBoxLayout()
            self.btn_upload = QPushButton("Dateien hochladen")
            self.btn_upload.setFixedHeight(48)
            self.btn_upload.setStyleSheet(self._button_style())
            self.btn_upload.setToolTip("Dateien auswählen")

            self.btn_download_all = QPushButton("Dateien herunterladen")
            self.btn_download_all.setFixedHeight(48)
            self.btn_download_all.setStyleSheet(self._button_style())
            self.btn_download_all.setToolTip("Dateien auswählen")

            self.drag_drop = DragDropWidget()
            def on_files_dropped(files):
                self.handle_files_upload(files)
            self.drag_drop.on_files_dropped = on_files_dropped
            left_vbox.addWidget(self.btn_upload)
            left_vbox.addWidget(self.btn_download_all)
            left_vbox.addWidget(self.drag_drop)
            left_vbox.addStretch()

            left_container = QWidget()
            left_container.setLayout(left_vbox)

            self.file_widget = FileListWidget()

            grid.addWidget(left_container, 0, 0)
            grid.addWidget(self.file_widget, 0, 1)
            grid.setColumnStretch(0, 1)
            grid.setColumnStretch(1, 2)

            self.btn_upload.clicked.connect(self.open_file_dialog)
            self.btn_download_all.clicked.connect(self.download_selected_file)

            self.file_widget.btn_add.clicked.connect(self.open_file_dialog)
            self.file_widget.btn_delete.clicked.connect(self.delete_selected_file)
            self.file_widget.btn_download.clicked.connect(self.download_selected_file)
            self.file_widget.btn_refresh.clicked.connect(self.load_files)

        def _create_menu(self):
            menubar = self.menuBar()
            if menubar is None:
                menubar = QMenuBar(self)
                self.setMenuBar(menubar)

            file_menu = menubar.addMenu("&Datei")
            act_open = QAction("Öffnen…", self)
            act_open.setShortcut("Ctrl+O")
            file_menu.addAction(act_open)
            file_menu.addSeparator()
            act_db_location = QAction("Datenbank Speicherort ändern", self)
            act_db_location.triggered.connect(self.change_database_location)
            file_menu.addAction(act_db_location)
            act_db_export = QAction("Datenbank exportieren", self)
            act_db_export.triggered.connect(self.database_download)
            file_menu.addAction(act_db_export)
            act_db_import = QAction("Datenbank importieren", self)
            act_db_import.triggered.connect(self.database_upload)
            file_menu.addAction(act_db_import)
            file_menu.addSeparator()
            act_exit = QAction("Beenden", self)
            act_exit.setShortcut("Ctrl+Q")
            act_exit.triggered.connect(self.close)
            file_menu.addAction(act_exit)

            edit_menu = menubar.addMenu("&Bearbeiten")
            act_undo = QAction("Rückgängig", self)
            act_undo.setShortcut("Ctrl+Z")
            edit_menu.addAction(act_undo)
            act_redo = QAction("Wiederholen", self)
            act_redo.setShortcut("Ctrl+Y")
            edit_menu.addAction(act_redo)
            act_edit_menu = QAction("Einstellungen", self)
            act_edit_menu.setShortcut("Ctrl+I")
            act_edit_menu.triggered.connect(self.show_settings_window)
            edit_menu.addAction(act_edit_menu)

            help_menu = menubar.addMenu("&Hilfe")
            act_about = QAction("Über", self)
            act_autor = QAction("Autor", self)
            act_autor.triggered.connect(self.show_autor)
            act_about.triggered.connect(self.show_about_dialog)
            help_menu.addAction(act_about)
            help_menu.addAction(act_autor)

        def show_autor(self):
            msg = QMessageBox(self)
            msg.setWindowTitle("Autor")
            image_url = "https://images.unsplash.com/photo-1506744038136-46273834b3fb?auto=format&fit=crop&w=200&q=60"

            def on_loaded(pix):
                if pix:
                    scaled = pix.scaledToWidth(100, Qt.TransformationMode.SmoothTransformation if hasattr(Qt, "TransformationMode") else Qt.SmoothTransformation)
                    msg.setIconPixmap(scaled)
                msg.setText(
                    "<b>Max Krebs</b><br>"
                    "E-Mail: max.krebs@example.com<br>"
                    "© Release 25.06.2024<br>"
                    "Mit Liebe gecodet durch Max Krebs"
                )
                msg.exec_()

            load_pixmap_from_url_async(image_url, on_loaded)

        def show_about_dialog(self):
            QMessageBox.about(
                self,
                "Über Fileuploader",
                "Fileuploader v1.0\n\nEin einfacher Drag-&-Drop Datei-Uploader\n© Release 25.06.2024 \n\nMit Liebe gecodet durch Max Krebs\n",
            )

        @staticmethod
        def _button_style() -> str:
            return """
                QPushButton {
                    background-color: #1f2937;
                    color: white;
                    border-radius: 12px;
                    font-weight: 600;
                    font-size: 16px;
                    padding: 12px 20px;
                }
                QPushButton:hover { background-color: #4b5563; }
                QPushButton:pressed { background-color: #111827; }
            """

        def _init_db(self):
            cur = self.conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS files(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT NOT NULL,
                    filetype TEXT NOT NULL,
                    data BLOB NOT NULL
                )
            """
            )
            self.conn.commit()

        def open_file_dialog(self):
            files, _ = QFileDialog.getOpenFileNames(
                self,
                "Auswahl der Dateien",
                "",
                "Alle unterstützten Dateien (*.webp *.avif *.png *.heic *.jpg *.jpeg *.bmp *.pdf *.doc *.docx *.odt *.odp *.txt *.zip *.7z);;"
                "Bilder (*.webp *.avif *.png *.heic *.jpg *.jpeg *.bmp);;"
                "PDF (*.pdf);;"
                "Dokumente (*.doc *.docx *.odt *.odp *.txt);;"
                "Zip Archive (*.zip *.7z)"
            )
            if files:
                self.handle_files_upload(files)

        def handle_files_upload(self, files):
            allowed = {".doc", ".docx", ".odt", ".odp", ".txt", ".pdf", ".zip", ".7z", ".png", ".jpg", ".jpeg", ".bmp", ".heic", ".webp", ".avif"}
            valid = [f for f in files if os.path.splitext(f)[1].lower() in allowed]

            if not valid:
                QMessageBox.warning(self, "Unsupported Files", "No supported file types selected.")
                return

            Thread(target=self.upload_to_server, args=(valid,), daemon=True).start()

        def load_files(self):
            self.file_widget.clear_list()
            try:
                resp = requests.get(FILES_ENDPOINT, timeout=self.server_timeout)
                resp.raise_for_status()
                data = resp.json()
                for f in data.get("files", []):
                    self.file_widget.add_file_item(f["id"], f["original_filename"], f.get("content_type") or "application/octet-stream")
            except requests.RequestException as exc:
                QMessageBox.warning(self, "Server Error", f"Could not fetch file list from server.\n{exc}")

        def delete_selected_file(self):
            file_id = self.file_widget.selected_file_id()
            if not file_id:
                QMessageBox.information(self, "Keine Auswahl", "Bitte Datei auswählen.")
                return
            if (
                QMessageBox.question(
                    self,
                    "Löschen bestätigen",
                    "Wollen Sie die ausgewählte Datei wirklich löschen?",
                    QMessageBox.Yes | QMessageBox.No,
                )
                == QMessageBox.Yes
            ):
                try:
                    resp = requests.post(DELETE_ENDPOINT_TEMPLATE.format(id=file_id), timeout=self.server_timeout)
                    resp.raise_for_status()
                    self.load_files()
                except requests.RequestException as exc:
                    QMessageBox.warning(self, "Fehler", f"Server-Fehler beim Löschen:\n{exc}")

        def download_selected_file(self):
            file_id = self.file_widget.selected_file_id()
            if not file_id:
                QMessageBox.information(self, "Keine Auswahl", "Bitte Datei auswählen.")
                return

            default_name = "downloaded_file"
            try:
                resp_list = requests.get(FILES_ENDPOINT, timeout=self.server_timeout)
                resp_list.raise_for_status()
                files = {f["id"]: f for f in resp_list.json().get("files", [])}
                info = files.get(file_id)
                if info and info.get("original_filename"):
                    default_name = info["original_filename"]
            except Exception:
                pass

            save_path, _ = QFileDialog.getSaveFileName(self, "Speichern unter …", default_name, "All Files (*)")
            if not save_path:
                return

            try:
                with requests.get(DOWNLOAD_ENDPOINT_TEMPLATE.format(id=file_id), stream=True, timeout=self.server_timeout) as r:
                    r.raise_for_status()
                    with open(save_path, "wb") as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                QMessageBox.information(self, "Erfolg", f"Datei gespeichert: {save_path}")
            except requests.RequestException as exc:
                QMessageBox.warning(self, "Error", f"Download fehlgeschlagen.\n{exc}")

        def change_database_location(self):
            new_path, _ = QFileDialog.getSaveFileName(
                self,
                "Neuen Datenbank-Speicherort wählen",
                self.current_db_path,
                "SQLite Database (*.db);;All Files (*)"
            )

            if not new_path:
                return

            try:
                self.conn.close()
                if os.path.exists(self.current_db_path):
                    shutil.copy2(self.current_db_path, new_path)
                self.current_db_path = new_path
                self.conn = sqlite3.connect(self.current_db_path)
                self._init_db()
                self.load_files()
                QMessageBox.information(self, "Erfolg", f"Datenbank-Speicherort geändert zu:\n{new_path}")
            except Exception as exc:
                QMessageBox.warning(self, "Fehler", f"Fehler beim Ändern des Datenbank-Speicherorts:\n{exc}")
                self.conn = sqlite3.connect(self.current_db_path)

        def database_download(self):
            if not os.path.exists(self.current_db_path):
                QMessageBox.warning(self, "Fehler", "Keine Datenbank zum Exportieren gefunden.")
                return

            export_path, _ = QFileDialog.getSaveFileName(
                self,
                "Datenbank exportieren",
                f"file_manager_backup_{os.path.basename(self.current_db_path)}",
                "SQLite Database (*.db);;All Files (*)"
            )

            if not export_path:
                return

            try:
                self.conn.commit()
                shutil.copy2(self.current_db_path, export_path)
                QMessageBox.information(self, "Erfolg", f"Datenbank erfolgreich exportiert nach:\n{export_path}")
            except Exception as exc:
                QMessageBox.warning(self, "Fehler", f"Fehler beim Exportieren der Datenbank:\n{exc}")

        def database_upload(self):
            import_path, _ = QFileDialog.getOpenFileName(
                self,
                "Datenbank importieren",
                "",
                "SQLite Database (*.db);;All Files (*)"
            )

            if not import_path:
                return

            reply = QMessageBox.question(
                self,
                "Import bestätigen",
                "Das Importieren einer Datenbank wird die aktuelle Datenbank ersetzen.\n"
                "Möchten Sie fortfahren?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )

            if reply != QMessageBox.Yes:
                return

            try:
                test_conn = sqlite3.connect(import_path)
                test_cursor = test_conn.cursor()
                test_cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='files'")
                if not test_cursor.fetchone():
                    test_conn.close()
                    QMessageBox.warning(self, "Ungültige Datenbank", "Die ausgewählte Datei scheint keine gültige Fileuploader-Datenbank zu sein.")
                    return
                test_conn.close()
                self.conn.close()
                shutil.copy2(import_path, self.current_db_path)
                self.conn = sqlite3.connect(self.current_db_path)
                self._init_db()
                self.load_files()
                QMessageBox.information(self, "Erfolg", f"Datenbank erfolgreich importiert von:\n{import_path}")
            except Exception as exc:
                QMessageBox.warning(self, "Fehler", f"Fehler beim Importieren der Datenbank:\n{exc}")
                try:
                    self.conn = sqlite3.connect(self.current_db_path)
                except Exception as e:
                    QMessageBox.warning(self, "Fehler", f"Fehler beim Wiederherstellen der Verbindung zur Original-Datenbank:\n{e}")

        def closeEvent(self, a0):
            try:
                self.conn.close()
            except Exception:
                pass
            super().closeEvent(a0)

        def event(self, event):
            if isinstance(event, _CallableEvent):
                event.execute()
                return True
            return super().event(event)

    class SettingsWindow(QWidget):
        @staticmethod
        def _is_dark(app: QApplication) -> bool:
            return app.palette().color(QPalette.Window).value() < 100

        @staticmethod
        def apply_dark_palette(enable: bool) -> None:
            app = QApplication.instance()
            if app is None:
                return
            qt6 = Version(QT_VERSION_STR).major >= 6 if QT_VERSION_STR and QT_VERSION_STR[0].isdigit() else False

            def pal_role(key: str):
                if qt6:
                    return getattr(QPalette.ColorRole, key)
                return getattr(QPalette, key)

            def gc_color(key: str):
                if qt6:
                    return getattr(Qt.GlobalColor, key)
                return getattr(Qt, key)

            if enable:
                dark = QPalette()
                dark.setColor(pal_role("Window"), QColor(53, 53, 53))
                dark.setColor(pal_role("WindowText"), gc_color("white"))
                dark.setColor(pal_role("Base"), QColor(35, 35, 35))
                dark.setColor(pal_role("AlternateBase"), QColor(53, 53, 53))
                dark.setColor(pal_role("ToolTipBase"), gc_color("white"))
                dark.setColor(pal_role("ToolTipText"), gc_color("white"))
                dark.setColor(pal_role("Text"), gc_color("white"))
                dark.setColor(pal_role("Button"), QColor(53, 53, 53))
                dark.setColor(pal_role("ButtonText"), gc_color("white"))
                dark.setColor(pal_role("BrightText"), gc_color("red"))
                dark.setColor(pal_role("Link"), QColor(42, 130, 218))
                dark.setColor(pal_role("Highlight"), QColor(42, 130, 218))
                dark.setColor(pal_role("HighlightedText"), gc_color("black"))
                app.setPalette(dark)
                app.setStyleSheet("QToolTip { color: #ffffff; background-color: #2a82da; border: 0px; }")
            else:
                app.setPalette(app.style().standardPalette())
                app.setStyleSheet("")

        def __init__(self, parent=None):
            super().__init__(parent)
            self.setWindowFlags(
                Qt.Window
                | Qt.WindowTitleHint
                | Qt.WindowCloseButtonHint
                | Qt.WindowMinimizeButtonHint
            )
            self.setWindowTitle("Einstellungen")
            self.setMinimumSize(600, 600)
            self.setMaximumSize(800, 800)
            self.setAutoFillBackground(True)
            pal = self.palette()
            pal.setColor(QPalette.Window, QColor(245, 245, 245))
            self.setPalette(pal)
            main_layout = QVBoxLayout(self)
            self.chk_darkmode = QCheckBox("Dark-Mode aktivieren")
            self.chk_darkmode.setChecked(SettingsWindow._is_dark(QApplication.instance()))
            self.spn_timeout = QSpinBox()
            self.spn_timeout.setRange(5, 300)
            self.spn_timeout.setSuffix(" s")
            self.spn_timeout.setValue(parent.server_timeout if parent else 30)
            frame = QFrame(self)
            frame.setAutoFillBackground(True)
            frame_pal = frame.palette()
            frame_pal.setColor(QPalette.Window, Qt.white)
            frame.setPalette(frame_pal)
            frame_layout = QVBoxLayout(frame)
            frame_layout.setContentsMargins(20, 20, 20, 20)
            frame_layout.setSpacing(20)
            heading = QLabel("<h2>Allgemeine Einstellungen</h2><p>Hier können Sie Ihre Einstellungen anpassen.</p>")
            heading.setStyleSheet("QLabel { color: #374151; font-size: 20px; font-weight: 600; }")
            frame_layout.addWidget(heading)
            frame_layout.addWidget(self.chk_darkmode)
            frame_layout.addWidget(QLabel("Server-Timeout:"))
            frame_layout.addWidget(self.spn_timeout)
            btn_row = QHBoxLayout()
            btn_row.addStretch()

            def _make_btn(text: str) -> QPushButton:
                btn = QPushButton(text)
                btn.setFixedHeight(48)
                btn.setStyleSheet(MainWindow._button_style())
                return btn

            self.btn_apply = _make_btn("Anwenden")
            self.btn_cancel = _make_btn("Abbrechen")
            self.btn_apply.clicked.connect(self.apply_settings)
            self.btn_cancel.clicked.connect(self.close)
            btn_row.addWidget(self.btn_apply)
            btn_row.addWidget(self.btn_cancel)
            frame_layout.addLayout(btn_row)
            main_layout.addWidget(frame)

        def apply_settings(self) -> None:
            main = self.parent()
            if main:
                SettingsWindow.apply_dark_palette(self.chk_darkmode.isChecked())
                main.server_timeout = self.spn_timeout.value()
            QMessageBox.information(self, "Einstellungen", "Änderungen angewendet.")

        def reset_settings(self) -> None:
            main = self.parent()
            if main:
                self.chk_darkmode.setChecked(SettingsWindow._is_dark(QApplication.instance()))
                self.spn_timeout.setValue(main.server_timeout)

        def closeEvent(self, event):
            parent = self.parent()
            if parent and hasattr(parent, "settings_window"):
                parent.settings_window = None
            super().closeEvent(event)

    # -------------------- Server runner: try external, else internal Flask app -------------------- #
    def run_server_background():
        """
        Try to import ServerFileuploader.start_server; if not available, run local Flask `app`.
        Running in a daemon thread so it won't block app exit.
        """
        try:
            from ServerFileuploader import start_server
            try:
                start_server(host="127.0.0.1", port=5000, debug=False)
                return
            except Exception:
                pass
        except Exception:
            pass

        # Fallback: run the included Flask app
        try:
            # Note: Flask's app.run is blocking, so run it in this thread (already a thread).
            app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
        except Exception as exc:
            print("Failed to start internal Flask server:", exc)

    if os.environ.get("FILEUPLOADER_NO_SERVER") != "1":
        server_thread = threading.Thread(target=run_server_background, daemon=True)
        server_thread.start()

    # -------------------- Application entry point -------------------- #
    def main():
        qapp = QApplication(sys.argv)
        window = MainWindow()
        window.show()
        sys.exit(qapp.exec_())

    if __name__ == "__main__":
        main()
