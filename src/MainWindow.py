from PyQt6 import QtWidgets, QtMultimedia
from PyQt6.QtCore import pyqtSlot, QThread, QObject, pyqtSignal, Qt, QSize, QUrl, QAbstractTableModel, QItemSelection, QModelIndex
from PyQt6.QtGui import QAction, QIcon, QKeySequence, QColor, QStandardItemModel, QStandardItem, QPixmap, QPen
from PyQt6.QtMultimedia import QMediaDevices, QCamera, QMediaCaptureSession, QCameraDevice, QCameraFormat
from PyQt6.QtMultimediaWidgets import QVideoWidget

import pyqtgraph as pg
import numpy as np
import pandas as pd

import distinctipy

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
            if index.column() == 5:
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
    lapSelected = pyqtSignal(str, int, int, int, tuple)  # Emitted when a lap is selected to view, contains filename, session_np, restart_no, lap_no, colour (as an rgb tuple)
    lapDeselected = pyqtSignal(str, int, int, int)  # Emitted when a lap is deselected, contains filename, session_np, restart_no, lap_no

    def __init__(self, trackDetails: pd.DataFrame, parent = None):
        super().__init__(parent)

        self.trackDetails: pd.DataFrame = trackDetails  # A DataFrame containing details about each track in the game (eg. track ordinal, length etc)
        self.telemetry: DataFrameModel = DataFrameModel()  # The data containing all the currently loaded sessions, restarts and laps
        self.numberOfSessions: int = 0  # The number of sessions currently represented by the data
        self.trackOrdinal: int | None = None
        self.initialised: bool = False  # True when the sessions have been loaded
        
        # A DataFrame containing important details about each lap from the telemetry data that the player completed. It contains:
        # Lap number, restart number, session number, filename, lap time, 
        self.lapDetails: LapDetailsModel = LapDetailsModel()
        self.lapColours = {}  # A dictionary of laps : colour. Keys (laps) are tuples containing (filename, session_no, restart_no, lap_no)
    
    def _updateLapDetails(self):
        """Creates a new DataFrame with important details about each completed lap. This filters the data in each session
        to look for completed laps only."""

        lapDistance = self.trackDetails.at[self.trackOrdinal, "length"]
        
        def filterFunc(x, lapDistance):
            """Takes a group of packets from a single lap, and returns True if that lap is valid. A lap is valid if the
            last packet's distance was close to the length of the track lap."""
            tolerance = 5  # metres either side of the lap distance is considered a valid lap
            distanceManaged = x["cur_lap_distance"].iat[-1]
            return True if distanceManaged > lapDistance - tolerance and distanceManaged < lapDistance + tolerance else False

        # Filter the packets from incomplete laps out of the data
        filteredLaps = self.telemetry.getDataFrame().groupby(["filename", "session_no", "restart_no", "lap_no"]).filter(lambda x : filterFunc(x, lapDistance))
        
        groupedLaps = filteredLaps.groupby(["filename", "session_no", "restart_no", "lap_no"])[["car_ordinal", "cur_lap_time"]].last()

        # Expand the series into a dataframe with the multi index turning into columns
        lapDetails = groupedLaps.rename(columns={'cur_lap_time': 'lap_time'})
        
        self.lapDetails.updateData(lapDetails.reset_index().rename(columns={'cur_lap_time': 'lap_time'}))

    def updateLapSelection(self, selected: QItemSelection, deselected: QItemSelection):
        """Update the current lap selection"""
        #logging.info(f"Selected: {[s.data() for s in selected.indexes()]}")
        #logging.info(f"Selected: {[f"{s.row()}, {s.column()}" for s in selected.indexes()]}")

        #logging.info(f"Deselected: {[s.data() for s in deselected.indexes()]}")
        #logging.info(f"Deselected: {[f"{s.row()}, {s.column()}" for s in deselected.indexes()]}")

        if len(selected.indexes()) > 0:
            selectedLap = (selected.indexes()[0].data(), int(selected.indexes()[1].data()), int(selected.indexes()[2].data()), int(selected.indexes()[3].data()))
            existingColours = list(self.lapColours.values())
            existingColours.append((0.0,0.0,0.0))
            # Attempt to get a new colour that is as visually distinct from the other lap's colours as possible
            newColour = distinctipy.get_colors(1, existingColours)[0]  # A (r, g, b) 
            self.lapColours[selectedLap] = newColour
            convertedColour = tuple([int(x * 255) for x in newColour])
            self.lapSelected.emit(*selectedLap, convertedColour)
        
        if len(deselected.indexes()) > 0:
            deselectedLap = (deselected.indexes()[0].data(), int(deselected.indexes()[1].data()), int(deselected.indexes()[2].data()), int(deselected.indexes()[3].data()))
            self.lapDeselected.emit(*deselectedLap)
            self.lapColours.pop(deselectedLap)

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
            tempSessionData.reset_index(drop=True, inplace=True)
            self.telemetry.updateData(tempSessionData)
            self.trackOrdinal = tempTrackOrdinal
            self.numberOfSessions = tempNumberOfSessions

            # Update the lap structure model using the new telelemtry DataFrame
            #self._updateLapStructureModel()

            # Update the lap details DataFrame
            self._updateLapDetails()

            self.initialised = True
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


