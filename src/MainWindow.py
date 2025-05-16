from PyQt6 import QtWidgets, QtMultimedia
from PyQt6.QtCore import pyqtSlot, QThread, QObject, pyqtSignal, Qt, QSize, QUrl, QAbstractTableModel
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


class SessionManager(QAbstractTableModel):
    """Stores and manages all the currently opened Sessions"""

    updated = pyqtSignal()  # Emitted when the data is updated with new sessions

    def __init__(self, trackDetails: pd.DataFrame, parent = None):
        super().__init__(parent)

        self.trackDetails: pd.DataFrame = trackDetails  # A DataFrame containing details about each track in the game (eg. track ordinal, length etc)
        self.data: pd.DataFrame | None = None  # The data containing all the currently loaded sessions, restarts and laps
        self.numberOfSessions: int = 0  # The number of sessions currently represented by the data
        self.trackOrdinal: int | None = None
        self.summaryTable: pd.DataFrame | None = None

    def data(self, index, role):
        if role == Qt.ItemDataRole.DisplayRole:
            if self.data is None:
                return None
            value = self.data.iloc[index.row(), index.column()]
            return str(value)
    
    def rowCount(self, index):
        if self.data is None:
            return 0
        return self.data.shape[0]

    def columnCount(self, index):
        if self.data is None:
            return 0
        return self.data.shape[1]

    def headerData(self, section, orientation, role):
        # section is the index of the column/row.
        if role == Qt.ItemDataRole.DisplayRole:
            if orientation == Qt.Orientation.Horizontal:
                return str(self.data.columns[section])

            if orientation == Qt.Orientation.Vertical:
                return str(self.data.index[section])

    def openNewSessions(self):
        """Opens and loads the telemetry csv files"""

        # Dialog to get the csv files
        dlg = QtWidgets.QFileDialog(self.parent())
        dlg.setWindowTitle("Open Sessions")
        dlg.setFileMode(QtWidgets.QFileDialog.FileMode.ExistingFiles)
        dlg.setNameFilter("*.csv")
        
        # If user presses okay
        if dlg.exec():
            filePathList = dlg.selectedFiles()
            logging.info("Found Files: {}".format(filePathList))

            tempSessionData = None  # Temporarily load sessions into here so the current sessions aren't overwritten before all files have been checked
            tempNumberOfSessions = 0
            tempTrackOrdinal = None
            data = None
            fileName = None

            for filePath in filePathList:
                # Read the csv file into a pandas DataFrame and attempt to add the sessions contained within to the Session Manager
                try:
                    data = pd.read_csv(filePath, skip_blank_lines=True)

                    # For now, just drop any row that contains an empty value in any field. In future, this could be sophisticated by only
                    # dropping rows that have NA values in required columns, or even replacing NA values with guesses/zeros or something useful.
                    data.dropna()

                    fileName = pathlib.Path(filePath).resolve().stem
                    self._processFile(data, fileName, tempNumberOfSessions)
                except Exception as e:
                    QtWidgets.QMessageBox.critical(self.parent(), "Error", str(e))
                    return

                # If this is the first file to be loaded into the Session Manager, use it to initialise useful values such as
                # the number of sessions, details about the track etc.
                # If there are any existing sessions, check that the new sessions are compatible, eg. track ordinal is the same etc
                if tempNumberOfSessions == 0:
                    tempNumberOfSessions = data["session_no"].max() + 1
                    tempTrackOrdinal = data["track_ordinal"][0]
                    tempSessionData = data
                else:
                    if tempTrackOrdinal != data["track_ordinal"][0]:
                        raise ValueError('Cannot load sessions from {}: contains telemetry data from a different track.'.format(filePath))
                    tempSessionData = pd.concat([tempSessionData, data], axis=0, ignore_index=True)
                    tempNumberOfSessions += data["session_no"].max() + 1
            
            # All sessions were loaded successfull, now replace the currently loaded sessions with new ones
            self.beginResetModel()
            self.data = tempSessionData
            self.trackOrdinal = tempTrackOrdinal
            self.numberOfSessions = tempNumberOfSessions
            self.summaryTable = SessionManager._generateSummaryTable(self.data)

            self.updated.emit()  # Emit the updated signal after all the files have been uploaded
            self.endResetModel()
    
    @staticmethod
    def _generateSummaryTable(data: pd.DataFrame) -> pd.DataFrame:
        """
        Returns a summary table of the sessions, restarts and laps in the 'data' DataFrame.

        This table will be made of the following columns:
        filename, session_no, restart_no, lap_no, lap_time

        So it will contain the lap time for each lap, in each restart, in each session, in each file.
        """
        summary = data.groupby(["filename", "session_no", "restart_no", "lap_no"])["cur_lap_time"].last()
        logging.info("Summary: \n{}".format(summary))
        return summary
                    
    @staticmethod
    def _isColumnUnique(series: pd.Series):
        """Checks if each value in a Pandas Series is equal."""
        n = series.to_numpy()
        return (n[0] == n).all()
    
    @staticmethod
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
            if data["timestamp_ms"][i] - data["timestamp_ms"][i - 1] > threshold:
                overflowIndex = i
                break
        
        # If the overflow index has changed, meaning an overflow has been detected at this index:
        if overflowIndex != -1:

            # Find the max value for timestamp_ms (Should be at the end)
            maxTimestamp = data["timestamp_ms"].max()

            # Add the max value + 1 to each of the timestamps after the overflow
            for i in range(0, overflowIndex):
                #data["timestamp_ms"].loc(i) += maxTimestamp + 1
                data["timestamp_ms"] = [x + maxTimestamp + 1 for x in data["timestamp_ms"]]
            
            # Re-sort the array in-place to put those rows after the overflow at the back again
            data.sort_values(by="timestamp_ms", inplace=True, kind='mergesort')

    def _processFile(self, data: pd.DataFrame, fileName: str, numberOfExistingSessions = 0):
        """Sorts, cleans, formats and groups the telemetry data within the 'data' DataFrame so it can be added to the Session Manager"""

        #logging.info("Unsorted Data:\n{}\n{}\n".format(data.head(), data.tail()))

        # A Session is defined as a continuous period of practice at a single track with a single car. Sessions organise telemetry data into
        # a sequence of restarts.
        # A Restart is defined as a continuous AND uninterrupted sequence of complete laps, increasing in lap number. To trigger a restart, a player
        # may restart a race, or skip a lap. Both options will reset the player's vehicle behind the start-finish line to begin a new lap.
        # A Lap is complete if the player began recording telemetry at or before the start line, and finished recording telemetry data at or
        # after the finish line. If telemetry begins or ends in the middle of a lap, it is not complete.

        # A single CSV telemetry file could contain multiple Sessions. As long as it contains data from only one track, this will split the
        # data into multiple sessions, with each session containing at least one restart. Each restart will contain at least one whole lap
        # of the circuit.

        # Data needs to be verified first to check that it's all within a single track (All track_ordinal values need to be equal)
        # and that it contains at least the most basic fields required for this app to work
        requiredFields = ["timestamp_ms", "car_ordinal", "dist_traveled", "last_lap_time",
                       "cur_lap_time", "lap_no", "track_ordinal"]
        missingFields = [field for field in requiredFields if field not in data.columns]
        if len(missingFields) > 0:
            errorMessage = "Couldn't load file {} because of missing fields: {}".format(fileName, missingFields)
            raise ValueError(errorMessage)
        
        if not self._isColumnUnique(data["track_ordinal"]):
            if data["track_ordinal"].hasnans:
                raise ValueError('Cannot load {}: track_ordinal field has missing values. Replace them and try again.'.format(fileName))
            raise ValueError('Cannot load {}: contains telemetry data from more than one track. Split the data into separate files and try again.'.format(fileName))

        # Data needs to be sorted based on Forza's internal timestamp (timestamp_ms), as the order it currently is in may not be reliable
        # (Eg. ordered based on time of collection when UDP is not reliable). As the timestamp may overflow back to 0, this also needs to
        # be accounted for
        SessionManager._sortData(data)
        #logging.info("Sorted Data:\n{}\n{}\n".format(data.head(), data.tail()))

        # Data needs to be split into Sessions. A new session begins when the car is changed.
        # Create a new field called 'session_no', starting at 0
        # Everytime the player changes car, increment the session_no
        currentSessionNumber = numberOfExistingSessions - 1  # If there are sessions already loaded in Session Manager, start the session count at the next available session number
        prevCarOrdinal = -1
        sessionNumberList = []
        for val in data["car_ordinal"]:
            if val != prevCarOrdinal:
                currentSessionNumber += 1
                prevCarOrdinal = val
            sessionNumberList.append(currentSessionNumber)
        data["session_no"] = sessionNumberList

        # Data needs to be split into restarts. Each restart will always begin with a negative dist_traveled value, unless the player began
        # recording telemetry in the middle of a lap.
        # Splitting the data into sections, with each section beginning at the FIRST packet with a negative dist_traveled and ending with
        # the last positive packet will correctly split the data. Lap number CANNOT be used here, as it is not reset after
        # a 'skip lap' is triggered, therefore becoming invalid as the game displays a different lap number to data out.
        prevDistanceWasPositive = True
        currentSessionNumber = -1
        currentRestartNo = -1
        restartNumberList = []
        for sessionNo, distTraveled in zip(data["session_no"], data["dist_traveled"]):
            if sessionNo != currentSessionNumber:  # If we're looking at a new session, reset the restart number
                currentSessionNumber = sessionNo
                currentRestartNo = -1

            if prevDistanceWasPositive and distTraveled < 0:  # Distance has gone from positive to negative indicating a restart
                prevDistanceWasPositive = False
                currentRestartNo += 1
            
            prevDistanceWasPositive = True if distTraveled >= 0 else False
            restartNumberList.append(currentRestartNo)
        
        data["restart_no"] = restartNumberList

        # At this point, session_no and restart_no columns have been added to group each data packet into a session, restart, and lap within that restart

        # Add a filename column to identify which file the sessions came from
        data["filename"] = [fileName for i in range(0, data.shape[0])]

        # Restarts can be discarded if they have less than one complete lap
        # ie. if the only unique lap in that restart is 0, and the last row's dist_traveled value is much less than the track's distance

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
    

