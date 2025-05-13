from PyQt6 import QtWidgets, QtMultimedia
from PyQt6.QtCore import pyqtSlot, QThread, QObject, pyqtSignal, Qt, QSize, QUrl
from PyQt6.QtGui import QAction, QIcon, QKeySequence
from PyQt6.QtMultimedia import QMediaDevices, QCamera, QMediaCaptureSession, QCameraDevice, QCameraFormat
from PyQt6.QtMultimediaWidgets import QVideoWidget

import pyqtgraph as pg
import numpy as np

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


class Session(QObject):
    """
    Stores the telemetry data and associated calculated data for a currently opened session. A session
    represents a single unit of time's worth of continuously logged packets saved as a csv file.
    """

    def __init__(self, data: np.ndarray, filePath = pathlib.Path, parent = None):
        super().__init__(parent = parent)

        # Contains all the Lap objects generated from the telemetry, in order from lap 0 to lap n or lap n - 1.
        # Lap n may or may not be included, depending on if the user finishes it (like in a lapped race) or if
        # they quit in the middle (like in free practice).

        # A structured numpy array containing data about the laps in a session
        self.laps: np.ndarray = None
        self.lapViews = dict()  # A dictionary of laps to numpy views of the data included in the lap, from the Session data
        
        self.trackID: int = None  # The forza-assigned ID of the track
        self.lapDistance: int = None  # The distance over a single lap, calculated at the end of the first lap

        self.data: np.ndarray = None  # The entire data of a session
        self.filePath = filePath  # A pathlib.Path object
        self.name = str(filePath.stem)  # Name of the session, equal to the name of the file

        # Sort the data and initialise the Session object
        Session._sortData(data)
        self.initialise(data)

        logging.info("Created Session {}".format(self.name))
        logging.info("Fastest Lap: {}".format(self.getFastestLap()))
    
    def addLap(self, data: np.ndarray):
        """Adds a single new lap onto the session, given all the packets collected during that lap. If there are multiple laps
        included in data, it will throw an exception. If the lap isn't complete (ie. the dist_traveled is too short, meaning
        the player didn't finish the lap) the lap will not be included but will still be used to improve the previous lap. A *copy* of
        data will be used for the Session."""

        data = data.copy()

        # Get the lap number (If there are many laps, throw exception)
        uniqueLaps, uniqueIndexes = np.unique(data["lap_no"], return_index=True)
        if len(uniqueLaps) > 1:
            raise ValueError("Multiple laps were included in the data array - Couldn't add a lap", uniqueLaps.tolist())
        currentLapNumber = int(uniqueLaps[0])

        # Get info from the last packet
        lastLapTime, curLapTime, distTraveled = data[-1]["last_lap_time"], data[-1]["cur_lap_time"], data[-1]["dist_traveled"]

        # Get info from the first packet
        lapBegin, trackID = data[0]["cur_race_time"], data[0]["track_ordinal"]

        # Create the lap
        currentLapData = np.array([(currentLapNumber, curLapTime, lapBegin, distTraveled)], dtype=np.dtype([("lap_no", "i8"), ("lap_time", "f8"), ("lap_begin_time", "f8"), ("dist_traveled", "f8")]))

        # If this is the first lap (if the laps array is empty), use all the data as it is (no previous lap to edit)
        # and set the track info
        if self.laps is None:
            self.lapDistance = float(distTraveled)
            self.trackID = int(trackID)
            self.laps = currentLapData
        else:
            # Get the previous lap and update the lap time with a more accurate figure from this lap
            prevLap = self.laps[-1]
            prevLap["lap_time"] = lastLapTime

            # Decide if this lap is a complete lap or not
            currentLapDistance = distTraveled - prevLap["dist_traveled"]
            tolerance = 3  # lap should be within the the first lap's distance +/- tolerance in metres
            if currentLapDistance > self.lapDistance - tolerance and currentLapDistance < self.lapDistance + tolerance:
                self.laps = np.hstack((self.laps, currentLapData))
        
        # Update the Session's data array with the new lap's data
        if self.data is not None:
            self.data = np.hstack((self.data, data))
        else:
            self.data = data
        
        # Update the Session's individual lap views after the Session's data array
        self.updateLapViews()
        
    def updateLapViews(self):
        """Updates the lapViews dictionary when the Session is updated with a new lap"""
        
        # Separate the Session data into views for each lap
        self.lapViews.clear()
        uniqueLaps, lapStartIndex = np.unique(self.data["lap_no"], return_index=True)
        lapStartIndex = np.append(lapStartIndex, [len(self.data)])

        # Create a new view by slicing the data array
        for i in range(0, len(uniqueLaps)):
            self.lapViews[uniqueLaps[i]] = self.data[lapStartIndex[i]:lapStartIndex[i+1]]
    
    def initialise(self, data: np.ndarray):
        """Initialises the Session object from pre-recorded telemetry data"""

        # Separate the data into views for each unique lap
        lapViews = OrderedDict()
        uniqueLaps, lapStartIndex = np.unique(data["lap_no"], return_index=True)
        lapStartIndex = np.append(lapStartIndex, [len(data)])

        for i in range(0, len(uniqueLaps)):
            lapViews[uniqueLaps[i]] = data[lapStartIndex[i]:lapStartIndex[i+1]]
        
        # For each lap view, create a lap entry and add it to the session object
        for view in lapViews.values():
            self.addLap(view)
        
    def _sortData(data: np.ndarray):
        """Sorts the packet numpy array based on the timestamp_ms field. This is Forza's internal timestamp and will ensure rows will be sorted
        by the order they were produced, instead of the order they were received as UDP may be unreliable. This is an unsigned 32 bit int, so
        can overflow after about 50 days. This will detect an overflow and re-sort the fields to restore true chronological order."""

        threshold = 3600000  # If the gap between adjacent timestamps is larger than this, an overflow has occurred (3600000ms is about an hour)
        #threshold = 1000  # For testing
        data.sort(order="timestamp_ms", kind="stable")  # First in-place sort to remove UDP unreliability

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
                data["timestamp_ms"][i] += maxTimestamp + 1
            
            # Re-sort the array in-place to put those rows after the overflow at the back again
            data.sort(order="timestamp_ms", kind="stable")

    def getFastestLap(self):
        """Returns the fastest lap time of the session as a numpy float"""
        fastestLap = self.laps["lap_time"].min()
        return fastestLap