class AddPlotDialog(QtWidgets.QDialog):
    """A custom dialog allowing different types of plots to be added to the plots widget"""


    class SimplePlotForm(QtWidgets.QFrame):
        """A form to add a simple plot"""

        def __init__(self, parent = None):
            super().__init__(parent)
            layout = QtWidgets.QFormLayout()
            self.setLayout(layout)

            xAxisCombo = QtWidgets.QComboBox()
            xAxisCombo.addItems(Utility.ForzaSettings.plotAxisTypes)
            xAxisCombo.currentTextChanged.connect(self.setXAxis)
            layout.addRow("X Axis", xAxisCombo)

            yAxisCombo = QtWidgets.QComboBox()
            yAxisCombo.addItems(Utility.ForzaSettings.plotAxisTypes)
            yAxisCombo.currentTextChanged.connect(self.setYAxis)
            layout.addRow("Y Axis", yAxisCombo)

            self.xAxis = xAxisCombo.currentText()
            self.yAxis = yAxisCombo.currentText()
        
        def setXAxis(self, value):
            self.xAxis = value
        
        def setYAxis(self, value):
            self.yAxis = value


    class DeltaPlotForm(QtWidgets.QFrame):
        """A form to add a delta plot"""

        def __init__(self, parent = None):
            super().__init__(parent)
            layout = QtWidgets.QFormLayout()
            self.setLayout(layout)

            xAxisCombo = QtWidgets.QComboBox()
            xAxisCombo.addItems(Utility.ForzaSettings.plotAxisTypes)
            xAxisCombo.currentTextChanged.connect(self.setXAxis)
            layout.addRow("X Axis", xAxisCombo)

            yAxisCombo = QtWidgets.QComboBox()
            yAxisCombo.addItems(Utility.ForzaSettings.plotAxisTypes)
            yAxisCombo.currentTextChanged.connect(self.setYAxis)
            layout.addRow("Y Axis", yAxisCombo)

            self.xAxis = xAxisCombo.currentText()
            self.yAxis = yAxisCombo.currentText()
        
        def setXAxis(self, value):
            self.xAxis = value
        
        def setYAxis(self, value):
            self.yAxis = value


    def __init__(self, parent = None):
        super().__init__(parent)
        self.setWindowTitle("Add New Plot")
        
        self.buttonBox = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        self.buttonBox.accepted.connect(self.accept)
        self.buttonBox.rejected.connect(self.reject)

        self.tabs = QtWidgets.QTabWidget()
        
        simpleForm = AddPlotDialog.SimplePlotForm()
        self.tabs.addTab(simpleForm, "Simple Plot")

        deltaForm = AddPlotDialog.DeltaPlotForm()
        self.tabs.addTab(deltaForm, "Delta Plot")

        layout = QtWidgets.QVBoxLayout()
        layout.addWidget(self.tabs)
        layout.addWidget(self.buttonBox)
        self.setLayout(layout)


