from PySide6 import QtWidgets, QtMultimediaWidgets, QtMultimedia
from PySide6.QtCore import Slot, QThread, QObject, Signal, Qt, QSize
from PySide6.QtGui import QAction, QIcon, QKeySequence

import pyqtgraph as pg
import numpy as np

from fdp import ForzaDataPacket

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



class Worker(QObject):
    """
    Listens for incoming forza UDP packets and communicates to QWidgets when
    a packet is collected
    """
    finished = Signal()
    collected = Signal(bytes)

    @Slot()
    def __init__(self, port:int):
        super(Worker, self).__init__()
        self.working = True
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(0)  # Set to non blocking, so the thread can be terminated without the socket blocking forever
        self.socketTimeout = 1
        self.port = port

    def work(self):
        """Binds the socket and starts listening for packets"""
        self.sock.bind(('', self.port))
        logging.info("Started listening on port {}".format(self.port))

        while self.working:
            try:
                ready = select.select([self.sock], [], [], self.socketTimeout)
                if ready[0]:
                    data, address = self.sock.recvfrom(1024)
                    logging.debug('received {} bytes from {}'.format(len(data), address))
                    self.collected.emit(data)
                else:
                    logging.debug("Socket timeout")
            except BlockingIOError:
                logging.info("Could not listen to {}, trying again...".format(address))
        
        # Close the socket after the player wants to stop listening, so that
        # a new socket can be created using the same port next time
        #self.sock.shutdown(socket.SHUT_RDWR)
        self.sock.close()
        logging.info("Socket closed.")
        self.finished.emit()


class Lap():
    """Stores all the important collected and calculated data from a single lap"""

    def __init__(self):
        pass


class Session(QObject):
    """
    Stores the telemetry data and associated calculated data for the currently opened session. A session
    represents a single unit of time's worth of continuously logged packets saved as a csv file.
    """

    # Emitted when the session object is updated so widgets can display the latest values.
    # Contains a dictionary of only the values that were updated. If a value stays the same between packets,
    # it will not be present.
    updated = Signal()
    newLapIndexes = []  # Stores the first index of each new lap

    def __init__(self):
        super().__init__()

    @Slot()
    def update(self, filePath: str) -> bool:
        """
        Updates the currently opened session using a filepath to the csv telemetry file.
        Returns True if the session was updated successfully, false otherwise.

        Parameters
        ----------
        filePath : The path to the CSV telemetry file
        """
        self.data = np.genfromtxt(filePath, delimiter=",", names=True)
        self.newLapIndexes.append(0)

        # Update the newLapIndexes list and create new lap objects

        """
        currentLap = 0
        currentIndex = 0
        lapView = self.data['lap_no']
        for x in np.nditer(lapView):
            if x != currentLap:
                self.newLapIndexes.append(i)
                currentLap = lapView[i]
        
        """
        
        self.updated.emit()
        return True


class VideoPlayer(QtWidgets.QWidget):
    """
    Displays the videos of the session to the user
    """

    def __init__(self):
        super().__init__()
        self.player = QtMultimedia.QMediaPlayer()
        self.videoWidget = QtMultimediaWidgets.QVideoWidget()

        self.player.setVideoOutput(self.videoWidget)
        
        layout = QtWidgets.QVBoxLayout()
        self.setLayout(layout)
        layout.addWidget(self.videoWidget)

        self.setStatusTip("Video player: Plays footage from your session.")
    

    def setSource(self, filePath: str):
        """
        Sets a new video source for the player.

        Parameters
        ----------

        filePath : The path to the new video source
        """

        # Do some error checking here. If file doesn't exist or is in the wront format, tell user
        self.player.setSource(filePath)
    

    @Slot()
    def playPause(self, play: bool):
        """Toggles the video playback"""
        if play:
            self.player.play()
        else:
            self.player.pause()


class PlotWidget(pg.GraphicsLayoutWidget):
    """Displays telemetry graphs in the upper or lower dock of the application"""

    def __init__(self):
        super().__init__()
    
    @Slot()
    def newSession(self):
        """Initialises the widget with a single graph of speed over distance of the fastest lap of the session"""
        pass

    def addNewGraph(self, yAxisLabel: str):
        """Adds a new graph to the layout. The Y axis can be any parameter that is present in the Session object"""
        pass

