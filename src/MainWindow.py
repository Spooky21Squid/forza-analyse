from PyQt6 import QtWidgets, QtMultimedia
from PyQt6.QtCore import pyqtSlot, QThread, QObject, pyqtSignal, Qt, QSize, QUrl, QAbstractTableModel, QItemSelection, QModelIndex, QRunnable, QThreadPool, QTimer
from PyQt6.QtGui import QAction, QIcon, QKeySequence, QColor, QStandardItemModel, QStandardItem, QPixmap, QPen, QCloseEvent
from PyQt6.QtMultimedia import QMediaDevices, QCamera, QMediaCaptureSession, QCameraDevice, QCameraFormat
from PyQt6.QtMultimediaWidgets import QVideoWidget

import pyqtgraph as pg
import numpy as np
import pandas as pd

import distinctipy
from fdp import ForzaDataPacket
from CaptureMode import CaptureModeWidget
from Settings import SettingsManager

import datetime
from time import sleep
import pathlib
import yaml
import logging
import select
import socket
import random
from enum import Enum, auto
from collections import OrderedDict
from abc import ABC, abstractmethod
from typing import Literal


class UDPWorker(QRunnable):
    """Listens to a single UDP socket and emits the bytes collected from a UDP packet through the 'collected' signal"""

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
        """Signals to the worker to stop and close the socket. The thread is not properly finished until the
        'finished' signal is emitted"""
        self.working = False
    
    def changePort(self, port:int):
        """Changes the port that the worker will listen to. If already running, this will do nothing until the worker is
        started again."""
        self.port = port


class TelemetryCaptureError(Enum):
    """The possible errors that can be raised by a TelemetryCapture object"""
    
    PortChangeError = "Port cannot be changed while telemetry is being captured"
    CaptureFailed = "Telemetry capture has failed"
    BadPacketReceived = "Packet could not be processed"


class TelemetryCapture(QObject):
    """A class to manage the recording and capture of Forza telemetry"""

    class Status(Enum):
        NotListening = auto()  # No socket set up
        Listening = auto()  # Socket and port set up, listening for packets but no Forza packets are being detected
        Capturing = auto()  # Forza packets detected and being captured

    
    class Signals(QObject):
        collected = pyqtSignal(ForzaDataPacket)  # Emitted on collection of a forza data packet
        errorOccurred = pyqtSignal(TelemetryCaptureError)
        activeChanged = pyqtSignal(bool)
        statusChanged = pyqtSignal(object)
        portChanged = pyqtSignal(int)


    def __init__(self, parent = None, port = None):
        super().__init__(parent)
        self.signals = TelemetryCapture.Signals()
        self._active = False  # Whether telemetry is currently being recorded
        self._status: TelemetryCapture.Status = self.Status.NotListening
        self._port: int = port  # The port that listens for incoming Forza data packets
        self._packetsCollected = 0
        self._invalidPacketsCollected = 0
        self._threadpool = QThreadPool(self)
        self._worker: UDPWorker = None
        self._startTime: datetime.datetime = None  # The date and time that the object started recording
        self._endTime: datetime.datetime = None  # The date and time that the object stopped recording

    def start(self):
        """Start recording telemetry"""
        
        if self._port is None or self._active:
            self.signals.errorOccurred.emit(TelemetryCaptureError.CaptureFailed)
            return

        self._setStatus(self.Status.Listening)

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
            self._setStatus(self.Status.Capturing)
            self._packetsCollected += 1
            self.signals.collected.emit(fdp)
        except:
            # If it's not a forza packet
            self._setStatus(self.Status.Listening)
            self.signals.errorOccurred.emit(TelemetryCaptureError.BadPacketReceived)
            self._invalidPacketsCollected += 1

        if self._packetsCollected % 60 == 0:
            logging.debug(f"Received {self._packetsCollected} packets.")
        if self._invalidPacketsCollected % 60 == 0:
            logging.debug(f"Received {self._invalidPacketsCollected} invalid packets.")

    def _onFinished(self):
        """Cleans up after the worker has stopped listening to packets"""
        self._setActive(False)
        self._setStatus(self.Status.NotListening)

    def stop(self):
        """Stops recording telemetry"""
        if self._active and self._worker is not None:
            self._worker.finish()
    
    def setPort(self, port: int):
        """Sets the port to listen to. If the object is currently capturing telemetry, the port will not change and an error will occur."""
        if self._active:
            self.signals.errorOccurred.emit(TelemetryCaptureError.PortChangeError)
        else:
            self._port = port
            self.signals.portChanged.emit(port)
    
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
    
    def _setStatus(self, status):
        """Sets the status of the object and emits a status changed signal"""
        if self._status is not status:
            self._status = status
            self.signals.statusChanged.emit(status)
    
    def getStatus(self):
        """Returns the current status of the telemetry capture object"""
        return self._status