class SessionManager(QObject):
    """Stores and manages all the currently opened Sessions"""

    # Emitted when a single new session is loaded in through the Session dialog box. If multiple sessions are loaded
    # at once, a signal will be emitted for each one.
    sessionLoaded = pyqtSignal(Session)

    # Emitted when the user focuses or unfocuses a lap, and contains the focusedLaps dictionary
    focusedLapsChanged = pyqtSignal(dict)

    # Emitted when a new set of sessions are loaded. Only emitted once per session reset
    sessionReset = pyqtSignal()

    def __init__(self, parent = None):
        super().__init__(parent)
        self.sessions = dict()  # session name (str) : Session object

        # All the focused laps in a session. Keys are the session name, and the value is a set of all the laps
        # in focus in that session.
        self.focusedLaps = dict()
    
    def lapFocusToggle(self, sessionName: str, lapNumber: int, checkState: Qt.CheckState):
        """Toggles which laps are in focus and displayed in the graphs and video widgets"""
        if checkState == Qt.CheckState.Checked:
            self.focusedLaps[sessionName].add(lapNumber)
        else:
            self.focusedLaps[sessionName].discard(lapNumber)
        self.focusedLapsChanged.emit(self.focusedLaps)            

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

            tempSessionsDict = dict()
            loadedTrackID = None

            for filePath in filePathList:
                data = np.genfromtxt(filePath, delimiter=",", names=True)  # Numpy loads the csv file into a numpy array
                newSession = Session(data, pathlib.Path(filePath).resolve(), self)
                fileName = pathlib.Path(filePath).resolve().stem
                if loadedTrackID is None:
                    loadedTrackID = newSession.trackID
                else:
                    if loadedTrackID != newSession.trackID:
                        failAlert = QtWidgets.QMessageBox(text="Error: Cannot open data from different tracks. Make sure all telemetry files are from the same track and try again.", parent=self.parent())
                        failAlert.setWindowTitle("Session Loading Error")
                        failAlert.exec()
                        return  # Fail if user is loading data from different tracks

                # Make sure all new sessions are loaded successfully before replacing all currently opened sessions
                tempSessionsDict[fileName] = newSession

            self.sessions = tempSessionsDict  # Replace the currently loaded sessions with new ones
            self.focusedLaps.clear()  # Clear the currently focused laps
            self.sessionReset.emit()
            for name, s in self.sessions.items():
                self.sessionLoaded.emit(s)
                self.focusedLaps[name] = set()


