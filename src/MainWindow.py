from PyQt6 import QtWidgets, QtMultimedia
from PyQt6.QtCore import pyqtSlot, QThread, QObject, pyqtSignal, Qt, QSize, QUrl, QAbstractTableModel, QItemSelection, QModelIndex
from PyQt6.QtGui import QAction, QIcon, QKeySequence, QColor, QStandardItemModel, QStandardItem, QPixmap
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
import random
from enum import Enum
from collections import OrderedDict
from abc import ABC, abstractmethod
from typing import Literal


class DataFrameModel(QAbstractTableModel):
    """A Table Model representing a pandas DataFrame"""

    def __init__(self, parent = None):
        super().__init__(parent)

        # The data that the model will represent
        self.frame: pd.DataFrame | None = None
    
    def data(self, index, role):
        if role == Qt.ItemDataRole.DisplayRole:
            if self.frame is None:
                return None
            value = self.frame.iat[index.row(), index.column()]
            return str(value)
    
    def rowCount(self, index):
        if self.frame is None:
            return 0
        return self.frame.shape[0]

    def columnCount(self, index):
        if self.frame is None:
            return 0
        return self.frame.shape[1]

    def headerData(self, section, orientation, role):
        # section is the index of the column/row.
        if role == Qt.ItemDataRole.DisplayRole:
            if orientation == Qt.Orientation.Horizontal:
                label = str(self.frame.columns[section]).replace("_", " ").title()
                return label

            if orientation == Qt.Orientation.Vertical:
                return str(self.frame.index[section])

    def updateData(self, data: pd.DataFrame):
        """Replaces the data currently held in the model with a copy of the supplied DataFrame"""
        self.beginResetModel()
        self.frame = data.copy()
        self.endResetModel()

    def getDataFrame(self):
        """Returns the DataFrame"""
        return self.frame


class LapDetailsModel(DataFrameModel):
    def __init__(self, parent=None):
        super().__init__(parent)
    
    def data(self, index: QModelIndex, role):

        if self.frame is None:
                return None
        
        if role == Qt.ItemDataRole.DisplayRole:
            value = self.frame.iat[index.row(), index.column()]
            if index.column() == 4:
                value = Utility.formatLapTime(value)
            return str(value)
        
        if role == Qt.ItemDataRole.BackgroundRole:
            if index.column() == 4:
                value = self.frame.iat[index.row(), index.column()]
                minLapTime = self.frame["lap_time"].min()
                if value == minLapTime:
                    return QColor("purple")


