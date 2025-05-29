import sys
from PyQt6 import QtWidgets, QtGui
from PyQt6.QtCore import Qt, QObject, QSize, QAbstractListModel, QModelIndex, pyqtSignal, QThread, QRunnable, QThreadPool, QTimer
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtMultimedia import QWindowCapture, QMediaCaptureSession, QCapturableWindow, QMediaDevices, QCamera, QCameraDevice, QCameraFormat
from PyQt6.QtMultimediaWidgets import QVideoWidget
import pathlib
import logging
import socket
import select
import datetime
from enum import Enum, auto
from fdp import ForzaDataPacket
from time import sleep

logging.basicConfig(level=logging.INFO)


class WarningMessages(Enum):
    """Defines a set of helpful warning messages to display to the user if they have not
    performed an action properly."""
    
    CaptureSettingsNotConfigured = "Warning: Telemetry and footage capture settings must be configured before starting a capture."


class UDPWorker(QRunnable):

    class Signals(QObject):
        """Signals for the UDPWorker"""
        finished = pyqtSignal()
        collected = pyqtSignal(bytes)

    def __init__(self, port:int):
        super().__init__()
        self.signals = UDPWorker.Signals()
        self.working = True
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(0)  # Set to non blocking, so the thread can be terminated without the socket blocking forever
        self.socketTimeout = 1
        self.port = port

    def run(self):
        """Binds the socket and starts listening for packets"""
        self.sock.bind(('', self.port))
        logging.info("Started listening on port {}".format(self.port))

        while self.working:
            try:
                ready = select.select([self.sock], [], [], self.socketTimeout)
                if ready[0]:
                    data, address = self.sock.recvfrom(1024)
                    logging.debug('received {} bytes from {}'.format(len(data), address))
                    self.signals.collected.emit(data)
                else:
                    logging.debug("Socket timeout")
            except BlockingIOError:
                logging.info("Could not listen to {}, trying again...".format(address))
        
        # Close the socket after the player wants to stop listening, so that
        # a new socket can be created using the same port next time
        self.sock.close()
        logging.info("Socket closed.")
        self.signals.finished.emit()
    
    def finish(self):
        """Stops the thread"""
        self.working = False
    
    def changePort(self, port:int):
        """Changes the port that the worker will listen to"""
        self.port = port


class SourceType(Enum):
    Camera = auto()
    Window = auto()


class WindowListModel(QAbstractListModel):
    """Contains a list of capturable windows currently open"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._window_list = QWindowCapture.capturableWindows()

    def rowCount(self, index: QModelIndex):
        return len(self._window_list)

    def data(self, index: QModelIndex, role: Qt.ItemDataRole):
        if role == Qt.ItemDataRole.DisplayRole:
            window = self._window_list[index.row()]
            return window.description()
        return None

    def window(self, index: QModelIndex):
        """Returns the QCapturableWindow object held at the index"""
        return self._window_list[index.row()]

    def populate(self):
        """Populates the model with all the currently capturable windows"""
        self.beginResetModel()
        self._window_list = QWindowCapture.capturableWindows()
        self.endResetModel()


class CameraDeviceListModel(QAbstractListModel):
    """Contains a list of available cameras"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._camera_list = QMediaDevices.videoInputs()

        # If the cameras change so do the list
        self.qmd = QMediaDevices()
        self.qmd.videoInputsChanged.connect(self.populate)

    def rowCount(self, index: QModelIndex):
        return len(self._camera_list)

    def data(self, index: QModelIndex, role: Qt.ItemDataRole):
        if role == Qt.ItemDataRole.DisplayRole:
            camera = self._camera_list[index.row()]
            return camera.description()
        return None

    def camera(self, index: QModelIndex):
        """Returns the QCameraDevice object held at the index"""
        return self._camera_list[index.row()]

    def populate(self):
        """Populates the model with all the currently capturable windows"""
        self.beginResetModel()
        self._camera_list = QMediaDevices.videoInputs()
        self.endResetModel()