class MainWindow(QtWidgets.QMainWindow):

    def __init__(self):
        super().__init__()

        parentDir = pathlib.Path(__file__).parent.parent.resolve()

        # A DataFrame containing all the track details
        trackDetailsPath = parentDir / pathlib.Path("track-details.csv")
        self.forzaTrackDetails = pd.read_csv(str(trackDetailsPath), index_col="ordinal")

        # Stores the data for all currently loaded sessions in a DataFrame
        self.sessionManager = SessionManager(self.forzaTrackDetails, self)
        self.sessionManager.updated.connect(self.update)

        # Central widget ----------------------
        #self.centreLabel = QtWidgets.QLabel(self, text="No status yet")
        #self.setCentralWidget(self.centreLabel)

        self.table = QtWidgets.QTableView()
        self.table.setModel(self.sessionManager)
        self.setCentralWidget(self.table)

        # Add the Toolbar and Actions --------------------------

        toolbar = QtWidgets.QToolBar()
        toolbar.setIconSize(QSize(16, 16))
        self.addToolBar(toolbar)

        # Status bar at the bottom of the application
        self.setStatusBar(QtWidgets.QStatusBar(self))

        # Action to open a new session, to load the telemetry csv files and the associated mp4 video with the same name
        openNewSessionsAction = QAction(QIcon(str(parentDir / pathlib.Path("assets/icons/folder-open-document.png"))), "Open New Sessions", self)
        openNewSessionsAction.setShortcut(QKeySequence("Ctrl+O"))
        openNewSessionsAction.setStatusTip("Open New Sessions: Opens new CSV telemetry files (and video if there is one) to be analysed.")
        openNewSessionsAction.triggered.connect(self.sessionManager.openNewSessions)
        toolbar.addAction(openNewSessionsAction)

        # Add the menu bar and connect actions ----------------------------
        menu = self.menuBar()

        fileMenu = menu.addMenu("&File")
        fileMenu.addAction(openNewSessionsAction)

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
    
    def update(self):
        """Updates the widgets with new information from SessionManager"""
        
        # Get the track details
        # Get the head and tail

        #trackSummary = str(self.sessionManager.trackDetails.loc[int(self.sessionManager.trackOrdinal)])
        #summary = self.sessionManager.summaryTable

        #self.centreLabel.setText(trackSummary + "\n" + str(summary.head) + "\n" + str(summary.tail))
        self.sessionManager.data.to_csv("test.csv")
        pass