class SessionManager(QObject):
    """Stores and manages all the currently opened Sessions"""

    updated = pyqtSignal()  # Emitted when the data is updated with new sessions

    def __init__(self, trackDetails: pd.DataFrame, parent = None):
        super().__init__(parent)

        self.trackDetails: pd.DataFrame = trackDetails  # A DataFrame containing details about each track in the game (eg. track ordinal, length etc)
        self.telemetry: DataFrameModel = DataFrameModel()  # The data containing all the currently loaded sessions, restarts and laps
        self.numberOfSessions: int = 0  # The number of sessions currently represented by the data
        self.trackOrdinal: int | None = None
        
        # A DataFrame containing important details about each lap from the telemetry data that the player completed. It contains:
        # Lap number, restart number, session number, filename, lap time, 
        self.lapDetails: LapDetailsModel = LapDetailsModel()
    
    def _updateLapDetails(self):
        """Creates a new DataFrame with important details about each completed lap"""

        lapDistance = self.trackDetails.at[self.trackOrdinal, "length"]
        
        def filterFunc(x, lapDistance):
            """Takes a group of packets from a single lap, and returns True if that lap is valid. A lap is valid if the
            last packet's distance was close to the length of the track lap."""
            tolerance = 5  # metres either side of the lap distance is considered a valid lap
            distanceManaged = x["cur_lap_distance"].iat[-1]
            return True if distanceManaged > lapDistance - tolerance and distanceManaged < lapDistance + tolerance else False

        # Remove the incomplete laps from the telemetry data
        summary = self.telemetry.getDataFrame().groupby(["filename", "session_no", "restart_no", "lap_no"]).filter(lambda x : filterFunc(x, lapDistance))
        summary = summary.groupby(["filename", "session_no", "restart_no", "lap_no"])["cur_lap_time"].last()
        self.lapDetails.updateData(summary.reset_index().rename(columns={'cur_lap_time': 'lap_time'}))

    def updateLapSelection(self, selected: QItemSelection, deselected: QItemSelection):
        """Update the current lap colours dictionary"""
        logging.info(f"Selected: {[s.data() for s in selected.indexes()]}")
        logging.info(f"Selected: {[f"{s.row()}, {s.column()}" for s in selected.indexes()]}")

        logging.info(f"Deselected: {[s.data() for s in deselected.indexes()]}")
        logging.info(f"Deselected: {[f"{s.row()}, {s.column()}" for s in deselected.indexes()]}")
        return
        
        for index in selected.indexes():
            item = index.model().itemFromIndex(index)
            icon = Utility.BlockColourIcon(self.colourPicker.pick())
            item.setIcon(icon)
            logging.info(f"Setting: {icon}")
        
        for index in deselected.indexes():
            item = index.model().itemFromIndex(index)
            item.setIcon(QIcon())  # Remove the icon by replacing it with a blank QIcon

            # Get the lap number, restart no, session no and filename

    def getLapData(self, filename, session_no, restart_no, lap_no, includeNegativeDistance = False):
        """
        Returns all the rows from a single lap when given the lap number, restart number,
        session number and filename of the lap. All 4 of these parameters together uniquely identify a lap.

        Paramaters
        ----------
        filename : The value to look for in the 'filename' field
        session_no : The value to look for in the 'session_no' field
        restart_no : The value to look for in the 'restart_no' field
        lap_no : The value to look for in the 'lap_no' field
        includeNegativeDistance : If True, rows that contain a negative value for dist_traveled will be returned.
        """
        frame = self.telemetry.getDataFrame()
        if includeNegativeDistance:
            return frame.loc[(frame["filename"] == filename) & (frame["session_no"] == session_no) & (frame["restart_no"] == restart_no) & (frame["lap_no"] == lap_no)]
        else:
            return frame.loc[(frame["filename"] == filename) & (frame["session_no"] == session_no) & (frame["restart_no"] == restart_no) & (frame["lap_no"] == lap_no) & (frame["dist_traveled"] >= 0)]

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
                    #data.dropna(inplace=True)

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
            self.telemetry.updateData(tempSessionData.reset_index())
            self.trackOrdinal = tempTrackOrdinal
            self.numberOfSessions = tempNumberOfSessions

            # Update the lap structure model using the new telelemtry DataFrame
            #self._updateLapStructureModel()

            # Update the lap details DataFrame
            self._updateLapDetails()

            self.updated.emit()  # Emit the updated signal after all the files have been uploaded and all data models have been updated
                    
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

        # Add a cur_lap_distance field to show how far around the lap a player has traveled. This is computed from the
        # dist_traveled field, and the length of the track from the trackDetails DataFrame.
        trackOrdinal = int(data["track_ordinal"][0])
        
        # Make sure that track is in the trackDetails DataFrame. If not, the user needs to update the track-details.csv file with the new track.
        assert trackOrdinal in self.trackDetails.index, f"Track {trackOrdinal} could not be located. Make sure the track-details.csv file is up to date."

        # Get the length of a lap
        trackLength = self.trackDetails.at[trackOrdinal, "length"]

        # Create the new column using modulo, unless dist_traveled is negative then set to 0
        data["cur_lap_distance"] = [curDistance % trackLength if curDistance >= 0 else 0 for curDistance in data["dist_traveled"]]

        # Restarts can be discarded if they have less than one complete lap
        # ie. if the only unique lap in that restart is 0, and the last row's dist_traveled value is much less than the track's distance

        # The last lap in each restart can be discarded if it is incomplete, but not before the last_lap_time value has been recorded.
        # This is needed to set an accurate lap time for the previous lap