class CameraFormatListModel(QAbstractListModel):
    """Contains a list of possible camera formats for a given camera device"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._camera_format_list = []
        self._camera_device: QCameraDevice = QCameraDevice()

    def rowCount(self, index: QModelIndex):
        return len(self._camera_format_list)

    def data(self, index: QModelIndex, role: Qt.ItemDataRole):
        if role == Qt.ItemDataRole.DisplayRole:
            format: QCameraFormat = self._camera_format_list[index.row()]
            frameRate = ""
            if format.minFrameRate() == format.maxFrameRate():
                frameRate = format.minFrameRate()
            else:
                frameRate = "{}-{}".format(format.minFrameRate(), format.maxFrameRate())
            result = "Resolution={}x{}, Frame Rate={}, Pixel Format={}".format(
                format.resolution().width(), format.resolution().height(),
                frameRate,
                format.pixelFormat().name)
            return result
        return None

    def cameraFormat(self, index: QModelIndex):
        """Returns the QCameraFormat object held at the index"""
        return self._camera_format_list[index.row()]
    
    def changeCameraDevice(self, cameraDevice: QCameraDevice):
        """Reset the format list to the formats found in the new camera device"""
        self._camera_device = cameraDevice
        self.beginResetModel()
        self._camera_format_list = self._camera_device.videoFormats()
        self.endResetModel()


class FootageCapture(QObject):
    """A class to manage the recording and capture of race videos from window/application capture or camera input"""

    def __init__(self, parent = None):
        super().__init__(parent)

    def start(self):
        """Start recording from the selected video source"""
        ...
    
    def stop(self):
        """Stop recording the video and save to a file"""
        ...


class TelemetryCaptureError(Enum):
    """The possible errors that can be raised by a TelemetryCapture object"""
    
    PortChangeError = "Port cannot be changed while telemetry is being captured"
    CaptureFailed = "Telemetry capture has failed"
    BadPacketReceived = "Packet could not be processed"


class TelemetryCapture(QObject):
    """A class to manage the recording and capture of Forza telemetry"""
    
    class Signals(QObject):
        collected = pyqtSignal(ForzaDataPacket)  # Emitted on collection of a forza data packet
        errorOccurred = pyqtSignal(TelemetryCaptureError)
        activeChanged = pyqtSignal(bool)
            
    def __init__(self, parent = None, port = None):
        super().__init__(parent)
        self.signals = TelemetryCapture.Signals()
        self._active = False  # Whether telemetry is currently being recorded
        self._port: int = port  # The port that listens for incoming Forza data packets
        self._packetsCollected = 0
        self._invalidPacketsCollected = 0
        self._threadpool = QThreadPool(self)
        self._worker: UDPWorker = None
        self._startTime: datetime.datetime = None  # The date and time that the object started recording
        self._endTime: datetime.datetime = None  # The date and time that the object stopped recording

    def start(self):
        """Start recording telemetry"""
        
        if self._port is None:
            self.signals.errorOccurred.emit(TelemetryCaptureError.CaptureFailed)
            return

        self._packetsCollected = 0
        self._invalidPacketsCollected = 0
        
        self._worker = UDPWorker(self._port)
        self._worker.setAutoDelete(True)
        self._worker.signals.collected.connect(self._onCollected)
        self._worker.signals.finished.connect(self._onFinished)
        self._threadpool.start(self._worker)
        self._setActive(True)
    
    def _setActive(self, active: bool):
        """Sets the active attribute"""
        if active:
            self._startTime = datetime.datetime.now()
            self._endTime = None
        else:
            self._endTime = datetime.datetime.now()

        self._active = active
        self.signals.activeChanged.emit(active)
        
    def _onCollected(self, data: bytes):
        """Called when a single UDP packet is collected. Receives the unprocessed
        packet data, transforms it into a Forza Data Packet and emits the collected signal with
        that forza data packet object. If packet cannot be read, it will emit an error signal"""

        fdp: ForzaDataPacket = None
        try:
            fdp = ForzaDataPacket(data)
            self._packetsCollected += 1
            self.signals.collected.emit(fdp)
        except:
            # If it's not a forza packet
            self.signals.errorOccurred.emit(TelemetryCaptureError.BadPacketReceived)
            self._invalidPacketsCollected += 1

        if self._packetsCollected % 60 == 0:
            logging.debug(f"Received {self._packetsCollected} packets.")
        if self._invalidPacketsCollected % 60 == 0:
            logging.debug(f"Received {self._invalidPacketsCollected} invalid packets.")

    def _onFinished(self):
        """Cleans up after the worker has stopped listening to packets"""
        self._setActive(False)

    def stop(self):
        """Stops recording telemetry"""
        if self._worker is not None:
            self._worker.finish()
    
    def setPort(self, port: int):
        """Sets the port to listen to. If the object is currently capturing telemetry, the port will not change and an error will occur."""
        if self._active:
            self.signals.errorOccurred.emit(TelemetryCaptureError.PortChangeError)
        else:
            self._port = port
    
    def getPort(self) -> int | None:
        """Returns the current port. Returns None if the port hasn't been set yet."""
        return self._port
    
    def isActive(self) -> bool:
        """Returns whether this object is currently capturing telemetry packets"""
        return self._active

    def getPacketsCollected(self) -> int:
        """Returns the number of valid packets collected during the last capture session"""
        return self._packetsCollected
    
    def getInvalidPacketsCollected(self) -> int:
        """Returns the number of invalid packets collected during the last capture session"""
        return self._invalidPacketsCollected

    def getStartTime(self) -> datetime.datetime | None:
        """Returns the start time of the last capture as a datetime object. Returns None if no capture session has started."""
        return self._startTime
    
    def getEndTime(self) -> datetime.datetime | None:
        """Returns the end time of the last capture as a datetime object. Returns None if no capture session has ended."""
        return self._endTime

    def ready(self) -> bool:
        """Returns True if the object is configured and ready to start recording. Returns False if it is not ready, or
        is already capturing."""
        if self._port is not None and not self._active:
            return True
        else:
            return False