class LapPlotItem(pg.PlotItem):

    wantToClose = pyqtSignal(int)  # Emitted when the close action has been triggered

    def __init__(self, id:int, parent=None, name=None, labels=None, title=None, viewBox=None, axisItems=None, enableMenu=True, **kargs):
        super().__init__(parent, name, labels, title, viewBox, axisItems, enableMenu, **kargs)

        # A dictionary of tuples : PlotDataItem. Each key is a unique tuple and identifies the PlotDataItem value (or line) that
        # it is associated with
        self.lineDict = {}
        self.id = id  # The id of the plot, passed to the close signal
        vb = self.getViewBox()
        closeAction = vb.menu.addAction("Close")
        closeAction.triggered.connect(self.closing)
        
    def closing(self):
        logging.info("Closing...")
        self.wantToClose.emit(self.id)

    @abstractmethod
    def addLap(self, lap, lapData: pd.DataFrame, colour):
        """Adds a new lap to the plot. Implement this method for each new plot type to customise its behaviour"""
    
    @abstractmethod
    def removeLap(self, lap):
        """Removes a lap from the plot given a tuple identifying it. Implement this method for each new plot type to customise its behaviour"""


class SimpleLapPlot(LapPlotItem):
    """Displays a simple x/y line graph of 2 chosen fields from a lap"""

    def __init__(self, id:int, xAxis:str, yAxis:str, parent=None, name=None, labels=None, title=None, viewBox=None, axisItems=None, enableMenu=True, **kargs):
        super().__init__(id, parent, name, labels, title, viewBox, axisItems, enableMenu, **kargs)
        self.xAxis: str = xAxis
        self.yAxis: str = yAxis

        self.setLabel(axis="left", text=yAxis.replace("_", " ").title())
        self.setLabel(axis="bottom", text=xAxis.replace("_", " ").title())
        self.setTitle(f"Simple {yAxis}/{xAxis} Plot")
        self.showGrid(True, True, 0.5)

    def addLap(self, lap, lapData: pd.DataFrame, colour):
            """
            Adds a new lap to the plot as a new line. Raises an exception if the lap has already been added.
            
            Parameters
            ----------
            lap : A tuple containing the filename, session_no, restart_no and lap_no of the lap being added
            lapData : A pandas DataFrame containing all the telemetry from the lap
            colour : A (r, g, b) tuple representing a colour in rgb form
            """

            # If the lap has already been added to the plot, raise error
            assert lap not in self.lineDict.keys(), f"Cannot add lap {lap} to the plot: Already added."

            # Check if the fields exist in the lapData
            assert self.xAxis in lapData.columns, "Couldn't add lap f{lap}, f{self.xAxis} Not found in data."
            assert self.yAxis in lapData.columns, "Couldn't add lap f{lap}, f{self.yAxis} Not found in data."

            xValues = lapData[self.xAxis]
            yValues = lapData[self.yAxis]

            pen = pg.mkPen(color=colour)
            name = f"{lap[0]}-{lap[1]}-{lap[2]}-{lap[3]}"
            line = self.plot(xValues, yValues, pen=pen, name=name)
            self.lineDict[lap] = line

    def removeLap(self, lap):
        """Removes a lap from the plot given a tuple identifying it. Raises an exception if the lap is not in the plot"""

        assert lap in self.lineDict.keys(), "Cannot remove lap {lap} from the plot: doesn't exist."

        line = self.lineDict.pop(lap)
        self.removeItem(line)