class MainWindow(QtWidgets.QMainWindow):

    # Maintains the state of a single session. Users can save this session to a file, or reset it.
    session = Session()

    def __init__(self):
        super().__init__()

        parentDir = pathlib.Path(__file__).parent.parent.resolve()

        self.worker = None
        self.thread = None

        self.dashConfig = dict()
        self.ip = ""

        self.videoPlayer = VideoPlayer()
        self.setCentralWidget(self.videoPlayer)

        # Hard codes a video file to TEST the video player
        #self.videoPlayer.player.setSource(str(videoPath))

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
        stopAction.triggered.connect(self.videoPlayer.player.stop)
        toolbar.addAction(stopAction)

        # Action to open a new session, to load the telemetry csv file and the associated mp4 video with the same name
        openSessionAction = QAction(QIcon(str(parentDir / pathlib.Path("assets/icons/folder-open-document.png"))), "Open Session", self)
        openSessionAction.setShortcut(QKeySequence("Ctrl+O"))
        openSessionAction.setStatusTip("Open Session: Opens a CSV telemetry file (and video if there is one) to be analysed.")
        openSessionAction.triggered.connect(self.openSession)
        toolbar.addAction(openSessionAction)

        # Add the menu bar and connect actions ----------------------------
        menu = self.menuBar()

        fileMenu = menu.addMenu("&File")
        #fileMenu.addAction(openVideoAction)
        fileMenu.addAction(openSessionAction)

        actionsMenu = menu.addMenu("&Actions")
        actionsMenu.addAction(playPauseAction)
        actionsMenu.addAction(stopAction)

        # Add the Dock widgets, eg. graph and data table ---------------------

        self.plotWidget = PlotWidget()
        #self.graphWidget = pg.PlotWidget()

        graphDockWidget = QtWidgets.QDockWidget("Telemetry Graphs", self)
        graphDockWidget.setAllowedAreas(Qt.TopDockWidgetArea | Qt.BottomDockWidgetArea)
        graphDockWidget.setWidget(self.plotWidget)
        graphDockWidget.setStatusTip("Telemetry Graph: Displays the telemetry data from the session.")
        self.addDockWidget(Qt.BottomDockWidgetArea, graphDockWidget)
    

    @Slot()
    def openSession(self):
        """Opens and loads the telemetry csv file and accompanying video footage (if there is any) into the application"""

        # Dialog to get the csv file
        dlg = QtWidgets.QFileDialog(self)
        dlg.setWindowTitle("Open Session")
        dlg.setFileMode(QtWidgets.QFileDialog.ExistingFile)
        dlg.setNameFilter("*.csv")

        # If user presses okay
        if dlg.exec():
            filePathList = dlg.selectedFiles()
            logging.info("Opened file: {}".format(filePathList[0]))
            if not self.session.update(filePathList[0]):  # Update the session with new file
                # open dialog box telling user the csv file couldnt be loaded
                dlg = QtWidgets.QMessageBox(self)
                dlg.setWindowTitle("Telemetry file not loaded.")
                dlg.setText('The file "{}" could not be loaded.'.format(filePathList[0]))
                dlg.exec()
                return

            filePath = pathlib.Path(filePathList[0]).resolve()

            # Try to find a video with the same file name in the same folder as the telemetry file and load it
            videoFilePath = filePath.with_suffix(".mp4")

            if not videoFilePath.exists():
                # open dialog box telling user no video could be found
                dlg = QtWidgets.QMessageBox(self)
                dlg.setWindowTitle("Video file not loaded.")
                dlg.setText('The video file "{}" could not be loaded.'.format(str(videoFilePath)))
                dlg.exec()
                return
            
            self.videoPlayer.player.setSource(str(videoFilePath))


    @Slot()
    def toggle_loop(self, checked):
        """
        Starts/stops listening for Forza UDP packets
        """
        if not checked:
            self.worker.working = False
            logging.debug("Worker set to false")
            self.thread.quit()
        else:
            logging.debug("Thread started")
            self.worker = Worker(self.dashConfig["port"])  # a new worker
            self.thread = QThread()  # a new thread to listen for packets
            self.worker.moveToThread(self.thread)
            # move the worker into the thread, do this first before connecting the signals
            self.thread.started.connect(self.worker.work)
            # begin worker object's loop when the thread starts running
            self.worker.collected.connect(self.onCollected)  # Update the widgets every time a packet is collected
            self.worker.finished.connect(self.loop_finished)  # Do something when the worker loop ends

            self.worker.finished.connect(self.thread.quit)  # Tell the thread to stop running
            self.worker.finished.connect(self.worker.deleteLater)  # Have worker mark itself for deletion
            self.thread.finished.connect(self.thread.deleteLater)  # Have thread mark itself for deletion
            # Make sure those last two are connected to themselves or you will get random crashes
            self.thread.start()

    def onCollected(self, data):
        """Called when a single UDP packet is collected. Receives the unprocessed
        packet data, transforms it into a Forza Data Packet and emits the update signal with
        that forza data packet object, and the dashboard config dictionary. If the race
        is not on, it does not emit the signal"""

        logging.debug("onCollected: Received Data")
        fdp = ForzaDataPacket(data)

        isRacing = bool(fdp.is_race_on)
        self.isRacing.emit(isRacing)

        self.session.update(fdp)

        if not fdp.is_race_on:
            return
        self.updateSignal.emit(fdp, self.dashConfig)

    def loop_finished(self):
        """Called after the port is closed and the dashboard stops listening to packets"""
        logging.info("Finished listening")