class FootageCaptureSettingsWidget(QtWidgets.QWidget):
    """A widget to help configure settings for capturing race footage"""

    def __init__(self, parent = None):
        super().__init__(parent)
        
        self._source_type = SourceType.Window
        self._window_label = QtWidgets.QLabel("Select window to capture:", self)
        self._camera_label = QtWidgets.QLabel("Select camera to capture:", self)
        self._camera_format_label = QtWidgets.QLabel("Select camera format:", self)
        self._media_capture_session = QMediaCaptureSession(self)  # The capture session that will handle window or camera capture
        self._video_widget_label = QtWidgets.QLabel("Capture output:", self)
        self._start_stop_button = QtWidgets.QPushButton("Select an input device", self)
        self._status_label = QtWidgets.QLabel(self)

        # Starts a preview of the capture session
        self._video_widget = QVideoWidget(self)
        self._media_capture_session.setVideoOutput(self._video_widget)

        # Gets a list of capturable windows
        self._window_list_view = QtWidgets.QListView(self)
        self._window_list_model = WindowListModel(self)
        self._window_list_view.setModel(self._window_list_model)

        self._window_capture = QWindowCapture(self)
        self._window_capture.start()
        self._media_capture_session.setWindowCapture(self._window_capture)

        # Adds a context menu to the window list view with an action to refresh the capturable windows
        update_window_list_action = QAction("Update Windows List", self)
        update_window_list_action.triggered.connect(self._window_list_model.populate)
        self._window_list_view.addAction(update_window_list_action)
        self._window_list_view.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)

        # Gets a list of cameras that can be captured
        self._camera_device_list_view = QtWidgets.QListView(self)
        self._camera_device_list_model = CameraDeviceListModel(self)
        self._camera_device_list_view.setModel(self._camera_device_list_model)

        self._camera = QCamera(self)
        #self._camera.start()
        self._media_capture_session.setCamera(self._camera)

        # Adds a context menu to the camera list view with an action to refresh the available cameras
        update_camera_device_list_action = QAction("Update Camera List", self)
        update_camera_device_list_action.triggered.connect(self._camera_device_list_model.populate)
        self._camera_device_list_view.addAction(update_camera_device_list_action)
        self._camera_device_list_view.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)

        # Gets a list of all the possible camera formats for the selected camera
        self._camera_format_list_view = QtWidgets.QListView(self)
        self._camera_format_list_model = CameraFormatListModel(self)
        self._camera_format_list_view.setModel(self._camera_format_list_model)

        # Layout the dialog with the camera list, window list, and footage preview
        grid_layout = QtWidgets.QGridLayout(self)
        grid_layout.addWidget(self._window_label, 0, 0)
        grid_layout.addWidget(self._window_list_view, 1, 0)

        grid_layout.addWidget(self._camera_label, 2, 0)
        grid_layout.addWidget(self._camera_device_list_view, 3, 0)

        grid_layout.addWidget(self._video_widget_label, 0, 1)
        grid_layout.addWidget(self._video_widget, 1, 1, 1, 1)
        grid_layout.addWidget(self._start_stop_button, 4, 0)
        grid_layout.addWidget(self._status_label, 4, 1)

        grid_layout.addWidget(self._camera_format_label, 2, 1)
        grid_layout.addWidget(self._camera_format_list_view, 3, 1)

        grid_layout.setColumnStretch(1, 1)
        grid_layout.setRowStretch(1, 1)
        grid_layout.setColumnMinimumWidth(0, 400)
        grid_layout.setColumnMinimumWidth(1, 400)
        grid_layout.setRowMinimumHeight(3, 1)
        
        # Connect the functions that update the footage sources when a new window or camera is clicked
        selection_model = self._window_list_view.selectionModel()
        selection_model.selectionChanged.connect(self.on_current_window_selection_changed)

        selection_model = self._camera_device_list_view.selectionModel()
        selection_model.selectionChanged.connect(self.on_current_camera_device_selection_changed)

        selection_model = self._camera_format_list_view.selectionModel()
        selection_model.selectionChanged.connect(self.on_current_camera_format_selection_changed)

        self._start_stop_button.clicked.connect(self.on_start_stop_button_clicked)
        self._window_capture.errorOccurred.connect(self.on_window_capture_error_occured,
                                                   Qt.ConnectionType.QueuedConnection)
        self._camera.errorOccurred.connect(self.on_camera_error_occured,
                                                   Qt.ConnectionType.QueuedConnection)

    def on_current_window_selection_changed(self, selection):
        self.clear_error_string()
        indexes = selection.indexes()
        if indexes:
            window = self._window_list_model.window(indexes[0])
            if not window.isValid():
                m = "The window is no longer valid. Update the list of windows?"
                answer = QtWidgets.QMessageBox.question(self, "Invalid window", m)
                if answer == QtWidgets.QMessageBox.Yes:
                    self.update_active(SourceType.Window, False)
                    self._window_list_view.clearSelection()
                    self._window_list_model.populate()
                    return
            self._window_capture.setWindow(window)
            self.update_active(SourceType.Window, self.is_active())
            self._camera_device_list_view.clearSelection()
        else:
            self._window_capture.setWindow(QCapturableWindow())
    
    def on_current_camera_device_selection_changed(self, selection):
        self.clear_error_string()
        indexes = selection.indexes()
        if indexes:
            cameraDevice = self._camera_device_list_model.camera(indexes[0])
            if cameraDevice.isNull():
                m = "The camera is no longer valid. Update the list of cameras?"
                answer = QtWidgets.QMessageBox.question(self, "Invalid camera", m)
                if answer == QtWidgets.QMessageBox.Yes:
                    self.update_active(SourceType.Camera, False)
                    self._camera_device_list_view.clearSelection()
                    self._camera_device_list_model.populate()
                    return
            self._camera.setCameraDevice(cameraDevice)
            self.update_active(SourceType.Camera, self.is_active())
            self._window_list_view.clearSelection()

            # Populate the camera format model with updated formats
            self._camera_format_list_model.changeCameraDevice(cameraDevice)

        else:
            self._camera.setCameraDevice(QCameraDevice())
            self._camera_format_list_model.changeCameraDevice(QCameraDevice())

    def on_current_camera_format_selection_changed(self, selection):
        """Updates the camera format for the currently selected camera"""
        indexes = selection.indexes()
        if indexes:
            cameraFormat: QCameraFormat = self._camera_format_list_model.cameraFormat(indexes[0])
            self._camera.setCameraFormat(cameraFormat)

    def on_window_capture_error_occured(self, error, error_string):
        self.set_error_string("QWindowCapture: Error occurred " + error_string)
    
    def on_camera_error_occured(self, error, error_string):
        self.set_error_string("QCamera: Error occurred " + error_string)
    
    def set_error_string(self, t):
        self._status_label.setStyleSheet("background-color: rgb(255, 0, 0);")
        self._status_label.setText(t)

    def clear_error_string(self):
        self._status_label.clear()
        self._status_label.setStyleSheet("")

    def on_start_stop_button_clicked(self):
        self.clear_error_string()
        self.update_active(self._source_type, not self.is_active())

    def update_start_stop_button_text(self):
        active = self.is_active()
        if self._source_type == SourceType.Window:
            m = "Stop window preview" if active else "Start window preview"
            self._start_stop_button.setText(m)
        elif self._source_type == SourceType.Camera:
            m = "Stop camera preview" if active else "Start camera preview"
            self._start_stop_button.setText(m)

    def update_active(self, source_type, active):
        self._source_type = source_type
        self._window_capture.setActive(active and source_type == SourceType.Window)
        self._camera.setActive(active and source_type == SourceType.Camera)

        self.update_start_stop_button_text()

    def is_active(self):
        if self._source_type == SourceType.Window:
            return self._window_capture.isActive()
        if self._source_type == SourceType.Camera:
            return self._camera.isActive()
        return False


