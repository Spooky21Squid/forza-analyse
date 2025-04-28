from PyQt6 import QtWidgets, QtMultimedia
from PyQt6.QtCore import pyqtSlot, QThread, QObject, pyqtSignal, Qt, QSize, QUrl
from PyQt6.QtGui import QAction, QIcon, QKeySequence
from PyQt6.QtMultimedia import QMediaDevices, QCamera, QMediaCaptureSession, QCameraDevice, QCameraFormat
from PyQt6.QtMultimediaWidgets import QVideoWidget

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
    finished = pyqtSignal()
    collected = pyqtSignal(bytes)

    @pyqtSlot()
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


class Record(QObject):
    """Controls the recording of video and telemetry for a new session"""

    statusUpdate = pyqtSignal(str, str)  # Emits a signal when the object is updated and sends the new port number as a string

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

        # Whether the user wants to record video as well as telemetry (initialise to false as there are no initial camera devices)
        self.recordVideo = False

        # The camera settings to find the source of the recording
        self.cameraDevice: QCameraDevice = None

        # The chosen camera format
        self.cameraFormat: QCameraFormat = None

        # The file paths that the video and telemetry file should save to
        self.videoFilePath: pathlib.Path = None
        self.telemetryFilePath: pathlib.Path = None
    
    def ready(self) -> bool:
        """Returns True if the object is ready to record telemetry and video. False if not"""
        pass
    
    def startRecording(self):
        """Starts recording telemetry and video"""
        
        if not self.ready():
            return  # Maybe put a dialog here to tell user not ready to record and why

    def stopRecording(self):
        """Stops recording video and telemetry and saves the results"""
        pass

    #@pyqtSlot(str, bool, dict, QCameraDevice)
    def update(self, port: str = None, allParams: bool = None, selectedParams: dict = None,
               recordVideo: bool = None, cameraDevice: QCameraDevice = None, cameraFormat: QCameraFormat = None):
        """Updates the config object"""
        if port:
            self.port = port
        
        if allParams:
            self.allParams = allParams

        if not allParams and allParams is not None:
            if selectedParams:
                for param, selected in selectedParams.items():
                    self.selectedParams[param] = selected
        
        if cameraDevice:
            self.cameraDevice = cameraDevice
        
        if cameraDevice and cameraFormat:
            self.cameraFormat = cameraFormat
        
        if recordVideo is not None:
            self.recordVideo = recordVideo
        
        self.statusUpdate.emit(str(self.port), self.cameraDevice.description() if self.cameraDevice else "None")

        logging.info("Updated record settings")


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


class VideoPlayer(QtWidgets.QWidget):
    """
    Displays the videos of the session to the user
    """

    def __init__(self):
        super().__init__()
        self.player = QtMultimedia.QMediaPlayer()
        self.videoWidget = QVideoWidget()

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
    

    @pyqtSlot(bool)
    def playPause(self, play: bool):
        """Toggles the video playback"""
        if play:
            self.player.play()
        else:
            self.player.pause()


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


class CameraPreview(QObject):
    """A preview of the current chosen camera"""
    def __init__(self, parent = None):
        super().__init__(parent)

        self.videoWidget = QVideoWidget()
        self.videoWidget.setMinimumHeight(300)
        self.camera = QCamera()

        self.mediaCaptureSession = QMediaCaptureSession()
        self.mediaCaptureSession.setCamera(self.camera)
        self.mediaCaptureSession.setVideoOutput(self.videoWidget)
    
    def changeCameraFormat(self, index: int):
        """Changes the camera format when given its index"""
        formats = self.camera.cameraDevice().videoFormats()
        if index < len(formats):
            self.camera.setCameraFormat(formats[index])
            logging.info("Changed camera format")
    
    def changeCameraDevice(self, cameraDescription: str):
        """Changes the chosen camera device given its description"""
        for camera in QMediaDevices.videoInputs():
            if cameraDescription == camera.description():
                self.camera.setCameraDevice(camera)
                self.camera.start()