class MultiPlotWidget(QtWidgets.QWidget):
    """Displays multiple telemetry plots generated from the session telemetry data. Each plot will correspond with either
    a parameter from the Forza data packet, or a calculated parameter such as delta. The parameter will be the Y axis, and
    the X axis will always be lap distance. A 'player head' will be visible in each plot, indicating which part of the plot
    the lap video viewer is looking at."""


    class PlotController(QtWidgets.QWidget):
        """A widget to control which plots are displayed"""


        class PlotCheckBox(QtWidgets.QCheckBox):
            
            # Emitted when the checkbox is clicked. Contains the plot type as a str, and the checkstate
            toggleFocus = pyqtSignal(str, Qt.CheckState)

            def __init__(self, plotType: str, parent = None):
                super().__init__(parent = parent)
                self.plotType = plotType
                self.stateChanged.connect(self._emitToggleSignal)
            
            def _emitToggleSignal(self):
                self.toggleFocus.emit(self.plotType, self.checkState())


        # Emitted when the user select a plot to add or remove
        togglePlot = pyqtSignal(str, Qt.CheckState)

        def __init__(self, customPlotList, parent = None):
            super().__init__(parent)
            self.displayedPlots = OrderedDict()  # Just using the keys to act like an ordered set. Each key is a plot type
            self.l = QtWidgets.QFormLayout()
            self.setLayout(self.l)

            # All the possible plot types formed of custom calculated plots, and the Forza parameters
            self.plotTypes = customPlotList
            self.plotTypes += Utility.ForzaSettings.params

            # Dictionary of all plot types to checkboxes so user can choose to add plots
            self.plotDict = dict()
            for plotType in self.plotTypes:
                checkBox = MultiPlotWidget.PlotController.PlotCheckBox(plotType=plotType)
                checkBox.toggleFocus.connect(self.togglePlot)
                self.plotDict[plotType] = checkBox

            # Add a checkbox for each plot type
            for plotType, checkBox in self.plotDict.items():
                self.l.addRow(plotType, checkBox)
        
        def reset(self):
            """Resets all the checkboxes and clears dictionaries"""
            self.displayedPlots.clear()
            for checkBox in self.plotDict.values():
                checkBox.setCheckState(Qt.CheckState.Unchecked)
    

    class PlotDisplay(QtWidgets.QWidget):
        """A widget that displays all the plots"""

        def __init__(self, parent = None):
            super().__init__(parent)

            self.lt = QtWidgets.QVBoxLayout()
            self.setLayout(self.lt)


    class MultiLapPlot(pg.PlotWidget):
        
        def __init__(self, title: str, parent=None, background='default', plotItem=None, **kargs):
            super().__init__(parent, background, plotItem, title=title, **kargs)
            self.lines = dict()  # (sessionName, Lap Number) : Line
        
        def addLap(self, sessionName: str, lapNumber: int, xValues: np.ndarray, yValues: np.ndarray, hue: int = 0):
            """Adds a new line onto the plot displaying values from a single lap"""

            if self.lines.get((sessionName, lapNumber)) == None:  # Don't add if it's already in the plot
                pen = pg.mkPen(color=(hue, 255, 255))
                line = self.plot(xValues, yValues, pen=pen)
                self.lines[(sessionName, lapNumber)] = line

        def removeLap(self, sessionName: str, lapNumber: int):
            """Removes a single line from the plot associated with a single lap"""

            line = self.lines.get((sessionName, lapNumber))
            if line is not None:
                self.removeItem(line)
                self.lines.pop((sessionName, lapNumber))


    def __init__(self, sessionManager: SessionManager):
        super().__init__()

        self.customPlotTypes = ["delta"]
        self.currentPlots = OrderedDict()  # Dictionary of plotType: str to PlotWidget objects

        self.controller = MultiPlotWidget.PlotController(self.customPlotTypes)
        self.controller.togglePlot.connect(self.togglePlot)
        self.plotDisplay = MultiPlotWidget.PlotDisplay()
        self.sessionManager = sessionManager  # So the plots can access the session data

        controllerScrollArea = QtWidgets.QScrollArea()
        controllerScrollArea.setWidget(self.controller)
        controllerScrollArea.setWidgetResizable(True)
        controllerScrollArea.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)

        plotScrollArea = QtWidgets.QScrollArea()
        plotScrollArea.setWidget(self.plotDisplay)
        plotScrollArea.setWidgetResizable(True)
        plotScrollArea.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)

        layout = QtWidgets.QHBoxLayout()
        layout.addWidget(controllerScrollArea, 0)
        layout.addWidget(plotScrollArea, 1)
        self.setLayout(layout)
    
    def togglePlot(self, plotType: str, checkState: Qt.CheckState):
        """Displays or removes a plot from the display widget. All currently focused laps will appear as lines in the plot."""
        
        if checkState == Qt.CheckState.Checked:
            # Add the plot to the end of the display widget and the dictionary
            plot = MultiPlotWidget.MultiLapPlot(plotType)

            # Add all the currently focused laps to the plot
            for sessionName, focusedLaps in self.sessionManager.focusedLaps.items():
                for lapNumber in focusedLaps:
                    xValues, yValues = self._getLapData(plotType, sessionName, lapNumber)
                    plot.addLap(sessionName, lapNumber, xValues, yValues)
                
            plot.setMinimumHeight(300)
            self.currentPlots[plotType] = plot
            self.plotDisplay.layout().addWidget(plot)
        else:
            # Remove the plot
            plot = self.currentPlots.get(plotType)
            if plot is not None:
                # Remove the widget from the layout, and delete it from the plot dictionary
                self.plotDisplay.layout().removeWidget(plot)
                self.currentPlots.pop(plotType)  
    
    def toggleLap(self, sessionName: str, lapNumber: int, checkState: Qt.CheckState):
        """Adds or removes a lap from all the plots"""
        
        if checkState == Qt.CheckState.Checked:
            # Add the lap to all the currently displayed plots
            for plotType, plotWidget in self.currentPlots.items():
                xValues, yValues = self._getLapData(plotType, sessionName, lapNumber)
                plotWidget.addLap(sessionName, lapNumber, xValues, yValues)
        else:
            # Remove the lap from all the currently displayed plots
            for plot in self.currentPlots.values():
                plot.removeLap(sessionName, lapNumber)
    
    def _getLapData(self, plotType: str, sessionName: str, lapNumber: int):
        """Returns a tuple of the x and y values as numpy arrays of the lap data for a specified plot type. Eg. if the plot type was 'speed',
        this will return (x-values, y-values) where x-values is a numpy array containing the distance traveled that lap, and y-values is
        a numpy array containing the speed values."""

        # Get the lap data from the correct session
        lapView = self.sessionManager.sessions[sessionName].lapViews[lapNumber]
        distanceCopy = lapView["dist_traveled"].copy()  # Copied so the values can be normalise without affecting the original data
        yValues = None

        # Normalise the dist_traveled for each entry so that it always starts at 0, allowing the
        # laps to sit on top of each other in the plot
        if lapNumber > 0:  # Lap 0 does not need to be normalised
            startDistance = distanceCopy[0]
            for i in range(0, len(distanceCopy)):
                distanceCopy[i] -= startDistance
        
        if plotType in Utility.ForzaSettings.params:
            yValues = lapView[plotType]
        else:
            # Fill yValues with the calculate values
            yValues = []
        
        return (distanceCopy, yValues)
    
    def reset(self):
        """Resets the plot widget - removes any displayed plots and unchecks the plot controller checkboxes"""
        
        # Remove the plot widgets from the layout and from the dictionary
        for plot in self.currentPlots.values():
            self.plotDisplay.layout().removeWidget(plot)
        self.currentPlots.clear()

        # Reset the checkboxes
        self.controller.reset()


    def _update(self, data: np.ndarray):
        """Creates new plots from the new session telemetry data, but doesn't display them right away"""
        self.data = data

        self.addNewPlot("time", "cur_lap_time")
        self.addNewPlot("dist_traveled", "speed")
        self.addNewPlot("dist_traveled", "steer")

        t = self.plots["cur_lap_time"].getPlotItem().getViewBox()
        u = self.plots["speed"].getPlotItem().getViewBox()
        v = self.plots["steer"].getPlotItem().getViewBox()

        v.setXLink(t)

    def _addNewPlot(self, x: str, y: str):
        """
        Adds a new plot to the layout
        
        Parameters
        ----------
        x : The parameter to assign to the x axis, eg. dist_traveled
        y : The parameter to assign to the y axis, eg. speed
        """
        
        xAxis = self.data[x]
        newPlot = pg.plot(title = y)
        newPlot.setMinimumHeight(300)
        logging.info("Min size hint of newPlot: {} by {}".format(newPlot.minimumSize().width(), newPlot.minimumSize().height()))
        yAxis = self.data[y]
        newPlot.plot(xAxis, yAxis)

        vLine = pg.InfiniteLine(angle=90, movable=False)  # Player head
        newPlot.addItem(vLine, ignoreBounds = True)

        self.plots[y] = newPlot
        self.lt.addWidget(newPlot)


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
            lapCheckBox.toggleLapFocus.connect(self.lapToggled)
            self.layout().addWidget(lapCheckBox, self._row, 1)
            self.layout().addWidget(QtWidgets.QLabel(text=Utility.formatLapTime(lap["lap_time"]), parent=self), self._row, 2)
            self._row += 1
        

    def __init__(self, parent = None):
        super().__init__(parent)
        self.openSessions = dict()  # Session Name (str) : Session tab (SessionViewerWidget)
        layout = QtWidgets.QVBoxLayout()
        self.setLayout(layout)
    
    def addSession(self, session: Session):
        """Adds a new session to the overview"""
        tab = SessionOverviewWidget.SessionViewerWidget(session)
        tab.toggleLapFocus.connect(self.toggleLapFocus)
        self.openSessions[session.name] = tab
        self.layout().addWidget(tab)
    
    def reset(self):
        """Resets the widget and clears the displayed sessions"""
        for sessionTab in self.openSessions.values():
            self.layout().removeWidget(sessionTab)
        self.openSessions.clear()