class TelemetryCaptureSettingsWidget(QtWidgets.QWidget):
    """A widget to help configure settings to capture race telemetry"""
    
    def __init__(self, parent = None):
        super().__init__(parent)

        self._ipLabel = QtWidgets.QLabel("IP Address: " + TelemetryCaptureSettingsWidget.getIP(), self)
        self._testDisplay = QtWidgets.QPlainTextEdit("Waiting for packets...", self)
        self._testDisplay.setReadOnly(True)
        self._testDisplayLabel = QtWidgets.QLabel("Connection Test Output")
        self._testConnectionButton = QtWidgets.QPushButton("Test Connection", self)
        self._testConnectionButton.pressed.connect(self.startTest)
        self._status_label = QtWidgets.QLabel(self)

        self._portSpinBox = QtWidgets.QSpinBox(self)
        self._portSpinBox.setRange(1025, 65535)
        self._portSpinBox.setValue(1337)  # Hard code a default for now, but use value from a config file later

        self._telemetryCapture = TelemetryCapture()
        self._telemetryCapture.setPort(self._portSpinBox.value())
        self._telemetryCapture.signals.activeChanged.connect(self.onActiveChanged)
        self._telemetryCapture.signals.collected.connect(self.onCollected)
        self._portSpinBox.valueChanged.connect(self._telemetryCapture.setPort)

        # 7 second timer for the connection test
        self._timer = QTimer(self)
        self._timer.setInterval(7000)
        self._timer.timeout.connect(self._telemetryCapture.stop)

        self._formLayout = QtWidgets.QFormLayout()
        self._formLayout.addRow("Port", self._portSpinBox)
        
        lt = QtWidgets.QGridLayout()
        self.setLayout(lt)
        lt.addWidget(self._ipLabel, 0, 0)
        lt.addWidget(self._testDisplayLabel, 0, 1)
        lt.addLayout(self._formLayout, 1, 0, 2, 1)
        lt.addWidget(self._testDisplay, 1, 1, 2, 1)
        lt.addWidget(self._status_label, 2, 0)
        lt.addWidget(self._testConnectionButton, 2, 1)

        lt.setColumnStretch(1, 1)
        lt.setRowStretch(1, 1)
        lt.setColumnMinimumWidth(0, 400)
        lt.setColumnMinimumWidth(1, 400)
        lt.setRowMinimumHeight(3, 1)

    def startTest(self):
        """Sets up and runs the thread to start listening for UDP Forza data packets"""

        self._portSpinBox.setEnabled(False)
        self._testConnectionButton.setEnabled(False)
        self._testDisplay.clear()
        self._testDisplay.insertPlainText("Running connection test...\n")
        self._testDisplay.insertPlainText("Make sure there is an active Forza race happening in order to receive data.\n\n")

        self._telemetryCapture.start()
        self._timer.start()
        logging.debug("Test started")
    
    def stopTest(self):
        """Stops the thread listening for Forza packets"""
        self._telemetryCapture.stop()
        self._timer.stop()

    def onCollected(self, fdp: ForzaDataPacket):
        """Called when a single valid Forza Data Packet is collected"""

        logging.debug("onCollected: Received FDP")

        packets = self._telemetryCapture.getPacketsCollected()
        if packets % 60 == 0:
            self._testDisplay.insertPlainText(f"Collected {packets} packets\n")

    def onActiveChanged(self, active: bool):
        """Called when the telemetry capture object changes its active status"""
        if active:
            self._testDisplay.insertPlainText("Telemetry capture active.\n")
            startTime = self._telemetryCapture.getStartTime()
            self._testDisplay.insertPlainText(f"Test started at {startTime}.\n")
        else:
            self.onTestStopped()
            endTime = self._telemetryCapture.getEndTime()
            self._testDisplay.insertPlainText(f"Test finished at {endTime}.\n")

    def onTestStopped(self):
        """Called after the port is closed and the dashboard stops listening to packets"""
        logging.debug("Finished listening")
        self._testDisplay.insertPlainText("Finished test - ")

        valid = self._telemetryCapture.getPacketsCollected()
        invalid = self._telemetryCapture.getInvalidPacketsCollected()

        if valid == 0 and invalid == 0:
            self._testDisplay.insertPlainText("No packets detected. Try another port.\n")
        elif valid == 0 and invalid > 0:
            self._testDisplay.insertPlainText("Connection was established but packets couldn't be processed. Make sure the Packet Format is set to 'Dash'.\n")
        elif valid > 0 and invalid == 0:
            if valid > 350:
                self._testDisplay.insertPlainText("Good connection.\n")
            else:
                self._testDisplay.insertPlainText("Connection established but is poor.\n")
        else:
            self._testDisplay.insertPlainText("Connection was established but some packets couldn't be processed. Try another port.\n")

        self._portSpinBox.setEnabled(True)
        self._testConnectionButton.setEnabled(True)

    def onCaptureError(self, error: TelemetryCaptureError):
        match error:
            case TelemetryCaptureError.PortChangeError:
                self.set_error_string(error.value)
                self._portSpinBox.setValue(self._telemetryCapture.getPort())

            case TelemetryCaptureError.CaptureFailed:
                self.set_error_string(error.value)

            case TelemetryCaptureError.BadPacketReceived:
                self.set_error_string(error.value)

            case _:
                ...

    def set_error_string(self, t):
        self._status_label.setStyleSheet("background-color: rgb(255, 0, 0);")
        self._status_label.setText(t)

    def clear_error_string(self):
        self._status_label.clear()
        self._status_label.setStyleSheet("")

    def getIP():
        """Returns the local IP address as a string. If an error is encountered while trying to
        establish a connection, it will return None."""

        ip = None
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 1337))
                ip = s.getsockname()[0]
                s.close()
        except:
            return None
        return str(ip)


