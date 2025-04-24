from PySide6 import QtWidgets, QtMultimediaWidgets, QtMultimedia
from PySide6.QtCore import Slot, QThread, QObject, Signal, Qt, QSize
from PySide6.QtGui import QAction, QIcon, QKeySequence
from PySide6.QtMultimedia import QMediaDevices

import pyqtgraph as pg
import numpy as np

from fdp import ForzaDataPacket
import Utility

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


class MultiPlotWidget(pg.GraphicsLayoutWidget):
    """Displays multiple telemetry plots generated from the session telemetry data"""

    def __init__(self, parent=None, show=False, size=None, title=None, **kargs):
        super().__init__(parent, show, size, title, **kargs)

        # Contains all the plots currently displayed in the layout
        self.plots = dict()
        self.data = None  # The numpy telemetry data
    
    @Slot()
    def update(self, data: np.ndarray):
        """Creates new plots from the new session telemetry data, but doesn't display them right away"""
        self.data = data

        self.addNewPlot("time", "cur_lap_time")
        self.addNewPlot("dist_traveled", "speed")
        self.addNewPlot("dist_traveled", "steer")
        
    
    @Slot()
    def addNewPlot(self, x: str, y: str):
        """
        Adds a new plot to the layout
        
        Parameters
        ----------
        x : The parameter to assign to the x axis, eg. dist_traveled
        y : The parameter to assign to the y axis, eg. speed
        """
        
        xAxis = self.data[x]
        newPlot = self.addPlot(title=y)
        newPlot.setMinimumHeight(300)
        logging.info("Min size hint of newPlot: {} by {}".format(newPlot.minimumSize().width(), newPlot.minimumSize().height()))
        yAxis = self.data[y]
        newPlot.plot(xAxis, yAxis)
        self.plots[y] = newPlot
        self.nextRow()


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
        self.lapNumber: int = None
        self.fastest: bool = None
        self.inLap: bool = None
        self.outLap: bool = None
        self.lapTime: int = None  # In seconds
        self.lapBegin: int = None  # Time the lap began in seconds relative to the start of the race (cur_race_time)



class RecordConfig(QObject):
    """Stores all the configuration settings for recording a session"""

    updated = Signal(str)  # Emits a signal when the object is updated and sends the new port number as a string

    def __init__(self):
        super().__init__()
        self.port = 1337  # Port to listen to for Forza data

        # Dict of the parameters the user has chosen to save to the csv telemetry file
        # All params initialised to True
        self.selectedParams = dict()
        for param in Utility.ForzaSettings.params:
            self.selectedParams[param] = True
        
        # Overrides the selectedParams if True, and records all the parameters. If False, only selectedParams are recorded
        self.allParams = True
        
        # IP address that Forza should send to - Can be None if IP address couldn't be received
        self.ip = Utility.getIP()
    
    @Slot()
    def update(self, port: str, allParams: bool, selectedParams: dict):
        """Updates the config object"""
        
        self.port = port
        self.allParams = allParams

        if not allParams:
            for param, selected in selectedParams.items():
                self.selectedParams[param] = selected
        
        self.updated.emit(port)

        logging.info("Updated record settings")


class Session(QObject):
    """
    Stores the telemetry data and associated calculated data for the currently opened session. A session
    represents a single unit of time's worth of continuously logged packets saved as a csv file.
    """

    # Emitted when the session object is updated so widgets can display the latest values from the numpy array
    updated = Signal(np.ndarray)

    def __init__(self):
        super().__init__()
        #self.newLapIndexes = []  # Stores the first index of each new lap

    @Slot()
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


class RecordConfigForm(QtWidgets.QWidget):
    """Form to adjust the settings for recording such as port number and which parameters to save"""

    # Emitted when a user saves the form by pressing the save button, and sends the data
    updated = Signal(str, bool, dict)
    
    def __init__(self):
        super().__init__()

        self.port = QtWidgets.QSpinBox(minimum=1025, maximum=65535, value=1337)
        self.allParams = QtWidgets.QCheckBox()
        self.allParams.setChecked(True)

        # Dictionary of all paramaters and their checkboxes
        self.paramDict = dict()
        for param in Utility.ForzaSettings.params:
            checkBox = QtWidgets.QCheckBox()
            checkBox.setChecked(True)
            self.paramDict[param] = checkBox

        formLayout = QtWidgets.QFormLayout()
        formLayout.addRow("Port", self.port)
        formLayout.addRow("Record All", self.allParams)

        # Add the checkbox for each parameter
        for param, checkBox in self.paramDict.items():
            formLayout.addRow(param, checkBox)

        saveButton = QtWidgets.QPushButton("Save")
        saveButton.clicked.connect(self.saved)

        layout = QtWidgets.QVBoxLayout()
        layout.addWidget(saveButton)
        layout.addLayout(formLayout)

        self.setLayout(layout)
    
    @Slot()
    def saved(self):
        """Collects the form data to be saved and emits a signal containing the data"""

        newParamDict = dict()
        for param, checkBox in self.paramDict.items():
            newParamDict[param] = checkBox.isChecked()

        self.updated.emit(str(self.port.value()), self.allParams.isChecked(), newParamDict)