class LapViewer(QtWidgets.QWidget):
    """Displays a single video widget to the user starting at a specified point in the video, eg. the start of a lap."""

    class State(Enum):
        UNINITIALISED = 0
        INITIALISED = 1

    def __init__(self, source: str = None, position: int = 0):
        super().__init__()
        self.mediaPlayer = QtMultimedia.QMediaPlayer()
        self.videoWidget = QVideoWidget()
        self.mediaPlayer.setVideoOutput(self.videoWidget)
        self.state = self.State.UNINITIALISED

        # The position the playback should start at, given as milliseconds since the beginning of the video.
        # Eg. if position = 0, the playback starts from the very beginning of the video.
        self.startingPosition = position

        self.mediaPlayer.setSource(QUrl(source))

        # Set the position only when the video has buffered, otherwise it won't set position
        self.mediaPlayer.mediaStatusChanged.connect(self._positionSettable)
        
        lt = QtWidgets.QVBoxLayout()
        self.setLayout(lt)
        lt.addWidget(self.videoWidget)
    
    def _positionSettable(self, mediaStatus: QtMultimedia.QMediaPlayer.MediaStatus):
        """Sets the position when the player has buffered media"""
        if mediaStatus is QtMultimedia.QMediaPlayer.MediaStatus.BufferedMedia and self.state is self.State.UNINITIALISED:
            self.mediaPlayer.setPosition(self.startingPosition)
            self.state = self.State.INITIALISED
            logging.info("Set position to {}".format(self.mediaPlayer.position()))
    
    def stop(self):
        """Stops the playback and resets the position to the given starting position"""
        self.mediaPlayer.stop()
        self.mediaPlayer.setPosition(self.startingPosition)
    
    def pause(self):
        """Pauses the playback at the current position"""

        # If the video has reached the end, pausing it will reset to the beginning. This disables that, so the user can
        # watch the rest of the other laps uninterrupted.
        if self.mediaPlayer.mediaStatus() is not QtMultimedia.QMediaPlayer.MediaStatus.EndOfMedia:
            self.mediaPlayer.pause()
    
    def play(self):
        """Starts playing the video at its current position"""

        if self.mediaPlayer.mediaStatus() is not QtMultimedia.QMediaPlayer.MediaStatus.EndOfMedia:
            logging.info("Playing...")
            self.mediaPlayer.play()