class AnalyseModeWidget(QtWidgets.QFrame):
    """Provides an interface for analysing forza telemetry files and footage"""

    def __init__(self, parent = None):
        super().__init__(parent)

        self.grid = QtWidgets.QGridLayout()
        self.setLayout(self.grid)

        self.placeholder = QtWidgets.QLabel("Analyse Mode")
        self.grid.addWidget(self.placeholder, 0, 0)


class CaptureStatusBar(QtWidgets.QFrame):
    """A horizontal status bar for capturing telemetry and footage"""

    class FootageCaptureStatus(Enum):
        NotCapturing = auto()  # No valid footage source set up
        Capturing = auto()  # Footage source detected but not recording to a file yet
        Recording = auto()  # Footage being recorded to a file


    def __init__(self, parent = None):
        super().__init__(parent)
        
        lt = QtWidgets.QHBoxLayout()
        self.setLayout(lt)

        self.port = 0
        self.telemetryCaptureStatus = TelemetryCapture.Status.NotListening
        self.telemetryStatusLabel = QtWidgets.QLabel()
        lt.addWidget(self.telemetryStatusLabel)

        self.footageCaptureStatus = self.FootageCaptureStatus.NotCapturing
        self.footageStatusLabel = QtWidgets.QLabel()
        lt.addWidget(self.footageStatusLabel)

        self.updateFootageStatusLabel()
        self.updateTelemetryStatusLabel()
    
    def updateFootageStatusLabel(self):
        """Update the footage capture label with the currently held status"""
        if self.footageCaptureStatus is self.FootageCaptureStatus.NotCapturing:
            self.footageStatusLabel.setText(f"Footage Status: Not Capturing")
        elif self.footageCaptureStatus is self.FootageCaptureStatus.Capturing:
            self.footageStatusLabel.setText(f"Footage Status: Footage detected.")
        elif self.footageCaptureStatus is self.FootageCaptureStatus.Recording:
            self.footageStatusLabel.setText(f"Footage Status: Recording footage.")

    def setFootageCaptureStatus(self, status):
        """Set the footage capture status"""
        if self.footageCaptureStatus is not status:
            self.footageCaptureStatus = status
            self.updateFootageStatusLabel()

    def updateTelemetryStatusLabel(self):
        """Update the telemetry capture label with the currently held status"""
        if self.telemetryCaptureStatus is TelemetryCapture.Status.NotListening:
            self.telemetryStatusLabel.setText(f"Telemetry Status: Not Listening")
        elif self.telemetryCaptureStatus is TelemetryCapture.Status.Listening:
            self.telemetryStatusLabel.setText(f"Telemetry Status: Listening on port {self.port}.")
        elif self.telemetryCaptureStatus is TelemetryCapture.Status.Capturing:
            self.telemetryStatusLabel.setText(f"Telemetry Status: Capturing Forza data on port {self.port}.")
    
    def setTelemetryCaptureStatus(self, status):
        """Set the telemetry capture status"""
        self.telemetryCaptureStatus = status
        self.updateTelemetryStatusLabel()
    
    def setPort(self, port: int):
        """Sets a new port to be displayed with the status"""
        self.port = port
        self.updateTelemetryStatusLabel()


