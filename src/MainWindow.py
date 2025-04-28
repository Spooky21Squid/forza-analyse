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
        
    
    @pyqtSlot()
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
        #newPlot = self.addPlot(title=y)
        newPlot.setMinimumHeight(300)
        logging.info("Min size hint of newPlot: {} by {}".format(newPlot.minimumSize().width(), newPlot.minimumSize().height()))
        yAxis = self.data[y]
        newPlot.plot(xAxis, yAxis)

        vLine = pg.InfiniteLine(angle=90, movable=False)  # Player head
        newPlot.addItem(vLine, ignoreBounds = True)

        self.plots[y] = newPlot
        self.lt.addWidget(newPlot)


class Session(QObject):
    """
    Stores the telemetry data and associated calculated data for the currently opened session. A session
    represents a single unit of time's worth of continuously logged packets saved as a csv file.
    """

    # Emitted when the session object is updated so widgets can display the latest values from the numpy array
    updated = pyqtSignal(np.ndarray)

    def __init__(self):
        super().__init__()
        #self.newLapIndexes = []  # Stores the first index of each new lap

    @pyqtSlot()
    def update(self, data: np.ndarray) -> bool:
        """
        Updates the currently opened session using a numpy array containing the udp data.
        Returns True if the session was updated successfully, false otherwise.

        Parameters
        ----------
        data : The telemetry data
        """
        self.data = data

        # Get the best lap time
        masked = np.ma.masked_equal(data["best_lap_time"], 0, copy=False)  # Mask out the rows where best lap time is 0
        bestLapTime = masked.min()
        logging.info("Best lap time: {}".format(bestLapTime))

        # ---------------
        # Group the records by lap, then just use the last record in each group to get lap time, dist etc
        # Can pre-define sectors for each lap based on distance (recorded by forza), then get the cur lap time at that distance
        # ---------------
        
        self.updated.emit(self.data)
        logging.info("Updated session telemetry data")
        return True


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

        self.mediaPlayer.setSource(source)

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

    def __init__(self):
        super().__init__()
        
        # A list of the different laps displayed as LapViewer widgets
        self.lapViewers = list()

        # The file path to the session's video
        self.source = None
        
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

        filePath : The path to the new video source
        """

        # Do some error checking here. If file doesn't exist or is in the wront format, tell user
        self.source = filePath
    
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

    # Stores previously recorded telemetry data
    session = Session()

    def __init__(self):
        super().__init__()

        parentDir = pathlib.Path(__file__).parent.parent.resolve()

        # Central widget ----------------------

        self.videoPlayer = VideoPlayer()
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
        openSessionAction.triggered.connect(self.openSession)
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
        self.session.updated.connect(self.plotWidget.update)

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

    def openSession(self):
        """Opens and loads the telemetry csv file and accompanying video footage (if there is any) into the application"""

        # Dialog to get the csv file
        dlg = QtWidgets.QFileDialog(self)
        dlg.setWindowTitle("Open Session")
        dlg.setFileMode(QtWidgets.QFileDialog.FileMode.ExistingFile)
        dlg.setNameFilter("*.csv")

        # If user presses okay
        if dlg.exec():
            filePathList = dlg.selectedFiles()
            logging.info("Opened file: {}".format(filePathList[0]))
            data = np.genfromtxt(filePathList[0], delimiter=",", names=True)  # Numpy loads the csv file into a numpy array
            if not self.session.update(data):  # Update the session with new udp data
                # open dialog box telling user the csv file couldnt be loaded
                dlg = QtWidgets.QMessageBox(self)
                dlg.setWindowTitle("Telemetry file not loaded.")
                dlg.setText('The file "{}" could not be loaded.'.format(filePathList[0]))
                dlg.exec()
                return

            # Try to find a video with the same file name in the same folder as the telemetry file and load it
            filePath = pathlib.Path(filePathList[0]).resolve()
            videoFilePath = filePath.with_suffix(".mp4")

            if not videoFilePath.exists():
                # open dialog box telling user no video could be found
                dlg = QtWidgets.QMessageBox(self)
                dlg.setWindowTitle("Video file not loaded.")
                dlg.setText('The video file "{}" could not be loaded.'.format(str(videoFilePath)))
                dlg.exec()
                return
            
            self.videoPlayer.setSource(QUrl.fromLocalFile(str(videoFilePath)))
            logging.info("Loaded video file")

            # Testing the video player displaying multiple viewpoints
            logging.info("Testing the video player...")
            self.videoPlayer.addViewer(20000)
            self.videoPlayer.addViewer(3000)  # 3 Seconds into the video