class VideoPlayer(QtWidgets.QWidget):
    """
    Displays the videos of the session to the user. Can display multiple different laps side by side.
    """

    # Emitted when the primary video is playing, and the position is updated. Emitted with position as an int, as
    # milliseconds since the beginning of the video
    positionChanged = pyqtSignal(int)

    def __init__(self, parent = None):
        super().__init__(parent = parent)
        
        # A list of the different laps displayed as LapViewer widgets
        self.lapViewers = list()

        # The file path to the session's video
        self.source: QUrl = None
        
        self.lt = QtWidgets.QHBoxLayout()
        self.setLayout(self.lt)

        self.setStatusTip("Video player: Plays footage from your session.")
    
    def addViewer(self, position: int = 0):
        """
        Adds a new viewer into the widget, starting at the given point in the video.

        Parameters
        ----------
        position : The position that the video should start at, given as milliseconds since the beginning of the video.
        """

        if self.source is None:
            return
        
        newLap = LapViewer(self.source, position)
        self.lapViewers.append(newLap)
        self.lt.addWidget(newLap)
    
    def setSource(self, filePath: str):
        """
        Sets a new video source for the player.

        Parameters
        ----------

        filePath : The path to the new video source. If the suffix is not a supported
        type (eg. mp4), it will be converted to one.
        """

        path = pathlib.Path(filePath).resolve()

        # Find an mp4 video file with the same name
        if path.suffix != ".mp4":
            path = path.with_suffix(".mp4")

        if not path.exists():
            dlg = QtWidgets.QMessageBox(self)
            dlg.setWindowTitle("Video file not loaded.")
            dlg.setText('The video file "{}" could not be loaded.'.format(str(path)))
            dlg.exec()
            return
        
        self.source = QUrl.fromLocalFile(str(path))
        logging.info("Loaded video file")

        # Testing the video player displaying multiple viewpoints
        logging.info("Testing the video player...")
        self.addViewer(20000)
        self.addViewer(3000)  # 3 Seconds into the video
    
    def stop(self):
        """Stops the lap viewers and resets them to their given start positions"""
        for viewer in self.lapViewers:
            viewer.stop()
    
    def playPause(self, play: bool):
        """Toggles the video playback"""
        if play:
            for viewer in self.lapViewers:
                viewer.play()
        else:
            for viewer in self.lapViewers:
                viewer.pause()