class MultiPlotWidget(pg.GraphicsLayoutWidget):
    """Displays multiple plots generated from the session data."""

    _PlotTypes = Literal["delta", "simple"]

    def __init__(self, parent=None, show=False, size=None, title=None, **kargs):
        super().__init__(parent, show, size, title, **kargs)

        self.sessionManager: SessionManager | None = None
        self.plots = []
        self.laps = []  # A list of displayed laps, organised into tuples of (filename, sessionNo, restartNo, lapNo)
    
    def setSessionManager(self, sessionManager: SessionManager):
        """Sets the session manager to get telemetry data from. Resets the plot widget."""
        self.sessionManager = sessionManager
        self.reset()
    
    def reset(self):
        """Resets the plots"""
        self.clear()  # Removes all items from the layout and resets current column and row to 0
        self.plots.clear()  # Remove all the plots from the plot list
    
    def addLap(self, filename: str, sessionNo: int, restartNo: int, lapNo: int):
        """Adds the telemetry from a lap into all of the existing widgets, and tells the widget to include it in all
        future plots, unless it is removed"""
        lapData = self.sessionManager.getLapData(filename, sessionNo, restartNo, lapNo).reset_index()

    def removeLap(self, filename: str, sessionNo: int, restartNo: int, lapNo: int):
        """Removes the telemetry from a lap from all of the existing widgets, and tells the widget not to include it in
        any future widgets until it is added again"""
        ...

    def addNewPlot(self, plotType: _PlotTypes, xAxis: Utility.ForzaSettings.params = None, yAxis: Utility.ForzaSettings.params = None):
        """
        Adds a new type of plot to the widget displaying all the currently selected laps. Plots can be chosen from a
        predefined list defined by the _PlotTypes literal. Some plots require x and y values to be provided, while others do not.
        The following plot types DO NOT require x and y values:
        - delta
        
        Parameters
        ----------
        plotType : The type of plot to add from a predefined list of plot types
        yAxisValues : The field to use for the Y Axis of the new plot
        xAxisValues : The field to use for the X Axis of the new plot
        """

        assert self.sessionManager is not None, "Error: Cannot add a plot before telemetry data has been loaded"

        newPlot = pg.PlotItem()
        
        match plotType:
            case "simple":
                # A simple line graph using the fields specified as the x and y axis
                
                # Check that the telemetry data includes the fields that were asked for
                assert yAxis in self.sessionManager.telemetry.getDataFrame().columns, f"Error: {yAxis} field not found in telemetry data."
                assert xAxis in self.sessionManager.telemetry.getDataFrame().columns, f"Error: {xAxis} field not found in telemetry data."
                
                newPlot.setTitle(f"Simple {yAxis}/{xAxis} Plot")
                newPlot.setLabel(axis="left", text=yAxis.capitalize())
                newPlot.setLabel(axis="bottom", text=xAxis.capitalize())
                newPlot.showGrid(True, True, 0.5)
                
                # Hardcoded for testing - get the details from the first packet in the telemetry data and use that lap only for now
                filename = self.sessionManager.telemetry.getDataFrame()["filename"][0]
                session_no = self.sessionManager.telemetry.getDataFrame()["session_no"][0]
                restart_no = self.sessionManager.telemetry.getDataFrame()["restart_no"][0]
                lap_no = self.sessionManager.telemetry.getDataFrame()["lap_no"][0]
                lapData = self.sessionManager.getLapData(filename, session_no, restart_no, lap_no).reset_index()

                #logging.info("Lap Data for Plot:\n{}".format(lapData))

                xValues = lapData[xAxis]
                yValues = lapData[yAxis]
                newPlot.plot(xValues, yValues)

            case "delta":
                ...
            case _:
                ...
        
        self.plots.append(newPlot)
        self.addItem(newPlot)
    

