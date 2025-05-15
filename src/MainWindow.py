from PyQt6 import QtWidgets, QtMultimedia
from PyQt6.QtCore import pyqtSlot, QThread, QObject, pyqtSignal, Qt, QSize, QUrl
from PyQt6.QtGui import QAction, QIcon, QKeySequence, QColor
from PyQt6.QtMultimedia import QMediaDevices, QCamera, QMediaCaptureSession, QCameraDevice, QCameraFormat
from PyQt6.QtMultimediaWidgets import QVideoWidget

import pyqtgraph as pg
import numpy as np
import pandas as pd

from fdp import ForzaDataPacket
import Utility

from time import sleep
import pathlib
import yaml
import logging
import select
import socket
from enum import Enum
from collections import OrderedDict
from abc import ABC, abstractmethod


class Session(QObject):
    """
    Stores the telemetry data and associated calculated data for a currently opened session. A session
    represents a single unit of time's worth of continuously logged packets saved as a csv file.
    """

    def __init__(self, data: pd.DataFrame, name: str, parent = None):
        super().__init__(parent = parent)
        self.data: pd.DataFrame = None  # The pandas DataFrame containing the data from the telemetry file
        self.name = name  # The name for the session

        self.addData(data = data)
    
    def addData(self, data: pd.DataFrame):
        """Updates the Session with new data from a pandas DataFrame. This expects that the data is from a single track only, and
        collected from a single uninterrupted sequence of laps (eg. the user did not restart in the middle of a session)"""
        self.data = data


class SessionManager(QObject):
    """Stores and manages all the currently opened Sessions"""

    def __init__(self, trackDetails: pd.DataFrame, parent = None):
        super().__init__(parent)

        self.trackDetails = trackDetails

        # A dictionary of currently opened Sessions. Each key is a session name as a string, and each value is a Session object
        self.sessions = dict()

    def openSessions(self):
        """Opens and loads the telemetry csv files into the sessions dict"""

        # Dialog to get the csv files
        dlg = QtWidgets.QFileDialog(self.parent())
        dlg.setWindowTitle("Open Sessions")
        dlg.setFileMode(QtWidgets.QFileDialog.FileMode.ExistingFiles)
        dlg.setNameFilter("*.csv")

        # If user presses okay
        if dlg.exec():
            filePathList = dlg.selectedFiles()
            logging.info("Found Files: {}".format(filePathList))

            for filePath in filePathList:
                # Read the csv file into a pandas DataFrame and attempt to add the sessions contained within to the Session Manager
                data = pd.read_csv(filePath)
                fileName = pathlib.Path(filePath).resolve().stem
                self._addSessions(data, fileName)
    
    def _isColumnUnique(series: pd.Series):
        """Checks if each value in a Pandas Series is equal."""
        n = series.to_numpy()
        return (n[0] == n).all()
    
    def _sortData(data: pd.DataFrame):
        """Sorts the DataFrame in-place based on the timestamp_ms field. This is Forza's internal timestamp and will ensure rows will be sorted
        by the order they were produced, instead of the order they were received as UDP may be unreliable. This is an unsigned 32 bit int, so
        can overflow after about 50 days. This will detect an overflow and re-sort the fields to restore true chronological order."""

        threshold = 3600000  # If the gap between adjacent timestamps is larger than this, an overflow has occurred (3600000ms is about an hour)
        #threshold = 1000  # For testing
        
        # Sort based on timestamp
        data.sort_values(by="timestamp_ms", inplace=True, kind='mergesort')

        # Iterate through array and try to detect an overflow
        overflowIndex = -1
        for i in range(1, len(data["timestamp_ms"])):
            if data["timestamp_ms"].loc(i) - data["timestamp_ms"].loc(i - 1) > threshold:
                overflowIndex = i
                break
        
        # If the overflow index has changed, meaning an overflow has been detected at this index:
        if overflowIndex != -1:

            # Find the max value for timestamp_ms (Should be at the end)
            maxTimestamp = data["timestamp_ms"].max()

            # Add the max value + 1 to each of the timestamps after the overflow
            for i in range(0, overflowIndex):
                data["timestamp_ms"].loc(i) += maxTimestamp + 1
            
            # Re-sort the array in-place to put those rows after the overflow at the back again
            data.sort_values(by="timestamp_ms", inplace=True, kind='mergesort')
    
    def _flip(restartNumber: int, restartFlag: bool) -> int:
        """Special function used to help assign restart numbers to session telemetry data"""
        restartFlag = not restartFlag
        restartNumber += 1
        return restartNumber

    def _addSessions(self, data: pd.DataFrame, sessionName: str):
        """Adds the sessions containined within the data frame to the Session Manager. A single session will be determined by when the user
        restarts a race or skips a lap."""

        # A single CSV telemetry file could span multiple restarts. As long as it contains data from only one track, this will split the
        # data into multiple usable sessions. Each session will contain at least one whole lap of the circuit, regardless of car used.

        # Data needs to be verified first to check that it's all within a single track (All packet track_ordinal values need to be equal)
        if not self._isColumnUnique(data["track_ordinal"]):
            raise ValueError('Cannot load session: contains telemetry data from more than one track. Split the data into separate files and try again.')

        # Data needs to be sorted based on Forza's internal timestamp (timestamp_ms), as the order it currently is in may not be reliable
        # (Eg. ordered based on time of collection when UDP is not reliable). As the timestamp may overflow back to 0, this also needs to
        # be accounted for
        SessionManager._sortData(data)

        # Data needs to be split into restarts
        # Each restart will ALWAYS start with a negative dist_traveled packet as forza always starts a car behind the start-finish line,
        # UNLESS the player stared recording telemetry in the middle of a lap.
        # Splitting the data into sections, with each section beginning at the FIRST packet with a negative dist_traveled and ending with
        # the packet just before a negative packet will correctly split the data. Lap number CANNOT be used here, as it is not reset after
        # a 'skip lap' is triggered, therefore becoming invalid as the game displays a different lap number to data out.
        restartNumber = 0
        restartFlag = True if data["dist_traveled"][0] > 0 else False  # Set to true if the player started recording mid lap

        # Create a new column 'restart' that groups each row into a restart
        data["restart"] = [restartNumber if (restartFlag and dist >= 0 or not restartFlag and dist < 0) else SessionManager._flip(restartNumber, restartFlag) for dist in data["dist_traveled"]]

        # Restarts can be discarded if they have less than one complete lap
        # ie. if the only unique lap in that restart is 0, and the last row's dist_traveled value is much less than the track's distance

        # Create a new session for each valid restart, leaving any incomplete laps in as they can still be used to get an accurate lap time for the previous lap
        tempSessions = dict()
        for r in range(0, restartNumber + 1):
            restartData = data[data["restart"] == r]
            newSession = Session(restartData)

        # The last lap in each restart can be discarded if it is incomplete, but not before the last_lap_time value has been recorded.
        # This is needed to set an accurate lap time for the previous lap


