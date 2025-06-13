from PyQt6 import QtWidgets, QtMultimedia
from PyQt6.QtCore import pyqtSlot, QThread, QObject, pyqtSignal, Qt, QSize, QUrl, QAbstractTableModel, QItemSelection, QModelIndex, QRunnable, QThreadPool, QTimer
from PyQt6.QtGui import QAction, QIcon, QKeySequence, QColor, QStandardItemModel, QStandardItem, QPixmap, QPen, QCloseEvent, QGuiApplication
from PyQt6.QtMultimedia import QMediaDevices, QCamera, QMediaCaptureSession, QCameraDevice, QCameraFormat, QScreenCapture, QWindowCapture, QMediaRecorder
from PyQt6.QtMultimediaWidgets import QVideoWidget

import pyqtgraph as pg
import numpy as np
import pandas as pd

import distinctipy
from fdp import ForzaDataPacket
from Utility import ForzaSettings
from CaptureMode import CaptureModeWidget, CaptureManager, TelemetryCapture, FootageCapture, TelemetryManager, TelemetryDSVFilePersistence, CaptureDialog
from Settings import SettingsManager

import csv
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

        # Get the details of the directory to save telemetry and footage to
        parentFolder = settingsManager.get("common", "parentFolder", default="default")
        if parentFolder == "default":
            parentFolder = pathlib.Path().home()
        else:
            parentFolder = pathlib.Path(parentFolder)
        folderName = settingsManager.get("common", "folderName", default="forza-analyse")
        saveDirectory = parentFolder / pathlib.Path(folderName)

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

        # Set up the footage capture using the directories from the settings file
        self.footageCapture = FootageCapture(footageDirectory=str(saveDirectory.resolve()))

        # Add display widgets for each mode
        self.analyseMode = AnalyseModeWidget()
        self.captureMode = CaptureModeWidget()
        self.footageCapture.setVideoPreview(self.captureMode.getVideoPreview())

        # Create a stacked widget - each child is a different mode (eg. analyse, record)
        self.stackedModes = QtWidgets.QStackedWidget()
        vbLayout.addWidget(self.stackedModes)
        self.stackedModes.addWidget(self.analyseMode)
        self.stackedModes.addWidget(self.captureMode)
        self.mode = self.ModeIndex.AnalyseMode
        self.setModeCapture()

        # Set up telemetry capture and try to start capturing
        p = settingsManager.get("recording", "port", default=7676)
        self.telemetryCapture = TelemetryCapture()
        self.telemetryCapture.signals.statusChanged.connect(self.captureStatus.setTelemetryCaptureStatus)
        self.telemetryCapture.signals.portChanged.connect(self.captureStatus.setPort)
        self.telemetryCapture.setPort(p)
        self.telemetryCapture.start()

        # Set up the telemetry persistence
        self.telemetryPersistence = TelemetryDSVFilePersistence()
        self.telemetryPersistence.setPath(str(saveDirectory))

        # Add and configure the Manager objects
        self.captureManager = CaptureManager()
        self.telemetryManager = TelemetryManager()
        self.telemetryManager.setTelemetryCapture(self.telemetryCapture)
        self.telemetryManager.setTelemetryPersistence(self.telemetryPersistence)
        self.captureManager.setTelemetryManager(self.telemetryManager)
        self.captureManager.setFootageCapture(self.footageCapture)
        
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
        configureCaptureAction.triggered.connect(self.configureCaptureSettings)
        toolbar.addAction(configureCaptureAction)

        # Action to start or stop telemetry and footage recording (Actually saving to files, not just capturing packets)
        toggleCaptureAction = QAction(QIcon(str(parentDir / pathlib.Path("assets/icons/control-record.png"))), "Capture", self)
        toggleCaptureAction.setStatusTip("Start/Stop Capture: Start/Stop capturing race footage and telemetry data.")
        toggleCaptureAction.setCheckable(True)
        toggleCaptureAction.triggered.connect(self.captureManager.toggleCapture)
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
    
    def configureCaptureSettings(self):
        """Opens a dialog to configure capture settings and applies them if accepted"""
        captureDialog = CaptureDialog()
        if captureDialog.exec():
            pass
    
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

