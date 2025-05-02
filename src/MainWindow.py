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

"""
Currently:
- working on Session.update: Need to:
    - Remove all the rows of data at the beginning of the data ndarray that have negative distance (all the data points
    collected before the player has crossed the start line) (Do this when creating the ndarray from the csv file)
    - Create a Lap object for each lap in the session that includes:
        - Distance traveled
        - Lap time
        - best lap boolean (True if best lap)
        - In lap boolean (if tyres change wear)
        - Out lap boolean (if tyres change wear)
        and more...
- When session is updated, initialise the graph widget with a speed over distance graph
"""


class MultiPlotWidget(QtWidgets.QWidget):
    """Displays multiple telemetry plots generated from the session telemetry data"""

    def __init__(self):
        super().__init__()

        # Contains all the plots currently displayed in the layout
        self.plots = dict()
        self.data = None  # The numpy telemetry data

        self.lt = QtWidgets.QVBoxLayout()
        self.setLayout(self.lt)
    
    @pyqtSlot(np.ndarray)
    def update(self, data: np.ndarray):
        """Creates new plots from the new session telemetry data, but doesn't display them right away"""
        self.data = data

        self.addNewPlot("time", "cur_lap_time")
        self.addNewPlot("dist_traveled", "speed")
        self.addNewPlot("dist_traveled", "steer")

        t = self.plots["cur_lap_time"].getPlotItem().getViewBox()
        u = self.plots["speed"].getPlotItem().getViewBox()
        v = self.plots["steer"].getPlotItem().getViewBox()

        v.setXLink(t)

    def addNewPlot(self, x: str, y: str):
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
    
    def movePlayerHead(position):
        """Moves the vertical line of the graphs"""
        pass