class SessionOverviewWidget(QtWidgets.QWidget):
    """Displays data about all the currently opened Sessions and all the laps in each Session"""

    # Emitted when the user wants to toggle focus on a lap (add to the graphs and display the video), by checking the
    # checkbox. Will emit with the session name as a str, lap number as an int, and whether the user is toggling
    # on or off, as a CheckState class
    toggleLapFocus = pyqtSignal(str, int, Qt.CheckState)


    class SessionViewerWidget(QtWidgets.QGroupBox):
        """Displays data about a single Session"""


        class LapCheckBox(QtWidgets.QCheckBox):
            
            # Emitted when the checkbox is clicked. Contains the lap number as an int, and the checkstate
            toggleLapFocus = pyqtSignal(int, Qt.CheckState)

            def __init__(self, lapNumber: int, parent = None):
                super().__init__(parent = parent)
                self.lapNumber = lapNumber
                self.stateChanged.connect(self._emitToggleSignal)
            
            def _emitToggleSignal(self):
                self.toggleLapFocus.emit(self.lapNumber, self.checkState())


        # Emitted when the user wants to toggle focus on a lap (add to the graphs and display the video), by checking the
        # checkbox. Will emit with the session name as a str, lap number as an int, and whether the user is toggling
        # on or off, as a CheckState class
        toggleLapFocus = pyqtSignal(str, int, Qt.CheckState)

        def __init__(self, session: Session, parent = None):
            super().__init__(title = session.name, parent = parent)

            self.sessionName = session.name
            self.lapCheckBoxes = []  # A list of all the LapCheckBox widgets

            layout = QtWidgets.QGridLayout()
            self.setLayout(layout)

            # Add the header
            layout.addWidget(QtWidgets.QLabel(text="Lap", parent=self), 0, 0)
            layout.addWidget(QtWidgets.QLabel(text="Show", parent=self), 0, 1)
            layout.addWidget(QtWidgets.QLabel(text="Lap Time", parent=self), 0, 2)

            self._row = 1  # The next free row to add a lap into
            for lap in session.laps:
                self.addLap(lap)
        
        def lapToggled(self, lapNumber: int, checkState: Qt.CheckState):
            self.toggleLapFocus.emit(self.sessionName, lapNumber, checkState)
            
        def addLap(self, lap: np.ndarray):
            """Adds a new lap as a new row into the viewer widget"""
            self.layout().addWidget(QtWidgets.QLabel(text=str(lap["lap_no"]), parent=self), self._row, 0)
            lapCheckBox = SessionOverviewWidget.SessionViewerWidget.LapCheckBox(lap["lap_no"], self)
            self.lapCheckBoxes.append(lapCheckBox)
            lapCheckBox.toggleLapFocus.connect(self.lapToggled)
            self.layout().addWidget(lapCheckBox, self._row, 1)
            self.layout().addWidget(QtWidgets.QLabel(text=Utility.formatLapTime(lap["lap_time"]), parent=self), self._row, 2)
            self._row += 1
        

    def __init__(self, parent = None):
        super().__init__(parent)
        self.openSessions = dict()  # Session Name (str) : Session tab (SessionViewerWidget)
        self.focusedLapsNumber = 0  # Number of currently focused laps

        layout = QtWidgets.QVBoxLayout()
        self.setLayout(layout)
    
    def addSession(self, session: Session):
        """Adds a new session to the overview"""
        tab = SessionOverviewWidget.SessionViewerWidget(session)
        tab.toggleLapFocus.connect(self._toggleLapFunction)
        self.openSessions[session.name] = tab
        self.layout().addWidget(tab)
    
    def reset(self):
        """Resets the widget and clears the displayed sessions"""
        for sessionTab in self.openSessions.values():
            self.layout().removeWidget(sessionTab)
        self.openSessions.clear()
    
    def _toggleLapFunction(self, sessionName: str, lapNumber: int, checkState: Qt.CheckState):
        """Sends a signal that a lap has been toggled, and enables/disables the checkboxes if the maximum focused laps have been reached"""

        self.toggleLapFocus.emit(sessionName, lapNumber, checkState)

        if checkState is Qt.CheckState.Checked:
            self.focusedLapsNumber += 1
            if self.focusedLapsNumber == 6:  # Max focused laps have been reached
                # Disable all the unchecked checkboxes
                for sessionWidget in self.openSessions.values():
                    for checkBox in sessionWidget.lapCheckBoxes:
                        if checkBox.checkState() is Qt.CheckState.Unchecked:
                            checkBox.setEnabled(False)
        else:
            if self.focusedLapsNumber == 6:
                # Enable all the checkboxes
                for sessionWidget in self.openSessions.values():
                    for checkBox in sessionWidget.lapCheckBoxes:
                        checkBox.setEnabled(True)

            self.focusedLapsNumber -= 1

    def updateColour(self, focusedLapsDict):
        """Updates the colour of the checkboxes of the focused laps"""
        pass