class DeltaPlot(LapPlotItem): 
    """Displays a delta plot for a chosen field for all selected laps. """

    def __init__(self, id:int, xAxis:str, yAxis:str, parent=None, name=None, labels=None, title=None, viewBox=None, axisItems=None, enableMenu=True, **kargs):
        super().__init__(id, parent, name, labels, title, viewBox, axisItems, enableMenu, **kargs)
        self.xAxis: str = xAxis
        self.yAxis: str = yAxis

        self.setLabel(axis="left", text=yAxis.replace("_", " ").title())
        self.setLabel(axis="bottom", text=xAxis.replace("_", " ").title())
        self.setTitle(f"Delta plot for {yAxis}")
        self.showGrid(True, True, 0.5)

        # The baseline lap values to compare all the other laps to
        self.baselineSeries: pd.Series | None = None  # The series to use as the baseline
        self.baseLap: tuple | None = None  # The lap that is used as the baseline, as a tuple

    def addLap(self, lap, lapData: pd.DataFrame, colour):
            """
            Adds a new lap to the plot as a new line. Raises an exception if the lap has already been added.
            
            Parameters
            ----------
            lap : A tuple containing the filename, session_no, restart_no and lap_no of the lap being added
            lapData : A pandas DataFrame containing all the telemetry from the lap
            colour : A (r, g, b) tuple representing a colour in rgb form
            """
            
            # Don't bother adding if it's the base lap
            if lap == self.baseLap:
                return
            
            # If the lap has already been added to the plot, raise error
            assert lap not in self.lineDict.keys(), f"Cannot add lap {lap} to the plot: Already added."

            # Check if the fields exist in the lapData
            assert self.xAxis in lapData.columns, "Couldn't add lap f{lap}, f{self.xAxis} Not found in data."
            assert self.yAxis in lapData.columns, "Couldn't add lap f{lap}, f{self.yAxis} Not found in data."

            xValues = lapData[self.xAxis]
            yValues = lapData[self.yAxis]

            # Create a new series of Y Values indexed by the X Values to use for comparison to other laps
            newLapSeries = pd.Series(yValues.values, index=xValues.copy())

            # If the lap is the first lap to be added, make it the base line.
            # This base line can never be changed once it is added to the plot.
            if self.baselineSeries is None:
                self.baselineSeries = newLapSeries.copy()
                self.baseLap = lap
            
            # Get the difference between the new lap, and the baseline
            dNew, dBase = self.baselineSeries.align(newLapSeries)
            difference = dBase.interpolate() - dNew.interpolate()
            #difference.interpolate(inplace=True)

            # Create the plot data item using the index as the x axis, and values as the y axis
            pen = pg.mkPen(color=colour)
            name = f"{lap[0]}-{lap[1]}-{lap[2]}-{lap[3]}"
            line = self.plot(difference.index.to_list(), difference.values, pen=pen, name=name)
            self.lineDict[lap] = line

    def removeLap(self, lap):
        """Removes a lap from the plot given a tuple identifying it. Raises an exception if the lap is not in the plot"""

        # Don't remove if it's the base lap
        if lap == self.baseLap:
            return

        assert lap in self.lineDict.keys(), "Cannot remove lap {lap} from the plot: doesn't exist."

        line = self.lineDict.pop(lap)
        self.removeItem(line)