class RecordStatusWidget(QtWidgets.QFrame):
    """Displays the current record config settings and status of the recording"""

    def __init__(self, port: str, ip: str):
        super().__init__()

        layout = QtWidgets.QHBoxLayout()
        self.currentPortLabel = QtWidgets.QLabel("Port: {}".format(port))
        layout.addWidget(self.currentPortLabel)

        self.ipLabel = QtWidgets.QLabel("IP: {}".format(ip))
        layout.addWidget(self.ipLabel)

        self.setLayout(layout)
    
    @Slot()
    def update(self, port: str):
        """Updates the widget with new record settings"""
        self.currentPortLabel.setText("Port: {}".format(port))


class RecordDialog(QtWidgets.QDialog):
    """Dialog that helps the user configure some settings to record telemetry and a video source"""

    def __init__(self, parent):
        super().__init__(parent)

        layout = QtWidgets.QVBoxLayout()

        self.setWindowTitle("Configure Record Settings")

        # Define the buttons at the bottom and connect them to the dialog
        buttons = (QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        self.buttonBox = (QtWidgets.QDialogButtonBox(buttons))
        self.buttonBox.accepted.connect(self.accept)
        self.buttonBox.rejected.connect(self.reject)

        inputs = QMediaDevices.videoInputs()
        logging.info("Inputs: {}".format(len(inputs)))
        for device in inputs:
            layout.addWidget(QtWidgets.QLabel("Device {}: {}".format(device.id, device.description)))

        #label = QtWidgets.QLabel("Record settings go here")

        #layout.addWidget(label)
        layout.addWidget(self.buttonBox)
        self.setLayout(layout)


class MainWindow(QtWidgets.QMainWindow):

    # Stores recorded telemetry data
    session = Session()

    # Stores all settings needed to record telemetry and video
    recordConfig = RecordConfig()

    def __init__(self):
        super().__init__()

        parentDir = pathlib.Path(__file__).parent.parent.resolve()

        self.worker = None
        self.thread = None

        self.dashConfig = dict()
        self.ip = ""

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
        stopAction.triggered.connect(self.videoPlayer.player.stop)
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
        recordConfigAction.triggered.connect(self.configureRecord)
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

        # Record settings form widget
        recordConfigForm = RecordConfigForm()
        recordConfigForm.updated.connect(self.recordConfig.update)

        formScrollArea = QtWidgets.QScrollArea()  # Put the form in this to make it scrollable
        formScrollArea.setWidget(recordConfigForm)
        formScrollArea.setWidgetResizable(True)
        formScrollArea.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        recordConfigFormDockWidget = QtWidgets.QDockWidget("Record Config Form", self)
        recordConfigFormDockWidget.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        recordConfigFormDockWidget.setWidget(formScrollArea)
        recordConfigFormDockWidget.setStatusTip("Record Config Form: Change the telemetry and video recording settings.")
        self.addDockWidget(Qt.RightDockWidgetArea, recordConfigFormDockWidget)

        # Record status widget
        recordStatusWidget = RecordStatusWidget(self.recordConfig.port, self.recordConfig.ip)
        recordStatusDockWidget = QtWidgets.QDockWidget("Record Status", self)
        recordStatusDockWidget.setAllowedAreas(Qt.TopDockWidgetArea | Qt.BottomDockWidgetArea)
        recordStatusDockWidget.setWidget(recordStatusWidget)
        recordStatusDockWidget.setStatusTip("Record Status: Displays the main settings and status of the recording.")
        self.addDockWidget(Qt.TopDockWidgetArea, recordStatusDockWidget)
        self.recordConfig.updated.connect(recordStatusWidget.update)

        # plot widget
        self.plotWidget = MultiPlotWidget()
        self.session.updated.connect(self.plotWidget.update)

        plotScrollArea = QtWidgets.QScrollArea()  # Put the plots in this to make it scrollable
        plotScrollArea.setWidget(self.plotWidget)
        plotScrollArea.setWidgetResizable(True)
        plotScrollArea.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)

        plotDockWidget = QtWidgets.QDockWidget("Telemetry plots", self)
        plotDockWidget.setAllowedAreas(Qt.TopDockWidgetArea | Qt.BottomDockWidgetArea)
        plotDockWidget.setWidget(plotScrollArea)
        plotDockWidget.setStatusTip("Telemetry plot: Displays the telemetry data from the session.")
        self.addDockWidget(Qt.BottomDockWidgetArea, plotDockWidget)

        # Add an action to the menu bar to open/close the dock widgets
        viewMenu.addAction(plotDockWidget.toggleViewAction())
        viewMenu.addAction(recordStatusDockWidget.toggleViewAction())
        viewMenu.addAction(recordConfigFormDockWidget.toggleViewAction())
    
    @Slot()
    def configureRecord(self):
        """Action to open and set the record settings"""

        dlg = RecordDialog(parent=self)
        if dlg.exec():
            logging.info("Record settings changed successfully")
        else:
            logging.info("Record settings could not be changed")
    

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
            data = np.genfromtxt(filePathList[0], delimiter=",", names=True)  # Numpy loads the csv file into a numpy array
            if not self.session.update(data):  # Update the session with new udp data
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
            logging.info("Loaded video file")

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