class MainWindow(QtWidgets.QMainWindow):

    class ModeIndex(Enum):
        AnalyseMode = 0
        CaptureMode = 1


    def __init__(self):
        super().__init__()

        parentDir = pathlib.Path(__file__).parent.parent.resolve()

        # Import the app settings
        settingsManager = SettingsManager()
        settingsManager.load(str(parentDir / pathlib.Path("config/config.json")))

        self._closeTimer = QTimer()
        self._closeTimer.timeout.connect(self._onCloseTimerTimeout)

        # A DataFrame containing all the track details
        trackDetailsPath = parentDir / pathlib.Path("config/track-details.csv")
        self.forzaTrackDetails = pd.read_csv(str(trackDetailsPath), index_col="ordinal")

        # Set the icon and title
        self.setWindowIcon(QIcon(str(parentDir / pathlib.Path("assets/images/Forza-logo-512.png"))))
        self.setWindowTitle("Forza Analyse")

        mainWidget = QtWidgets.QWidget()
        self.setCentralWidget(mainWidget)
        vbLayout = QtWidgets.QVBoxLayout()
        mainWidget.setLayout(vbLayout)

        # Create a capture status bar
        self.captureStatus = CaptureStatusBar()
        vbLayout.addWidget(self.captureStatus)

        # Create a stacked widget - each child is a different mode (eg. analyse, record)
        self.stackedModes = QtWidgets.QStackedWidget()
        vbLayout.addWidget(self.stackedModes)
        self.analyseMode = AnalyseModeWidget()
        self.recordMode = CaptureModeWidget()
        self.stackedModes.addWidget(self.analyseMode)
        self.stackedModes.addWidget(self.recordMode)
        self.mode = self.ModeIndex.AnalyseMode
        self.setModeCapture()

        # Set up telemetry capture and try to start capturing
        p = settingsManager.get("recording", "port", default=7676)
        self.telemetryCapture = TelemetryCapture()
        self.telemetryCapture.signals.statusChanged.connect(self.captureStatus.setTelemetryCaptureStatus)
        self.telemetryCapture.signals.portChanged.connect(self.captureStatus.setPort)
        self.telemetryCapture.setPort(p)
        self.telemetryCapture.start()
        
        # Add the Toolbar and Actions --------------------------

        toolbar = QtWidgets.QToolBar()
        toolbar.setIconSize(QSize(16, 16))
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.addToolBar(toolbar)

        # Status bar at the bottom of the application
        self.setStatusBar(QtWidgets.QStatusBar(self))

        # Action to configure capture settings - open a dialog to set port number, footage source etc
        configureCaptureAction = QAction(QIcon(str(parentDir / pathlib.Path("assets/icons/gear.png"))), "Capture Settings", self)
        configureCaptureAction.setStatusTip("Configure Capture Settings: Change the settings used for capturing race footage and telemetry.")
        #configureCaptureAction.triggered.connect(self.captureManager.openConfigureDialog)
        toolbar.addAction(configureCaptureAction)

        # Action to start or stop telemetry and footage capture
        toggleCaptureAction = QAction(QIcon(str(parentDir / pathlib.Path("assets/icons/control-record.png"))), "Capture", self)
        toggleCaptureAction.setStatusTip("Start/Stop Capture: Start/Stop capturing race footage and telemetry data.")
        toggleCaptureAction.setCheckable(True)
        #toggleCaptureAction.triggered.connect(self.captureManager.toggleCapture)
        toolbar.addAction(toggleCaptureAction)

        toolbar.addSeparator()

        # Action to open new sessions and replace any opened ones, to load the telemetry csv files and the associated mp4 video with the same name
        openNewSessionsAction = QAction(QIcon(str(parentDir / pathlib.Path("assets/icons/folder-open-document.png"))), "New Sessions", self)
        openNewSessionsAction.setShortcut(QKeySequence("Ctrl+O"))
        openNewSessionsAction.setStatusTip("Open New Sessions: Opens new CSV telemetry files (and video if there is one) to be analysed, replacing any currently opened sessions.")
        #openNewSessionsAction.triggered.connect(self.sessionManager.openNewSessions)
        toolbar.addAction(openNewSessionsAction)

        # Action to add sessions to be analysed
        addNewSessionsAction = QAction(QIcon(str(parentDir / pathlib.Path("assets/icons/folder--plus.png"))), "Add Sessions", self)
        #addNewSessionsAction.setShortcut(QKeySequence("Ctrl+O"))
        addNewSessionsAction.setStatusTip("Add Sessions: Adds new CSV telemetry files (and video if there is one) to be analysed.")
        #openNewSessionsAction.triggered.connect(self.sessionManager.openNewSessions)
        toolbar.addAction(addNewSessionsAction)

        toolbar.addSeparator()

        # Action to play/pause the videos and animate the graphs
        playPauseAction = QAction(QIcon(str(parentDir / pathlib.Path("assets/icons/control-play-pause.png"))), "Play/Pause", self)
        playPauseAction.setCheckable(True)
        playPauseAction.setShortcut(QKeySequence("Space"))
        playPauseAction.setStatusTip("Play/Pause Button: Plays or pauses the footage and the telemetry graphs.")
        #playPauseAction.triggered.connect(self.videoPlayer.playPause)
        toolbar.addAction(playPauseAction)

        # Action to stop and skip to the beginning of the footage
        stopAction = QAction(QIcon(str(parentDir / pathlib.Path("assets/icons/control-stop.png"))), "Stop", self)
        stopAction.setStatusTip("Stop Button: Stops the footage and skips to the beginning.")
        #stopAction.triggered.connect(self.videoPlayer.stop)
        toolbar.addAction(stopAction)

        # Add a new toolbar for switching between different modes
        modeBar = QtWidgets.QToolBar()
        modeBar.setIconSize(QSize(16, 16))
        modeBar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.addToolBar(modeBar)

        # Action to switch to the analyse mode
        analyseModeAction = QAction(QIcon(str(parentDir / pathlib.Path("assets/icons/magnifier.png"))), "Analyse Mode", self)
        analyseModeAction.setStatusTip("Analyse Mode: Switch to Analyse Mode to view footage and telemetry from saved sessions.")
        #analyseModeAction.setCheckable(True)
        analyseModeAction.setChecked(True)
        analyseModeAction.triggered.connect(self.setModeAnalyse)
        modeBar.addAction(analyseModeAction)

        # Action to switch to the record mode
        captureModeAction = QAction(QIcon(str(parentDir / pathlib.Path("assets/icons/script-attribute-c.png"))), "Capture Mode", self)
        captureModeAction.setStatusTip("Capture Mode: Switch to Capture Mode to view live footage and telemetry.")
        #captureModeAction.setCheckable(True)
        captureModeAction.triggered.connect(self.setModeCapture)
        modeBar.addAction(captureModeAction)

        # Add the menu bar and connect actions ----------------------------
        menu = self.menuBar()

        fileMenu = menu.addMenu("&File")
        fileMenu.addAction(openNewSessionsAction)
        fileMenu.addAction(addNewSessionsAction)

        captureMenu = menu.addMenu("&Capture")
        captureMenu.addAction(configureCaptureAction)
        captureMenu.addAction(toggleCaptureAction)

        playbackMenu = menu.addMenu("&Playback")
        playbackMenu.addAction(playPauseAction)
        playbackMenu.addAction(stopAction)

        modeMenu = menu.addMenu("&Mode")
        modeMenu.addAction(analyseModeAction)
        modeMenu.addAction(captureModeAction)

        # Add the Dock widgets, eg. graph and data table ---------------------

        # Contains actions to open/close the dock widgets
        viewMenu = menu.addMenu("&View")
    
    def _onCloseTimerTimeout(self):
        """Refires a close event"""
        self._closeTimer.stop()
        logging.info("Closing")
        self.close()
    
    def closeEvent(self, event: QCloseEvent):
        """Closes if all processes are finished else closes all processes, sets a timer and tries again."""
        if self.telemetryCapture.isActive():
            self.telemetryCapture.stop()
            logging.info("Stopping Telemetry capture...")
            self._closeTimer.start(500)
            event.ignore()
        else:
            event.accept()

    def setModeAnalyse(self):
        """Switched to Analyse Mode"""
        if self.mode is not self.ModeIndex.AnalyseMode:
            self.stackedModes.setCurrentIndex(self.ModeIndex.AnalyseMode.value)
            self.mode = self.ModeIndex.AnalyseMode

    def setModeCapture(self):
        """Switched to Capture Mode"""
        if self.mode is not self.ModeIndex.CaptureMode:
            self.stackedModes.setCurrentIndex(self.ModeIndex.CaptureMode.value)
            self.mode = self.ModeIndex.CaptureMode