class TreeDataItem(QStandardItem):
    """A common item class for the tree view"""

    def __init__(self, text):
        super().__init__(text)
        self.setEditable(False)  # Don't want the user to be able to edit cells
        self.setSelectable(False)


class GroupDataItem(TreeDataItem):
    """An item class for all the groups that a lap is under in the tree view"""

    def __init__(self, text):
        super().__init__(text)


class LapDataItem(TreeDataItem):
    """A specific item class for laps in the tree view"""

    # The colour choices available for the lap colour when selected
    ColourChoices = Literal["red", "green", "blue", "cyan", "magenta", "yellow"]

    def __init__(self, text):
        super().__init__(text)
        self.setSelectable(True)
        
    def assignColour(self, colour: ColourChoices):
        """Adds an icon next to the Item with the selected colour"""
        pm = QPixmap(16, 16)
        pm.fill(QColor(colour))
        icon = QIcon(pm)
        self.setIcon(icon)
    
    def removeColour(self):
        """Removes the coloured icon from the Item"""
        self.setIcon(QIcon())


class LapViewerDock(QtWidgets.QDockWidget):

    """A dock widget that displays an overview of the currently loaded laps"""
    def __init__(self, sessionManager: SessionManager, parent=None):
        super().__init__("Lap View", parent=parent)
        self.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)
        self.setStatusTip("Lap Viewer: Choose which laps to view and analyse.")

        self.sessionManager: SessionManager = sessionManager

        # This view displays the data from the heirarchical lap structure model found in the Session Manager
        self.dataView = QtWidgets.QTreeView()

        # Multiple laps can be selected at once
        self.dataView.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.MultiSelection)

        self.dataModel = self.sessionManager.lapDetails
        self.dataView.setModel(self.dataModel)
        self.setWidget(self.dataView)

        self.dataView.clicked.connect(self.doSomething)
        self.dataView.selectionModel().selectionChanged.connect(self.sessionManager.updateLapSelection)

    def doSomething(self, val):
        """Do something"""
        ...
        #print(f"Val: {val}")
        #print(f"Data: {val.data()}")
        #print(f"Row: {val.row()}")
        #print(f"Column: {val.column()}")
    

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

        # Central widget is a Tab Widget, user can select from a number of different ways to view the telemetry data
        centralTabWidget = QtWidgets.QTabWidget()
        self.setCentralWidget(centralTabWidget)

        # A simple table to view the raw telemetry data
        self.table = QtWidgets.QTableView()
        self.table.setModel(self.sessionManager.telemetry)
        centralTabWidget.addTab(self.table, QIcon(str(parentDir / pathlib.Path("assets/icons/table.png"))), "Table")

        # A more involved graph/plot view. Interactive so the user can add or remove different plots, and define what parts of
        # the data they look at
        self.plots = MultiPlotWidget(show=True, title="Telemetry Plotting")
        self.plots.setSessionManager(self.sessionManager)
        centralTabWidget.addTab(self.plots, QIcon(str(parentDir / pathlib.Path("assets/icons/chart.png"))), "Plots")
        
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

        # Add the Dock widgets, eg. graph and data table ---------------------

        self.lapViewerDock = LapViewerDock(self.sessionManager)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.lapViewerDock)

        # Contains actions to open/close the dock widgets
        viewMenu = menu.addMenu("&View")
        #viewMenu.addAction(dataControllerDock.toggleViewAction())
    
    def update(self):
        """Updates the widgets with new information from SessionManager"""
        # Purely used as a convenience for testing right now

        #self.sessionManager.telemetry.to_csv("test.csv")

        #logging.info("Testing Data: {}".format(
        #    self.sessionManager.telemetry.loc[(self.sessionManager.telemetry["lap_no"] == 1) & (self.sessionManager.telemetry["dist_traveled"] < 1950)])
        #    )

        self.plots.addNewPlot("simple", "cur_lap_distance", "speed")