class MultiPlotWidget(QtWidgets.QFrame):
    """Displays multiple plots generated from the session data."""

    def __init__(self, parent = ..., flags = ...):
        super().__init__(parent, flags)


class AbstractPlot(pg.PlotWidget, ABC):
    """An abstract plot base class that can be used with the MultiPlotWidget"""

    def __init__(self, parent=None, background='default', plotItem=None, **kargs):
        super().__init__(parent, background, plotItem, **kargs)
    

class MainWindow(QtWidgets.QMainWindow):

    def __init__(self):
        super().__init__()

        parentDir = pathlib.Path(__file__).parent.parent.resolve()

        # A DataFrame containing all the track details
        self.forzaTrackDetails = pd.read_csv(parentDir / pathlib.Path("track-details.csv"))
        self.sessionManager = SessionManager(self.forzaTrackDetails, self)

        # Central widget ----------------------

        # Add the Toolbar and Actions --------------------------

        toolbar = QtWidgets.QToolBar()
        toolbar.setIconSize(QSize(16, 16))
        self.addToolBar(toolbar)

        # Status bar at the bottom of the application
        self.setStatusBar(QtWidgets.QStatusBar(self))

        # Action to open a new session, to load the telemetry csv files and the associated mp4 video with the same name
        openSessionAction = QAction(QIcon(str(parentDir / pathlib.Path("assets/icons/folder-open-document.png"))), "Open Session", self)
        openSessionAction.setShortcut(QKeySequence("Ctrl+O"))
        openSessionAction.setStatusTip("Open Session: Opens a CSV telemetry file (and video if there is one) to be analysed.")
        openSessionAction.triggered.connect(self.sessionManager.openSessions)
        toolbar.addAction(openSessionAction)

        # Add the menu bar and connect actions ----------------------------
        menu = self.menuBar()

        fileMenu = menu.addMenu("&File")
        fileMenu.addAction(openSessionAction)

        # Contains actions to open/close the dock widgets
        viewMenu = menu.addMenu("&View")

        # Add the Dock widgets, eg. graph and data table ---------------------

        # Session Data Viewer widget
        sessionOverviewWidget = SessionOverviewWidget(self)

        sessionScrollArea = QtWidgets.QScrollArea()  # Put the plots in this to make it scrollable
        sessionScrollArea.setWidget(sessionOverviewWidget)
        sessionScrollArea.setWidgetResizable(True)
        sessionScrollArea.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)

        sessionOverviewDockWidget = QtWidgets.QDockWidget("Session Overview", self)
        sessionOverviewDockWidget.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)
        sessionOverviewDockWidget.setWidget(sessionScrollArea)
        sessionOverviewWidget.setStatusTip("Session Overview: View the select which laps to focus on from each session.")
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, sessionOverviewDockWidget)
        self.sessionManager.sessionLoaded.connect(sessionOverviewWidget.addSession)
        sessionOverviewWidget.toggleLapFocus.connect(self.sessionManager.lapFocusToggle)
        self.sessionManager.sessionReset.connect(sessionOverviewWidget.reset)
        self.sessionManager.focusedLapsChanged.connect(sessionOverviewWidget.updateColour)