class RecordStatusWidget(QtWidgets.QFrame):
    """Displays the current record config settings and status of the recording"""

    def __init__(self, port: str, ip: str, camera: str = None):
        super().__init__()

        layout = QtWidgets.QHBoxLayout()
        self.currentPortLabel = QtWidgets.QLabel("Port: {}".format(port))
        layout.addWidget(self.currentPortLabel)

        self.ipLabel = QtWidgets.QLabel("IP: {}".format(ip))
        layout.addWidget(self.ipLabel)

        self.cameraLabel = QtWidgets.QLabel("Camera: {}".format(camera))
        layout.addWidget(self.cameraLabel)

        self.setLayout(layout)
    
    #@pyqtSlot(str)
    def update(self, port: str, camera: str):
        """Updates the widget with new record settings"""
        self.currentPortLabel.setText("Port: {}".format(port))
        self.cameraLabel.setText("Camera: {}".format(camera))


class MainWindow(QtWidgets.QMainWindow):

    def __init__(self):
        super().__init__()

        parentDir = pathlib.Path(__file__).parent.parent.resolve()

        self.sessionManager = SessionManager(self)

        # Central widget ----------------------

        self.videoPlayer = VideoPlayer(self)
        self.setCentralWidget(self.videoPlayer)

        # Add the Toolbar and Actions --------------------------

        toolbar = QtWidgets.QToolBar()
        toolbar.setIconSize(QSize(16, 16))
        self.addToolBar(toolbar)

        # Status bar at the bottom of the application
        self.setStatusBar(QtWidgets.QStatusBar(self))

        # Action to play the videos and animate the graphs
        playPauseAction = QAction(QIcon(str(parentDir / pathlib.Path("assets/icons/control-play-pause.png"))), "Play/Pause", self)
        playPauseAction.setCheckable(True)
        playPauseAction.setShortcut(QKeySequence("Space"))
        playPauseAction.setStatusTip("Play/Pause Button: Plays or pauses the footage and the telemetry graphs.")
        playPauseAction.triggered.connect(self.videoPlayer.playPause)
        toolbar.addAction(playPauseAction)

        # Action to stop and skip to the beginning of the footage
        stopAction = QAction(QIcon(str(parentDir / pathlib.Path("assets/icons/control-stop.png"))), "Stop", self)
        stopAction.setStatusTip("Stop Button: Stops the footage and skips to the beginning.")
        stopAction.triggered.connect(self.videoPlayer.stop)
        toolbar.addAction(stopAction)

        # Action to open a new session, to load the telemetry csv files and the associated mp4 video with the same name
        openSessionAction = QAction(QIcon(str(parentDir / pathlib.Path("assets/icons/folder-open-document.png"))), "Open Session", self)
        openSessionAction.setShortcut(QKeySequence("Ctrl+O"))
        openSessionAction.setStatusTip("Open Session: Opens a CSV telemetry file (and video if there is one) to be analysed.")
        openSessionAction.triggered.connect(self.sessionManager.openSessions)
        toolbar.addAction(openSessionAction)

        # Action to start/stop recording a session (Record UDP data and a video input source)
        recordSessionAction = QAction(QIcon(str(parentDir / pathlib.Path("assets/icons/control-record.png"))), "Record Session", self)
        recordSessionAction.setShortcut(QKeySequence("Ctrl+R"))
        recordSessionAction.setCheckable(True)
        recordSessionAction.setStatusTip("Record Session: Starts recording Forza data and an accompanying video source.")
        #recordSessionAction.triggered.connect()
        toolbar.addAction(recordSessionAction)

        # Action to change the record config settings
        recordConfigAction = QAction(QIcon(str(parentDir / pathlib.Path("assets/icons/gear.png"))), "Record Config", self)
        recordConfigAction.setShortcut(QKeySequence("Ctrl+S"))
        recordConfigAction.setStatusTip("Record Config: Change the telemetry and video recording settings.")
        #recordConfigAction.triggered.connect(self.configureRecord)
        toolbar.addAction(recordConfigAction)

        # Add the menu bar and connect actions ----------------------------
        menu = self.menuBar()

        fileMenu = menu.addMenu("&File")
        fileMenu.addAction(openSessionAction)

        actionsMenu = menu.addMenu("&Actions")
        actionsMenu.addAction(playPauseAction)
        actionsMenu.addAction(stopAction)

        recordMenu = menu.addMenu("&Record")
        recordMenu.addAction(recordSessionAction)

        # Contains actions to open/close the dock widgets
        viewMenu = menu.addMenu("&View")

        # Add the Dock widgets, eg. graph and data table ---------------------

        # Record status widget
        """
        recordStatusWidget = RecordStatusWidget(self.record.port, self.record.ip)
        recordStatusDockWidget = QtWidgets.QDockWidget("Record Status", self)
        recordStatusDockWidget.setAllowedAreas(Qt.DockWidgetArea.TopDockWidgetArea | Qt.DockWidgetArea.BottomDockWidgetArea)
        recordStatusDockWidget.setWidget(recordStatusWidget)
        recordStatusDockWidget.setStatusTip("Record Status: Displays the main settings and status of the recording.")
        self.addDockWidget(Qt.DockWidgetArea.TopDockWidgetArea, recordStatusDockWidget)
        self.record.statusUpdate.connect(recordStatusWidget.update)
        """

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

        # plot widget
        self.plotWidget = MultiPlotWidget(self.sessionManager)
        sessionOverviewWidget.toggleLapFocus.connect(self.plotWidget.toggleLap)
        self.sessionManager.sessionReset.connect(self.plotWidget.reset)

        plotDockWidget = QtWidgets.QDockWidget("Telemetry plots", self)
        plotDockWidget.setAllowedAreas(Qt.DockWidgetArea.TopDockWidgetArea | Qt.DockWidgetArea.BottomDockWidgetArea)
        plotDockWidget.setWidget(self.plotWidget)
        plotDockWidget.setStatusTip("Telemetry plot: Displays the telemetry data from the session.")
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, plotDockWidget)

        # Add an action to the menu bar to open/close the dock widgets
        viewMenu.addAction(plotDockWidget.toggleViewAction())
        #viewMenu.addAction(recordStatusDockWidget.toggleViewAction())