class Session(QObject):
    """
    Stores the telemetry data and associated calculated data for a currently opened session. A session
    represents a single unit of time's worth of continuously logged packets saved as a csv file.
    """

    class Lap():
        """Stores all the important collected and calculated data from a single lap"""

        def __init__(self):
            self.lapNumber: int = None

            # Uses the last_lap_time parameter from a packet collected from the next lap. This is because the last packet recorded
            # during a lap will be slightly before the finish line, and so the recorded lap time will be slightly quicker than the
            # real lap time.
            # The only problem with this is the last lap will have to use a lap time collected from the last packet. This means the last
            # lap's lap time will be slightly quicker than real (about 1/60th of a second, or 0.017s). But it's better to have an accurate
            # lap time for 99% of the laps than to be consistently slightly wrong every lap.
            self.lapTime: int = None  # In seconds

            self.lapBegin: int = None  # Time the lap began in seconds relative to the start of the race (cur_race_time)
            self.fastest: bool = False

            self.inLap: bool = False
            self.outLap: bool = False


    def __init__(self, data: np.ndarray, filePath = pathlib.Path, parent = None):
        super().__init__(parent = parent)

        # Contains all the Lap objects generated from the telemetry, in order from lap 0 to lap n or lap n - 1.
        # Lap n may or may not be included, depending on if the user finishes it (like in a lapped race) or if
        # they quit in the middle (like in free practice).

        # An ordered dictionary of Lap objects. Indexed by lap number, may not always start at 1
        self.laps = OrderedDict() 

        self.data = Session._sortData(data)
        self.filePath = filePath  # A pathlib.Path object
        self.name = str(filePath.stem)  # Name of the session, equal to the name of the file

        self.laps = Session._getLaps(self.data)
                
        logging.info("Created Session")
    
    def _sortData(data: np.ndarray):
        """Sorts the packet numpy array based on the timestamp_ms field. This is Forza's internal timestamp and will ensure rows will be sorted
        by the order they were produced, instead of the order they were received as UDP may be unreliable. This is an unsigned 32 bit int, so
        can overflow after about 50 days. This will detect an overflow and re-sort the fields to restore true chronological order."""

        #threshold = 3600000  # If the gap between adjacent timestamps is larger than this, an overflow has occurred (3600000ms is about an hour)
        threshold = 1000  # For testing
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

    def _getLaps(data: np.ndarray):
        """
        Generates a list of new Lap objects given the sorted packet data. The very last lap will only be
        counted if it is a complete lap, ie. the user didn't quit in the middle of a lap.

        Parameters
        ----------
        data : A 1d numpy array containing the sorted session telemetry data
        """

        # Get the last packet of each lap and append onto lapRows
        lapRows = []
        lastRow = data[0]
        for row in data:
            if row["lap_no"] == lastRow["lap_no"] + 1:
                lapRows.append(lastRow)
            lastRow = row
        
        # Add the very last packet onto lapRows (will be used to help create the second to last lap even if it's incomplete)
        lapRows.append(data[-1])
        includeLastLap = False  # Whether the last lap is complete and will be included

        # Find the fastest lap time, so that it can be compared with each lap
        # Use last_lap_time because best_lap_time only counts Forza Clean laps
        bestLapTime = data["last_lap_time"].min()
        
        # Include the last lap if the distance traveled that lap roughly matches the first lap
        tolerance = 1  # lap should be within the the first lap's distance +/- tolerance in metres

        totalDistance = data[-1]["dist_traveled"]
        lastLapDistance = totalDistance - data[-1]["dist_traveled"]
        firstLapDistance = data[0]["dist_traveled"]

        if lastLapDistance > firstLapDistance - tolerance and lastLapDistance < firstLapDistance + tolerance:
            includeLastLap = True
        
        # If last lap is complete, consider it for fastest lap
        if includeLastLap:
            lastLapTime = data["cur_lap_time"][-1]
            if lastLapTime < bestLapTime:
                bestLapTime = lastLapTime
        
        # Get the number of laps in the session
        numberOfLaps = data["lap_no"].max() - data["lap_no"].min()

        # Separate the laps into a number of views
        lapViews = OrderedDict()
        uniqueLaps, lapStartIndex = np.unique(data["lap_no"], return_index=True)
        lapStartIndex = np.append(lapStartIndex, [len(data)])

        for i in range(0, len(uniqueLaps)):
            lapViews[uniqueLaps[i]] = data[lapStartIndex[i]:lapStartIndex[i+1]]
        
        #--------------------------------------------------------------

        """
        newLapList = []
        if len(laps) > 1:
            i = 0
            for i in range(0, len(laps) - 1):
                row = laps[row]
                nextRow = laps[row + 1]

                newLap = Session.Lap()
                newLap.lapNumber = row["lap_no"]
                newLap.lapTime = nextRow["last_lap_time"]
                newLap.lapBegin = row["cur_race_time"]

                if newLap["best_lap_time"] == bestLapTime:
                    newLap.fastest = True
                
                # Set inLap and outLap

                newLapList.append(newLap)
            
            # Add the very last row if the distance traveled that lap roughly matches the first lap
            tolerance = 1  # lap should be within the the first lap's distance +/- tolerance

            totalDistance = laps[-1]["dist_traveled"]
            lastLapDistance = totalDistance - laps[-1]["dist_traveled"]
            firstLapDistance = laps[0]["dist_traveled"]

            if lastLapDistance > firstLapDistance - tolerance and lastLapDistance < firstLapDistance + tolerance:
                row = laps[-1]

                newLap = Session.Lap()
                newLap.lapNumber = row["lap_no"]
                newLap.lapTime = row["cur_lap_time"]
                newLap.lapBegin = row["cur_race_time"]

                if newLap["best_lap_time"] == bestLapTime:
                    newLap.fastest = True
                
                # Set inLap and outLap

                newLapList.append(newLap)
                """


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

        # A dictionary of Session objects to store telemetry from multiple loaded CSV files
        # Keys are the filename without the extension, and Items are the Session objects
        self.sessions = dict()

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

        # Action to open a new session, to load the telemetry csv file and the associated mp4 video with the same name
        openSessionAction = QAction(QIcon(str(parentDir / pathlib.Path("assets/icons/folder-open-document.png"))), "Open Session", self)
        openSessionAction.setShortcut(QKeySequence("Ctrl+O"))
        openSessionAction.setStatusTip("Open Session: Opens a CSV telemetry file (and video if there is one) to be analysed.")
        openSessionAction.triggered.connect(self.openSessions)
        toolbar.addAction(openSessionAction)

        # Action to start/stop recording a session (Record UDP data and a video input source)
        recordSessionAction = QAction(QIcon(str(parentDir / pathlib.Path("assets/icons/control-record.png"))), "Record Session", self)
        recordSessionAction.setShortcut(QKeySequence("Ctrl+R"))
        recordSessionAction.setCheckable(True)
        recordSessionAction.setStatusTip("Record Session: Starts recording Forza data and an accompanying video source.")
        #recordSessionAction.triggered.connect()
        toolbar.addAction(recordSessionAction)

        # Action to change the record condig settings
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

        # plot widget
        self.plotWidget = MultiPlotWidget()
        #self.session.updated.connect(self.plotWidget.update)

        plotScrollArea = QtWidgets.QScrollArea()  # Put the plots in this to make it scrollable
        plotScrollArea.setWidget(self.plotWidget)
        plotScrollArea.setWidgetResizable(True)
        plotScrollArea.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)

        plotDockWidget = QtWidgets.QDockWidget("Telemetry plots", self)
        plotDockWidget.setAllowedAreas(Qt.DockWidgetArea.TopDockWidgetArea | Qt.DockWidgetArea.BottomDockWidgetArea)
        plotDockWidget.setWidget(plotScrollArea)
        plotDockWidget.setStatusTip("Telemetry plot: Displays the telemetry data from the session.")
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, plotDockWidget)

        # Add an action to the menu bar to open/close the dock widgets
        viewMenu.addAction(plotDockWidget.toggleViewAction())
        #viewMenu.addAction(recordStatusDockWidget.toggleViewAction())
    
    def openSessions(self):
        """Opens and loads the telemetry csv files into the sessions dict"""

        # Dialog to get the csv files
        dlg = QtWidgets.QFileDialog(self)
        dlg.setWindowTitle("Open Sessions")
        dlg.setFileMode(QtWidgets.QFileDialog.FileMode.ExistingFile)
        dlg.setNameFilter("*.csv")

        # If user presses okay
        if dlg.exec():
            filePathList = dlg.selectedFiles()
            logging.info("Found Files: {}".format(filePathList))

            for filePath in filePathList:
                data = np.genfromtxt(filePath, delimiter=",", names=True)  # Numpy loads the csv file into a numpy array
                newSession = Session(data, pathlib.Path(filePath).resolve(), self)
                fileName = pathlib.Path(filePath).resolve().stem
                self.sessions[fileName] = newSession  # Create a new entry in the sessions dict