class CaptureDialog(QtWidgets.QDialog):
    """A dialog to help configure settings for capturing race footage and telemetry"""

    def __init__(self, parent = None):
        super().__init__(parent)

        self.footageWidget = FootageCaptureSettingsWidget()
        self.telemetryWidget = TelemetryCaptureSettingsWidget()
        tabs = QtWidgets.QTabWidget()
        tabs.addTab(self.footageWidget, "Footage Capture Settings")
        tabs.addTab(self.telemetryWidget, "Telemetry Settings")
        
        self._button_box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Apply |
            QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        self._button_box.accepted.connect(self.onAccepted)
        self._button_box.rejected.connect(self.onRejected)

        lt = QtWidgets.QVBoxLayout()
        self.setLayout(lt)
        lt.addWidget(tabs)
        lt.addWidget(self._button_box)
    
    def onAccepted(self):
        self.telemetryWidget.stopTest()
        sleep(1)
        self.accept()
    
    def onRejected(self):
        self.telemetryWidget.stopTest()
        sleep(1)
        self.reject()


class CaptureManager(QObject):
    """A class to manage the recording and capture of Forza telemetry and race footage together"""

    def __init__(self, parent = None):
        super().__init__(parent)
        self.footageSession = FootageCapture()
        self.telemetrySession = TelemetryCapture()

        # If there is footage or telemetry currently being captured
        self.capturing: bool = False
    
    def isConfigured(self) -> bool:
        """Returns True if the CaptureManager is ready to capture telemetry and footage"""
        ...

    def openConfigureDialog(self):
        """Opens the dialog to help configure the capture settings"""
        
        dlg = CaptureDialog()
        if dlg.exec():
            logging.info("Success")
        else:
            logging.info("Canceled")
    
    def startCapture(self):
        """Start recording from the selected video source"""
        
        if not self.configured:
            QtWidgets.QMessageBox.critical(self.parent(), "Error", WarningMessages.CaptureSettingsNotConfigured.value)
        
        if self.capturing:
            return
        
        ...
    
    def stopCapture(self):
        """Stop recording the video and save to a file"""

        if not self.capturing:
            return
        
        ...
    
    def toggleCapture(self, capture: bool):
        """Starts or stops capturing footage and telemetry"""
        
        self.startCapture() if capture else self.stopCapture()