class RecordDialog(QtWidgets.QDialog):
    """Dialog that helps the user configure some settings to record telemetry and a video source"""

    save = pyqtSignal(int, bool, dict, bool, object, object)

    def __init__(self, parent):
        super().__init__(parent)

        layout = QtWidgets.QVBoxLayout()

        # Add a camera preview -----------
        self.cameraPreview = CameraPreview()
        self.availableCameras = QMediaDevices.videoInputs()

        layout.addWidget(self.cameraPreview.videoWidget)

        # layout for the form widget
        scrollArea = QtWidgets.QScrollArea()
        scrollArea.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        formWidget = QtWidgets.QWidget()
        scrollArea.setWidget(formWidget)
        scrollArea.setWidgetResizable(True)
        formLayout = QtWidgets.QFormLayout()  # For the port, video source, widgets etc
        formWidget.setLayout(formLayout)

        # Check box for recording video or not - also disables or enables the video input boxes
        self.recordVideo = QtWidgets.QCheckBox()
        self.recordVideo.setChecked(True)
        self.recordVideo.checkStateChanged.connect(self.disableEnableVideo)
        formLayout.addRow("Record Video", self.recordVideo)

        self.videoInputBox = QtWidgets.QComboBox()
        self.videoInputBox.setPlaceholderText("No video input selected")
        self.videoInputBox.currentTextChanged.connect(self.cameraPreview.changeCameraDevice)
        self.videoInputBox.currentTextChanged.connect(self.updateCameraFormatBox)
        formLayout.addRow("Video Input", self.videoInputBox)

        self.cameraFormatBox = QtWidgets.QComboBox()
        self.cameraFormatBox.setPlaceholderText("No video format selected")
        self.cameraFormatBox.currentIndexChanged.connect(self.cameraPreview.changeCameraFormat)
        formLayout.addRow("Camera Format", self.cameraFormatBox)

        # Populate the video input box only if there are available cameras
        if len(self.availableCameras) != 0:
            self.videoInputBox.addItems(camera.description() for camera in self.availableCameras)

        self.port = QtWidgets.QSpinBox(minimum=1025, maximum=65535, value=1337)
        formLayout.addRow("Port", self.port)

        # Check box to record all parameters
        self.allParams = QtWidgets.QCheckBox()
        self.allParams.setChecked(True)
        self.allParams.checkStateChanged.connect(self.disableEnableParameters)
        formLayout.addRow("Record All", self.allParams)

        # Dictionary of all paramaters and their checkboxes so user can choose parameters
        self.paramDict = dict()
        for param in Utility.ForzaSettings.params:
            checkBox = QtWidgets.QCheckBox()
            checkBox.setChecked(True)
            self.paramDict[param] = checkBox

        # Add a checkbox for each parameter
        for param, checkBox in self.paramDict.items():
            formLayout.addRow(param, checkBox)

        self.setWindowTitle("Configure Record Settings")

        # Define the buttons at the bottom and connect them to the dialog
        buttons = (QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        self.buttonBox = (QtWidgets.QDialogButtonBox(buttons))
        self.buttonBox.accepted.connect(self.accept)
        self.buttonBox.accepted.connect(self.collectAndSave)
        self.buttonBox.rejected.connect(self.reject)

        layout.addWidget(scrollArea)

        layout.addWidget(self.buttonBox)
        self.setLayout(layout)
    
    def collectAndSave(self):
        """Collect all the chosen settings and emit a save signal"""

        port = self.port.value()
        allParams = self.allParams.isChecked()
        selectedParams = dict()
        if not allParams:
            for param, checkBox in self.paramDict.items():
                selectedParams[param] = checkBox.isChecked()
        recordVideo = self.recordVideo.isChecked()

        cameraDevice = None
        cameraFormat = None
        if recordVideo:  # don't bother changing the video/format settings if the user isn't going to record them
            for cd in QMediaDevices.videoInputs():
                if self.videoInputBox.currentText() == cd.description():
                    cameraDevice = cd
            if cameraDevice is not None:
                cameraFormat = cameraDevice.videoFormats()[self.cameraFormatBox.currentIndex()]
        
        self.save.emit(port, allParams, selectedParams, recordVideo, cameraDevice, cameraFormat)
    
    def updateCameraFormatBox(self, cameraDescription: str):
        """Updates the video format box given the description of the camera device"""

        self.cameraFormatBox.clear()

        for camera in QMediaDevices.videoInputs():
            if cameraDescription == camera.description():
                self.cameraFormatBox.addItems(Utility.QCameraFormatToStr(format) for format in camera.videoFormats())
                self.cameraFormatBox.setCurrentIndex(0)
    
    def disableEnableVideo(self, checked: Qt.CheckState):
        """Enable or disable the video and video format input boxes"""

        if checked is Qt.CheckState.Checked:
            self.videoInputBox.setEnabled(True)
            self.cameraFormatBox.setEnabled(True)
        else:
            self.videoInputBox.setEnabled(False)
            self.cameraFormatBox.setEnabled(False)
    
    @pyqtSlot(Qt.CheckState)
    def disableEnableParameters(self, checked: Qt.CheckState):
        """Enable or disable the checkboxes for individual parameters"""
        
        for param, checkBox in self.paramDict.items():
            if checked is Qt.CheckState.Checked:
                checkBox.setEnabled(True)
            else:
                checkBox.setEnabled(False)


class MainWindow(QtWidgets.QMainWindow):

    # Stores previously recorded telemetry data
    session = Session()

    # Stores all settings needed to record telemetry and video
    record = Record()

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

        # Record status widget
        recordStatusWidget = RecordStatusWidget(self.record.port, self.record.ip)
        recordStatusDockWidget = QtWidgets.QDockWidget("Record Status", self)
        recordStatusDockWidget.setAllowedAreas(Qt.DockWidgetArea.TopDockWidgetArea | Qt.DockWidgetArea.BottomDockWidgetArea)
        recordStatusDockWidget.setWidget(recordStatusWidget)
        recordStatusDockWidget.setStatusTip("Record Status: Displays the main settings and status of the recording.")
        self.addDockWidget(Qt.DockWidgetArea.TopDockWidgetArea, recordStatusDockWidget)
        self.record.statusUpdate.connect(recordStatusWidget.update)

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
        viewMenu.addAction(recordStatusDockWidget.toggleViewAction())
    
    @pyqtSlot()
    def configureRecord(self):
        """Opens a RecordDialog dialog box to update the record settings (port, camera etc) and save
        them to the Record object"""

        dlg = RecordDialog(parent=self)
        dlg.save.connect(self.record.update)
        if dlg.exec():  # If the user presses OK to save their settings
            """ MOVED TO DIALOG ITSELF
            port = dlg.port.value()
            allParams = dlg.allParams.isChecked()
            selectedParams = dict()
            if not allParams:
                for param, checkBox in dlg.paramDict.items():
                    selectedParams[param] = checkBox.isChecked()
            recordVideo = dlg.recordVideo.isChecked()

            cameraDevice = None
            cameraFormat = None
            if recordVideo:  # don't bother changing the video/format settings if the user isn't going to record them
                for cd in QMediaDevices.videoInputs():
                    if dlg.videoInputBox.currentText() == cd.description():
                        cameraDevice = cd
                cameraFormat = cameraDevice.videoFormats()[dlg.cameraFormatBox.currentIndex()]
            """
            logging.info("Record settings changed successfully")
        else:
            logging.info("Record settings could not be changed")

    @pyqtSlot()
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
            
            self.videoPlayer.player.setSource(QUrl.fromLocalFile(str(videoFilePath)))
            logging.info("Loaded video file")

    @pyqtSlot()
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
