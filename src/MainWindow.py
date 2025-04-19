from PySide6 import QtWidgets, QtMultimediaWidgets, QtMultimedia
from PySide6.QtCore import Slot, QThread, QObject, Signal, Qt, QSize
from PySide6.QtGui import QAction, QIcon, QKeySequence

from fdp import ForzaDataPacket

import pathlib
import yaml
import logging
import select
import socket
from enum import Enum

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

"""
Introduced a session object to easily pass new data between to tabs without them doing their own calculations on it.
Eg. this main window can pass session to dashboard, and to analytics separately so their only jobs are to display
the data.

Session will get a fdp and alter its own state to reflect the current state of the game.

Its state will comprise of the original fdp values, plus its own calculated values like fuel and interval.
It will then update the updated values dictionary and emit that to widgets to display.
"""

class Session(QObject):
    """
    Maintains the state of a session so individual widgets are not responsible for keeping it, just displaying
    the data.
    
    A session represents a single unit of time's worth of continuously logged packets.

    Each tab will rely on this object to provide them with up to date values from packets. When updated, it will
    emit a signal to tell widgets to change their values.
    """

    # Emitted when the session object is updated so widgets can display the latest values.
    # Contains a dictionary of only the values that were updated. If a value stays the same between packets,
    # it will not be present.
    updated = Signal(dict)

    def __init__(self):
        super().__init__()

    @Slot()
    def update(self, fdp: ForzaDataPacket):
        """
        Updates the state of the session using a new data packet.
        """
        updatedValues = dict()

        # Update the session state and put any updated values in the dictionary here
        
        self.updated.emit(updatedValues)


class MyToolBar(QtWidgets.QToolBar):
    """
    The toolbar at the top of the application, holding the icons that perform functions like play, or change view
    """

    def __init__(self):
        super().__init__()


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


class MainWindow(QtWidgets.QMainWindow):

    # Maintains the state of a single session. Users can save this session to a file, or reset it.
    session = Session()


    def __init__(self):
        super().__init__()

        parentDir = pathlib.Path(__file__).parent.parent.resolve()
        videoPath = parentDir / pathlib.Path("media") / pathlib.Path("example-forza-video.mp4")

        self.worker = None
        self.thread = None

        self.dashConfig = dict()
        self.ip = ""

        self.videoPlayer = VideoPlayer()
        self.setCentralWidget(self.videoPlayer)

        # Hard codes a video file to TEST the video player
        self.videoPlayer.player.setSource(str(videoPath))

        # Add the Toolbar and Actions --------------------------

        toolbar = MyToolBar()
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

        # Add the menu bar and connect actions
        menu = self.menuBar()
        actionsMenu = menu.addMenu("&Actions")
        actionsMenu.addAction(playPauseAction)
        actionsMenu.addAction(stopAction)
    
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