class MainWindow(QtWidgets.QMainWindow):

    def __init__(self, parent = None):
        super().__init__(parent)

        parentDir = pathlib.Path(__file__).parent.parent.resolve()

        self.captureManager = CaptureManager()

        toolbar = QtWidgets.QToolBar()
        toolbar.setIconSize(QSize(16, 16))
        self.addToolBar(toolbar)

        # Status bar at the bottom of the application
        self.setStatusBar(QtWidgets.QStatusBar(self))

        configureCaptureAction = QAction(QIcon(str(parentDir / pathlib.Path("assets/icons/gear.png"))), "Configure Capture Settings", self)
        configureCaptureAction.setStatusTip("Configure Capture Settings: Change the settings used for capturing race footage and telemetry.")
        configureCaptureAction.triggered.connect(self.captureManager.openConfigureDialog)
        toolbar.addAction(configureCaptureAction)

        toggleCaptureAction = QAction(QIcon(str(parentDir / pathlib.Path("assets/icons/control-record.png"))), "Start/Stop Capture", self)
        toggleCaptureAction.setStatusTip("Start/Stop Capture: Start/Stop capturing race footage and telemetry data.")
        toggleCaptureAction.setCheckable(True)
        toggleCaptureAction.triggered.connect(self.captureManager.toggleCapture)
        toolbar.addAction(toggleCaptureAction)


def run():
    app = QtWidgets.QApplication(sys.argv)

    db = MainWindow()
    db.showMaximized()

    sys.exit(app.exec())

if __name__ == "__main__":
    run()