class MultiPlotWidget(pg.GraphicsLayoutWidget):
    """Displays multiple plots generated from the session data."""

    _PlotTypes = Literal["delta", "simple"]

    def __init__(self, sessionManager: SessionManager, parent=None, show=False, size=None, title=None, **kargs):
        super().__init__(parent, show, size, title, **kargs)

        self.nextid = 0  # The next ID to give to a plot
        self.sessionManager: SessionManager = sessionManager
        self.plots = {}  # A dict of id (int) : plot (plotitem)
        self.laps = {}  # A dictionary of displayed laps : data, organised into tuples of (filename, sessionNo, restartNo, lapNo) : DataFrame
        self.lapColours = {}  # A dictionary of displayed laps : colour

        self.sessionManager.lapSelected.connect(self.addLap)
        self.sessionManager.lapDeselected.connect(self.removeLap)
    
    def reset(self):
        """Resets the plots"""
        self.clear()  # Removes all items from the layout and resets current column and row to 0
        self.plots.clear()  # Remove all the plots from the plot list
    
    def addLap(self, filename: str, sessionNo: int, restartNo: int, lapNo: int, lapColour: tuple):
        """Adds the telemetry from a lap into all of the existing widgets, and tells the widget to include it in all
        future plots, unless it is removed"""
        lapData = self.sessionManager.getLapData(filename, sessionNo, restartNo, lapNo).reset_index()
        lapTuple = (filename, sessionNo, restartNo, lapNo)
        for plot in self.plots.values():
            plot.addLap(lapTuple, lapData, lapColour)
        self.laps[lapTuple] = lapData
        self.lapColours[lapTuple] = lapColour

    def removeLap(self, filename: str, sessionNo: int, restartNo: int, lapNo: int):
        """Removes the telemetry from a lap from all of the existing widgets, and tells the widget not to include it in
        any future widgets until it is added again"""
        lap = (filename, sessionNo, restartNo, lapNo)
        for plot in self.plots.values():
            plot.removeLap(lap)
        self.laps.pop(lap)
        self.lapColours.pop(lap)

    def removePlot(self, plotId: int):
        """Closes and removes a plot from the layout when given its ID"""
        plot = self.plots.pop(plotId)
        self.removeItem(plot)

    def addNewPlot(self, plotType: _PlotTypes, xAxis: str = None, yAxis: str = None):
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

        newPlot: LapPlotItem | None = None
        plotId = self.nextid
        self.nextid += 1
        
        match plotType:
            case "simple":  # A simple line graph using the fields specified as the x and y axis
                # Check that the telemetry data includes the fields that were asked for
                assert yAxis in self.sessionManager.telemetry.getDataFrame().columns, f"Error: {yAxis} field not found in telemetry data."
                assert xAxis in self.sessionManager.telemetry.getDataFrame().columns, f"Error: {xAxis} field not found in telemetry data."

                newPlot = SimpleLapPlot(plotId, xAxis, yAxis)
                newPlot.addLegend()

            case "delta":
                # Check that the telemetry data includes the fields that were asked for
                assert yAxis in self.sessionManager.telemetry.getDataFrame().columns, f"Error: {yAxis} field not found in telemetry data."
                assert xAxis in self.sessionManager.telemetry.getDataFrame().columns, f"Error: {xAxis} field not found in telemetry data."

                newPlot = DeltaPlot(plotId, xAxis, yAxis)
                newPlot.addLegend()
                
            case _:
                ...
        
        # Add all selected laps to the plot
        for lap, data in self.laps.items():
            colour = self.lapColours[lap]
            newPlot.addLap(lap, data, colour)
        
        # Connect the close action
        newPlot.wantToClose.connect(self.removePlot)

        self.plots[plotId] = newPlot
        self.addItem(newPlot)
    
    def addNewPlotAction(self):
        logging.info("Add new plot action")
        if not self.sessionManager.initialised:
            QtWidgets.QMessageBox.information(self, "No Session Loaded", "Cannot add new plot: Session has not been loaded")
            return
        dlg = AddPlotDialog()
        if dlg.exec():
            match dlg.tabs.currentIndex():
                case 0:  # Simple Plot
                    self.addNewPlot("simple", dlg.tabs.currentWidget().xAxis, dlg.tabs.currentWidget().yAxis)

                case 1:  # Delta
                    self.addNewPlot("delta", dlg.tabs.currentWidget().xAxis, dlg.tabs.currentWidget().yAxis)

                case _:
                    ...

        else:
            logging.info("Didn't add plot")
        

class LapViewerDock(QtWidgets.QDockWidget):

    """A dock widget that displays an overview of the currently loaded laps"""
    def __init__(self, sessionManager: SessionManager, parent=None):
        super().__init__("Lap View", parent=parent)
        #self.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)
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
        self.plots = MultiPlotWidget(self.sessionManager, show=True, title="Telemetry Plotting")
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

        addNewPlotAction = QAction(QIcon(str(parentDir / pathlib.Path("assets/icons/chart.png"))), "Add New Plot", self)
        addNewPlotAction.setStatusTip("Add New Plot: Creates and adds a new plot to the plot window.")
        addNewPlotAction.triggered.connect(self.plots.addNewPlotAction)
        toolbar.addAction(addNewPlotAction)

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

        #self.plots.addNewPlot("simple", "cur_lap_distance", "speed")
        #self.plots.addNewPlot("simple", "cur_lap_distance", "accel")
        #self.plots.addNewPlot("simple", "cur_lap_distance", "brake")
