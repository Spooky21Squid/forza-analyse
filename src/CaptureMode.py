from PyQt6 import QtWidgets, QtMultimedia
from PyQt6.QtCore import pyqtSlot, QThread, QObject, pyqtSignal, Qt, QSize, QUrl, QAbstractTableModel, QAbstractListModel, QItemSelection, QModelIndex, QRunnable, QThreadPool, QTimer
from PyQt6.QtGui import QAction, QIcon, QKeySequence, QColor, QStandardItemModel, QStandardItem, QPixmap, QPen, QCloseEvent, QGuiApplication
from PyQt6.QtMultimedia import QMediaDevices, QCamera, QMediaCaptureSession, QCameraDevice, QCameraFormat, QWindowCapture, QCapturableWindow, QScreenCapture, QMediaRecorder
from PyQt6.QtMultimediaWidgets import QVideoWidget

import pyqtgraph as pg
import numpy as np
import pandas as pd

import distinctipy
from fdp import ForzaDataPacket

from Utility import ForzaSettings
import csv
import datetime
from time import sleep
import pathlib
import yaml
import logging
import select
import socket
import random
from enum import Enum, auto
from collections import OrderedDict
from abc import ABC, abstractmethod
from typing import Literal

"""
- implement a CaptureModeSessionManager that:
    - gets packets from the telemetry manager
    - works out which lap its on (using an internal lap counter), and what track and car
    - sends out a signal with the new data
    - adds it to an internal pandas data frame-backed model

- It should also be able to:
    - save whatever it has to a file
    - get all packets related to a lap

"""


class UDPWorker(QRunnable):
    """Listens to a single UDP socket and emits the bytes collected from a UDP packet through the 'collected' signal"""

    class Signals(QObject):
        """Signals for the UDPWorker"""
        finished = pyqtSignal()
        collected = pyqtSignal(bytes)

    def __init__(self, port:int):
        super().__init__()
        self.signals = UDPWorker.Signals()
        self.working = True
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(0)  # Set to non blocking, so the thread can be terminated without the socket blocking forever
        self.socketTimeout = 1
        self.port = port

    def run(self):
        """Binds the socket and starts listening for packets"""
        try:
            self.sock.bind(('', self.port))
        except:
            logging.info("Socket could not be opened.")
            self.signals.finished.emit()
        logging.info("Started listening on port {}".format(self.port))

        while self.working:
            try:
                ready = select.select([self.sock], [], [], self.socketTimeout)
                if ready[0]:
                    data, address = self.sock.recvfrom(1024)
                    logging.debug('received {} bytes from {}'.format(len(data), address))
                    self.signals.collected.emit(data)
                else:
                    logging.debug("Socket timeout")
            except BlockingIOError:
                logging.info("Could not listen to {}, trying again...".format(address))
        
        # Close the socket after the player wants to stop listening, so that
        # a new socket can be created using the same port next time
        self.sock.close()
        logging.info("Socket closed.")
        self.signals.finished.emit()
    
    def finish(self):
        """Signals to the worker to stop and close the socket. The thread is not properly finished until the
        'finished' signal is emitted"""
        self.working = False
    
    def changePort(self, port:int):
        """Changes the port that the worker will listen to. If already running, this will do nothing until the worker is
        started again."""
        self.port = port


class FootageCapture(QObject):
    """
    A class to capture and record footage from either camera, screen or window. Basically a wrapper around
    QMediaCaptureSession with some defaults.
    
    Default is to record the main screen to the user's home directory.
    """

    class Error(Enum):
        """The possible errors that can be raised by a FootageCapture object"""
    
        CaptureFailed = "Footage capture has failed"
        RecordFailed = "Footage recording has failed"


    class Signals(QObject):
        errorOccurred = pyqtSignal(object, str)  # Emits a FootageCapture.Error and descriptive string
        activeChanged = pyqtSignal(bool)
    

    class SourceType(Enum):
        """The footage's source type"""
        Screen = auto()
        Window = auto()
        Camera = auto()
    

    def __init__(self, parent = None, footageDirectory: str = None):
        super().__init__(parent)

        self._mediaCaptureSession = QMediaCaptureSession()
        self._sourceType = self.SourceType.Screen
        self._active: bool = False  # If footage is currently being captured
        self.signals = self.Signals()

        # Default to main screen capture
        self._screenCapture = QScreenCapture()
        self._screenCapture.errorOccurred.connect(self._onFootageCaptureError)
        self._screenCapture.setScreen(QGuiApplication.primaryScreen())
        self._mediaCaptureSession.setScreenCapture(self._screenCapture)
        self._screenCapture.start()

        # Add option to capture a single window
        self._windowCapture = QWindowCapture()
        self._windowCapture.errorOccurred.connect(self._onFootageCaptureError)
        self._mediaCaptureSession.setWindowCapture(self._windowCapture)

        # Add option to record a camera, eg. an elgato
        self._camera = QCamera()
        self._camera.errorOccurred.connect(self._onFootageCaptureError)
        self._mediaCaptureSession.setCamera(self._camera)

        # Add the recorder to save the media capture session to a file
        self._recorder = QMediaRecorder()
        self._outputDirectory = pathlib.Path.home().resolve()  # Set output to user's home directory as default
        if footageDirectory is not None and footageDirectory != "default":
            self._outputDirectory = pathlib.Path(footageDirectory).resolve()
        self._recorder.errorOccurred.connect(self._onFootageRecordError)
        self._mediaCaptureSession.setRecorder(self._recorder)
    
    def _onFootageCaptureError(self, error, errorString: str):
        """Called when one of the sources for footage capture encounters an error"""

        if isinstance(error, QScreenCapture.Error) and self._sourceType is self.SourceType.Screen:
            self.signals.errorOccurred.emit(self.Error.CaptureFailed, errorString)
        
        if isinstance(error, QWindowCapture.Error) and self._sourceType is self.SourceType.Window:
            self.signals.errorOccurred.emit(self.Error.CaptureFailed, errorString)
        
        if isinstance(error, QCamera.Error) and self._sourceType is self.SourceType.Camera:
            self.signals.errorOccurred.emit(self.Error.CaptureFailed, errorString)

    def _onFootageRecordError(self, error, errorString: str):
        """Called when the recorder of the footage encounters an error"""
        self.signals.errorOccurred.emit(self.Error.RecordFailed, errorString)

    def isActive(self) -> bool:
        """Returns True if footage is being captured and recorded"""
        return self._active

    def _setActive(self, active: bool):
        """Sets the active attribute and emits the active changed signal"""
        if not self._active == active:
            self._active = active
            self.signals.activeChanged.emit(active)

    def start(self, trackOrdinal: int, dt: datetime.datetime):
        """
        Starts capturing and recording footage.

        Parameters
        ----------
        trackOrdinal : The unique ID of a track. Determines the directory that a footage file should be saved to (eg. the player is
        recording Brands Hatch with track ordinal 860, so the footage is saved under the /860/ directory)
        dt : The date and time of the start of the recording. Determines the name of the footage file
        """
        extension = ".mp4"
        prefix = "Forza-Session_"
        filename = prefix + dt.strftime("%Y-%m-%d_%H-%M-%S") + extension
        trackDirectory = self._outputDirectory / pathlib.Path(str(trackOrdinal))
        trackDirectory.mkdir(parents=True, exist_ok=True)  # Ensure the track directory is created
        outputFile = trackDirectory / pathlib.Path(filename)
        outputFile.resolve()
        self._recorder.setOutputLocation(QUrl.fromLocalFile(str(outputFile)))
        self._setActive(True)
        self._recorder.record()
        logging.info("FootageCapture started recording")
    
    def stop(self):
        """Stops recording footage"""
        self._setActive(False)
        self._recorder.stop()
        logging.info("FootageCapture stopped recording")

    def getSourceType(self):
        """Returns the current source type"""
        return self._sourceType

    def setSourceType(self, sourceType):
        """Change the source of the footage between the current screen, window or camera"""

        self.footageSourceType = sourceType
        self._screenCapture.setActive(sourceType == self.SourceType.Screen)
        self._windowCapture.setActive(sourceType == self.SourceType.Window)
        self._camera.setActive(sourceType == self.SourceType.Camera)

    def getScreenCapture(self):
        """Returns the current QScreenCapture object"""
        return self._screenCapture
    
    def setScreenCapture(self, screenCapture: QScreenCapture):
        """Sets the screen capture object"""
        self._screenCapture.errorOccurred.disconnect(self._onFootageCaptureError)
        screenCapture.errorOccurred.connect(self._onFootageCaptureError)
        self._screenCapture = screenCapture
    
    def getWindowCapture(self):
        """Returns the current QWindowCapture object"""
        return self._windowCapture
    
    def setWindowCapture(self, windowCapture: QWindowCapture):
        """Sets the Window capture object"""
        self._windowCapture.errorOccurred.disconnect(self._onFootageCaptureError)
        windowCapture.errorOccurred.connect(self._onFootageCaptureError)
        self._windowCapture = windowCapture
    
    def getCamera(self):
        """Returns the current QCamera object"""
        return self._camera
    
    def setCamera(self, camera: QCamera):
        """Sets the camera object"""
        self._camera.errorOccurred.disconnect(self._onFootageCaptureError)
        camera.errorOccurred.connect(self._onFootageCaptureError)
        self._camera = camera

    def getRecorder(self):
        """Returns the QMediaRecorder"""
        return self._recorder
    
    def setRecorder(self, recorder: QMediaRecorder):
        """Sets the recorder. If changed while recording, an errorOccurred signal will be emitted and recording will stop"""
        if self._active:
            self.stop()
            self.signals.errorOccurred.emit(self.Error.RecordFailed, "Recorder was changed during recording")
        
        self._recorder.errorOccurred.disconnect(self._onFootageRecordError)
        recorder.errorOccurred.connect(self._onFootageRecordError)
        self._recorder = recorder
    
    def setVideoPreview(self, videoOutput: QObject):
        """Sets a new video widget as the output for the media session"""
        self._mediaCaptureSession.setVideoOutput(videoOutput)


class TelemetryPersistence(QObject):
    """An abstract base class for saving telemetry packets received on demand."""


    class Error(Enum):
        """The possible errors that can be raised by a TelemetryPersistence object"""
        PersistenceFailed = "Telemetry persistence has failed and has stopped"
        PacketNotSaved = "Could not save packet"
        AttributeNotSet = "Attribute cannot be set while persistence is active"

    
    class Signals(QObject):
        errorOccurred = pyqtSignal(object)  # Emits with a TelemetryPersistence.Error object
        activeChanged = pyqtSignal(bool)

        # Emitted after the first packet was accepted to be saved. Signal is emitted with a track ordinal, a car ordinal,
        # and the date and time that the first saved packet was collected
        firstPacketSaved = pyqtSignal(int, int, datetime.datetime)


    def __init__(self, parent = None):
        super().__init__(parent)
        self._active = False  # Whether object is currently performing any persistence
        self.signals = self.Signals()
    
    @abstractmethod
    def start(self):
        """Starts saving telemetry"""
        ...
    
    @abstractmethod
    def stop(self):
        """Stops saving telemetry"""
        ...
    
    @abstractmethod
    def savePacket(self, fdp: ForzaDataPacket):
        """Saves a single Forza Data Packet"""
        ...
    
    @abstractmethod
    def ready(self):
        """Returns True if object is ready to save telemetry"""
        ...

    def isActive(self) -> bool:
        """Returns True if the object is active"""
        return self._active

    def _setActive(self, active: bool):
        """Sets the active attribute and emits the active changed signal"""
        if not self._active == active:
            self._active = active
            self.signals.activeChanged.emit(active)


class TelemetryDSVFilePersistence(TelemetryPersistence):
    """
    Saves Forza Data Packets to Delimiter Separated Files. This class takes a path to a directory (like the user's home
    directory) and saves telemetry to new files using the time of the start of the session to name the file, and the track
    ordinal to choose the specific track directory.

    Telemetry will be saved in the same file as long as the player stays on one track. If a new packet arrives with a different
    track ID, the previous file will be closed and a new file will be opened using a new start time as the file name.

    Packets collected while the race is not on will be ignored.
    """

    class Delimiter(Enum):
        """Types of delimiter to be used to separate values in the same row"""
        Comma = ","
        Tab = "\t"

    def __init__(self, delimiter: Delimiter = Delimiter.Comma, parent=None):
        """
        Constructs a new object to save telemetry to delimiter separated files.
        
        Parameters
        ----------
        delimiter : The type of delimiter that will separate fields in a packet
        parent : The parent widget
        """

        super().__init__(parent)
        self._file = None  # The file object used to write telemetry
        self._delimiter: TelemetryDSVFilePersistence.Delimiter = delimiter  # To use commas or tabs to separate the entries
        self._path: pathlib.Path | None = None  # Path to the parent directory (above specific track directories) to save the file in
        self._csvWriter: csv.DictWriter | None = None  # The CSV Writer object if comma is the chosen delimiter
        self._onlySaveRaceOn: bool = True  # If True, only packets received while the race is on will be saved
        self._firstPacketReceived = False
        self._currentTrackOrdinal: int | None = None
        self._currentCarOrdinal: int | None = None
    
    def setOnlySaveRaceOn(self, value: bool):
        """If value is True, only packets received while the race is on will be saved. If False, all packets (including
        when the race is not on) will be saved. If this is changed while the object is active, it will affect packets
        received after the attribute was changed."""
        self._onlySaveRaceOn = value
    
    def getOnlySaveRaceOn(self) -> bool:
        """Returns whether the object is only saving packets during a race"""
        return self._onlySaveRaceOn
    
    def setPath(self, path: pathlib.Path | str):
        """Sets the path to the parent directory that telemetry files should be saved to. Files will be saved under their specific
        track directory under this parent directory. Emits errorOccurred with an AttributeNotSet signal if it cannot be set
        (eg. while the persistence is active)"""

        if isinstance(path, str):
            path = pathlib.Path(path).resolve()
        
        if not path.is_dir():
            self.signals.errorOccurred.emit(self.Error.AttributeNotSet)

        self._path = path

    def getPath(self) -> pathlib.Path | None:
        """Returns the current path, or None if it has not been set."""
        return self._path
    
    def setDelimiter(self, delimiter: Delimiter):
        """Sets the delimiter. Emits the errorOccurred signal with AttributeNotSet if the object is active and the delimiter can't be changed."""
        if self._active:
            self.signals.errorOccurred.emit(self.Error.AttributeNotSet)
        else:
            self._delimiter = delimiter
    
    def getDelimiter(self) -> Delimiter:
        """Returns the type of delimiter being used"""
        return self._delimiter

    def _to_str(value):
        """
        Returns a string representation of the given value, if it's a floating
        number, format it.

        :param value: the value to format
        """
        if isinstance(value, float):
            return('{:f}'.format(value))

        return('{}'.format(value))

    def start(self):
        """Starts saving telemetry. Once the first packet has been received, a new file will be opened
        in it's track's specific directory and packets will be written"""
        
        if self._path is None:
            self.signals.errorOccurred.emit(self.Error.PersistenceFailed)
            return
        
        if self._active:
            return

        self._firstPacketReceived = False
        self._setActive(True)
        logging.info("TelemetryPersistence started")

    def stop(self):
        """Stops saving telemetry and closes the file"""
        
        if not self._active:
            return
        
        self._setActive(False)
        if self._file is not None:
            self._file.close()
        logging.info("TelemetryPersistence stopped")
    
    def savePacket(self, fdp: ForzaDataPacket):
        """Receives a single Forza Data Packet and decides how or if it should be saved, and saves it."""
        
        if not self._active:
            return
        
        # Discard packets that are received when the race is not on if they are not being saved
        if not fdp.is_race_on and self._onlySaveRaceOn:
            return

        # If the car ordinal or track ordinal is different, player has started a new session, so stop saving packets and restart
        if self._firstPacketReceived and (fdp.track_ordinal != self._currentTrackOrdinal or fdp.car_ordinal != self._currentCarOrdinal):
            logging.info("TelemetryPersistence: Car/Track change detected")
            self.stop()
            self.start()
            return
        
        # If this is the first packet received, set up the file
        if not self._firstPacketReceived:

            # build the file path and name
            dt = datetime.datetime.now()
            filename = dt.strftime("%Y-%m-%d_%H-%M-%S")
            if self._delimiter is TelemetryDSVFilePersistence.Delimiter.Comma:
                filename += ".csv"
            elif self._delimiter is TelemetryDSVFilePersistence.Delimiter.Tab:
                filename += ".tsv"
            filename = "Forza-Session_" + filename
            trackOrdinal = str(fdp.track_ordinal)
            directory = self._path / pathlib.Path(trackOrdinal)
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / pathlib.Path(filename)

            # Prepare the file
            try:
                self._file = open(str(path), "w")
                # Add the header row
                params = ForzaSettings.paramsList
                if self._delimiter is TelemetryDSVFilePersistence.Delimiter.Comma:
                    self._csvWriter = csv.writer(self._file, lineterminator = "\r")
                    self._csvWriter.writerow(params)
                else:
                    self._file.write('\t'.join(params))
                    self._file.write('\n')
            except OSError as e:
                if self._file != None:
                    self._file.close()
                self.signals.errorOccurred.emit(self.Error.PersistenceFailed)
                return
            self._firstPacketReceived = True  # Set to true so file isn't set up next time

            # Emit the firstPacketSaved signal
            self.signals.firstPacketSaved.emit(fdp.track_ordinal, fdp.car_ordinal, dt)

            # Set the current track and car ordinals
            self._currentTrackOrdinal = fdp.track_ordinal
            self._currentCarOrdinal = fdp.car_ordinal
            logging.info("TelemetryPersistence: First packet saved")

        # Now can save the packet

        params = ForzaSettings.paramsList

        try:
            if self._delimiter is self.Delimiter.Comma:
                self._csvWriter.writerow(fdp.to_list(params))
            else:
                self._file.write('\t'.join([self._to_str(v) for v in fdp.to_list(params)]))
                self._file.write('\n')
        except:
            self.signals.errorOccurred.emit(self.Error.PacketNotSaved)

    def ready(self):
        if self._path is not None and not self._active:
            return True
        else:
            return False


class TelemetryCapture(QObject):
    """Captures Forza telemetry packets. This class manages a UDPWorker and tries to transform any collected packets into
    ForzaDataPacket objects. When a valid packet is collected a signal is emitted containing that ForzaDataPacket object
    regardless of the race status. This class also maintains a status about the packets' validity, and a status about
    the underlying socket."""

    class Status(Enum):
        NotListening = auto()  # No socket set up
        Listening = auto()  # Socket and port set up, listening for packets but no Forza packets are being detected
        Capturing = auto()  # Forza packets detected and being captured
    

    class Error(Enum):
        """The possible errors that can be raised by a TelemetryCapture object"""
        
        PortChangeError = "Port cannot be changed while telemetry is being captured"
        CaptureFailed = "Telemetry capture has failed"
        BadPacketReceived = "Packet could not be processed"

    
    class Signals(QObject):
        collected = pyqtSignal(ForzaDataPacket)  # Emitted on collection of a forza data packet
        errorOccurred = pyqtSignal(object)  # Emits a TelemetryCapture.Error object
        activeChanged = pyqtSignal(bool)
        statusChanged = pyqtSignal(object)
        portChanged = pyqtSignal(int)


    def __init__(self, parent = None, port = None):
        super().__init__(parent)
        self.signals = TelemetryCapture.Signals()
        self._active = False  # Whether telemetry is currently being recorded
        self._status: TelemetryCapture.Status = self.Status.NotListening
        self._port: int = port  # The port that listens for incoming Forza data packets
        self._packetsCollected = 0
        self._invalidPacketsCollected = 0
        self._threadpool = QThreadPool(self)
        self._worker: UDPWorker = None
        self._startTime: datetime.datetime = None  # The date and time that the object started recording
        self._endTime: datetime.datetime = None  # The date and time that the object stopped recording

    def start(self):
        """Start capturing telemetry"""
        
        if self._port is None or self._active:
            self.signals.errorOccurred.emit(self.Error.CaptureFailed)
            return

        self._setStatus(self.Status.Listening)

        self._packetsCollected = 0
        self._invalidPacketsCollected = 0
        
        self._worker = UDPWorker(self._port)
        self._worker.setAutoDelete(True)
        self._worker.signals.collected.connect(self._onCollected)
        self._worker.signals.finished.connect(self._onFinished)
        self._threadpool.start(self._worker)
        self._setActive(True)
    
    def _setActive(self, active: bool):
        """Sets the active attribute"""
        if active:
            self._startTime = datetime.datetime.now()
            self._endTime = None
        else:
            self._endTime = datetime.datetime.now()

        self._active = active
        self.signals.activeChanged.emit(active)
        
    def _onCollected(self, data: bytes):
        """Called when a single UDP packet is collected. Receives the unprocessed
        packet data, transforms it into a Forza Data Packet and emits the collected signal with
        that forza data packet object. If packet cannot be read, it will emit an error signal"""

        fdp: ForzaDataPacket = None
        try:
            fdp = ForzaDataPacket(data)
            self._setStatus(self.Status.Capturing)
            self._packetsCollected += 1
            self.signals.collected.emit(fdp)
        except:
            # If it's not a forza packet
            self._setStatus(self.Status.Listening)
            self.signals.errorOccurred.emit(self.Error.BadPacketReceived)
            self._invalidPacketsCollected += 1

        if self._packetsCollected % 60 == 0:
            logging.debug(f"Received {self._packetsCollected} packets.")
        if self._invalidPacketsCollected % 60 == 0:
            logging.debug(f"Received {self._invalidPacketsCollected} invalid packets.")

    def _onFinished(self):
        """Cleans up after the worker has stopped listening to packets"""
        self._setActive(False)
        self._setStatus(self.Status.NotListening)

    def stop(self):
        """Stops recording telemetry"""
        if self._active and self._worker is not None:
            self._worker.finish()
    
    def setPort(self, port: int):
        """Sets the port to listen to. If the object is currently active and listening for packets, listening will be temporarily stopped
        while the port is changed, and resumed using the new port."""
        if self._active:
            self.stop()
            self._port = port
            self.signals.portChanged.emit(port)
            self.start()
            #self.signals.errorOccurred.emit(self.Error.PortChangeError)
        else:
            self._port = port
            self.signals.portChanged.emit(port)
    
    def getPort(self) -> int | None:
        """Returns the current port. Returns None if the port hasn't been set yet."""
        return self._port
    
    def isActive(self) -> bool:
        """Returns whether this object is currently capturing telemetry packets"""
        return self._active

    def getPacketsCollected(self) -> int:
        """Returns the number of valid packets collected during the last capture session"""
        return self._packetsCollected
    
    def getInvalidPacketsCollected(self) -> int:
        """Returns the number of invalid packets collected during the last capture session"""
        return self._invalidPacketsCollected

    def getStartTime(self) -> datetime.datetime | None:
        """Returns the start time of the last capture as a datetime object. Returns None if no capture session has started."""
        return self._startTime
    
    def getEndTime(self) -> datetime.datetime | None:
        """Returns the end time of the last capture as a datetime object. Returns None if no capture session has ended."""
        return self._endTime

    def ready(self) -> bool:
        """Returns True if the object is configured and ready to start recording. Returns False if it is not ready, or
        is already capturing."""
        if self._port is not None and not self._active:
            return True
        else:
            return False
    
    def _setStatus(self, status):
        """Sets the status of the object and emits a status changed signal"""
        if self._status is not status:
            self._status = status
            self.signals.statusChanged.emit(status)
    
    def getStatus(self):
        """Returns the current status of the telemetry capture object"""
        return self._status


class TelemetryManager(QObject):
    """Manages and coordinates the capture and persistence of Forza telemetry"""


    class Signals(QObject):
        """All the signals produced by the TelemetryManager class"""

        # Telemetry is being captured AND has started recording to a file. Signal is emitted with a track ordinal
        # and the date and time that the first saved packet was collected, so a footage recorder knows which track directory
        # to save footage to and the datetime to use to name the footage file
        startedRecording = pyqtSignal(int, datetime.datetime)

        # Telemetry has stopped being recorded to a file. Telemetry may still be captured from a socket
        stoppedRecording = pyqtSignal()

    
    def __init__(self, parent = None):
        super().__init__(parent)
        self.signals = self.Signals()
        self._telemetryCapture: TelemetryCapture | None = None
        self._telemetryPersistence: TelemetryPersistence | None = None
    
    def getTelemetryCapture(self) -> TelemetryCapture | None:
        """Return the current TelemetryManager object, or None if not set"""
        return self._telemetryCapture
    
    def setTelemetryCapture(self, telemetryCapture: TelemetryCapture):
        """Adds a new TelemetryCapture object to the TelemetryManager"""
        # Unlink the old telemetry capture object's signals and slots and link the new
        if self._telemetryPersistence is not None:
            self._telemetryCapture.signals.collected.disconnect(self._telemetryPersistence.savePacket)
            telemetryCapture.signals.collected.connect(self._telemetryPersistence.savePacket)

        # Add the new capture object
        self._telemetryCapture = telemetryCapture
    
    def getTelemetryPersistence(self) -> TelemetryPersistence | None:
        """Return the current TelemetryPersistence object, or None if not set"""
        return self._telemetryPersistence

    def setTelemetryPersistence(self, telemetryPersistence: TelemetryPersistence):
        """Adds a new TelemetryPersistence object to the TelemetryManager"""
        # Unlink the old telemetry persistence object's signals and slots and link the new
        if self._telemetryPersistence is not None:
            if self._telemetryCapture is not None:
                self._telemetryCapture.signals.collected.disconnect(self._telemetryPersistence.savePacket)
            self._telemetryPersistence.signals.activeChanged.disconnect(self._onTelemetryPersistenceActiveChanged)
            self._telemetryPersistence.signals.firstPacketSaved.disconnect(self._onTelemetryPersistenceFirstPacket)

        if self._telemetryCapture is not None:
            self._telemetryCapture.signals.collected.connect(telemetryPersistence.savePacket)

        telemetryPersistence.signals.activeChanged.connect(self._onTelemetryPersistenceActiveChanged)
        telemetryPersistence.signals.firstPacketSaved.connect(self._onTelemetryPersistenceFirstPacket)

        # Add the new capture object
        self._telemetryPersistence = telemetryPersistence
    
    def _onTelemetryCaptureActiveChanged(self, value: bool):
        if not value:
            if self._telemetryPersistence is not None:
                self._telemetryPersistence.stop()
    
    def _onTelemetryPersistenceActiveChanged(self, value: bool):
        if not value:
            self.signals.stoppedRecording.emit()
    
    def _onTelemetryPersistenceFirstPacket(self, trackOrdinal: int, carOrdinal: int, dt: datetime.datetime):
        self.signals.startedRecording.emit(trackOrdinal, dt)

    def startSaving(self):
        """Tells the telemetry persistence object to start saving packets if one exists"""
        logging.info("TelemetryManager started saving")
        # Make sure telemetry is being captured before trying to save
        if self._telemetryCapture is not None:
            self._telemetryCapture.start()
        if self._telemetryPersistence is not None:
            self._telemetryPersistence.start()
    
    def stopSaving(self):
        """Tells the telemetry persistence object to stop saving packets if one exists"""
        logging.info("TelemetryManager stopped saving")
        if self._telemetryPersistence is not None:
            self._telemetryPersistence.stop()


class CaptureManager(QObject):
    """Manages and coordinates the capture of Forza telemetry and footage"""

    def __init__(self, parent = None):
        super().__init__(parent)
        self._telemetryManager: TelemetryManager | None = None
        self._footageCapture: FootageCapture | None = None
        self._started: bool = False
    
    def getFootageCapture(self) -> FootageCapture | None:
        """Returns the current FootageCapture object, or none if it isn't set"""
        return self._footageCapture
    
    def setFootageCapture(self, footageCapture: FootageCapture):
        """Adds a new FootageCapture object to the CaptureManager. This does not stop any recording of telemetry or
        footage, so make sure these are stopped or able to be stopped before setting a new TelemetryManager"""

        # Unlink the old footage capture
        if self._telemetryManager is not None and self._footageCapture is not None:
            self._telemetryManager.signals.startedRecording.disconnect(self._footageCapture.start)
            self._telemetryManager.signals.stoppedRecording.disconnect(self._footageCapture.stop)

        # Link the new
        if self._telemetryManager is not None:
            self._telemetryManager.signals.startedRecording.connect(footageCapture.start)
            self._telemetryManager.signals.stoppedRecording.connect(footageCapture.stop)

        # Add the new manager
        self._footageCapture = footageCapture

    def setTelemetryManager(self, telemetryManager: TelemetryManager):
        """Adds a new TelemetryManager object to the CaptureManager. This does not stop any recording of telemetry or
        footage, so make sure these are stopped or able to be stopped before setting a new TelemetryManager"""

        # Unlink the old telemetry manager
        if self._telemetryManager is not None and self._footageCapture is not None:
            telemetryManager.signals.startedRecording.disconnect(self._footageCapture.start)
            telemetryManager.signals.stoppedRecording.disconnect(self._footageCapture.stop)

        # Link the new manager
        if self._footageCapture is not None:
            telemetryManager.signals.startedRecording.connect(self._footageCapture.start)
            telemetryManager.signals.stoppedRecording.connect(self._footageCapture.stop)

        # Add the new manager
        self._telemetryManager = telemetryManager
    
    def getTelemetryManager(self) -> TelemetryManager | None:
        """Returns the current TelemetryManager object. Returns None if no manager set"""
        return self._telemetryManager

    def startSaving(self):
        """Tells the CaptureManager to start saving telemetry packets and recording footage"""
        logging.info("CaptureManager started saving")
        if self._telemetryManager is not None:
            self._telemetryManager.startSaving()
    
    def stopSaving(self):
        """Tells the CaptureManager to stop saving telemetry and recording footage"""
        logging.info("CaptureManager stopped saving")
        if self._telemetryManager is not None:
            self._telemetryManager.stopSaving()
        
        if self._footageCapture is not None:
            self._footageCapture.stop()

    def toggleCapture(self):
        """Toggles between saving and not saving"""
        if self._started:
            self.stopSaving()
        else:
            self.startSaving()
        self._started = not self._started


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


class LaptimeModel(QAbstractTableModel):
    """A Table Model of the laps currently completed or in progress in the current session"""

    def __init__(self, parent = None):
        super().__init__(parent)

        # Data is a list of lap times in ms, indexed by their lap number.
        # Lap number does not match the number in Forza, but rather the total number of laps done in a single session, counted
        # over multiple restarts.
        self.lapTimes = []

        # The names of all columns, used for headerData
        self.columnLabels = ["Lap", "Time"]
    
    def data(self, index, role):
        if role == Qt.ItemDataRole.DisplayRole:
            if len(self.lapTimes) == 0:
                return None
            
            if index.column() == 0:
                return index.row()
            else:
                return str(self.lapTimes[index.row()])
    
    def rowCount(self, index):
        return len(self.lapTimes)

    def columnCount(self, index):
        return 2  # Lap number and Lap time

    def headerData(self, section, orientation, role):
        # section is the index of the column/row.
        if role == Qt.ItemDataRole.DisplayRole:
            if orientation == Qt.Orientation.Horizontal:
                return self.columnLabels[section]

            #if orientation == Qt.Orientation.Vertical:
                #return str(section)
    
    def addNewLap(self):
        """Adds a new lap to the model that will become the current lap. Lap number will follow from the last lap
        and lap time will initially be set to 0"""
        self.beginResetModel()
        self.lapTimes.append[0]
        self.endResetModel()

    def updateTime(self, lapTime: int):
        """Updates the lap time of the current lap"""
        self.beginResetModel()
        self.lapTimes[-1] = lapTime
        self.endResetModel()


class CaptureOverview(QtWidgets.QFrame):
    """A vertical widget that displays a live video preview of the footage being captured, and an overview of the
    laps covered during the session"""

    def __init__(self, parent = None):
        super().__init__(parent)
        
        lt = QtWidgets.QVBoxLayout()
        self.setLayout(lt)

        self.videoPreview = QVideoWidget()
        self.videoPreview.setMinimumHeight(200)
        lt.addWidget(self.videoPreview)

        self.laptimesView = QtWidgets.QTableView()
        self.laptimesModel = LaptimeModel()
        self.laptimesView.setModel(self.laptimesModel)
        lt.addWidget(self.laptimesView)
    
    def getVideoWidget(self) -> QVideoWidget:
        return self.videoPreview


class TelemetryPlotItem(pg.PlotItem):

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
    def addData(self, data: ForzaDataPacket):
        """Adds new data to the plot when given a Forza Data Packet"""
    
    @abstractmethod
    def reset(self):
        """Resets the plot"""


class MultiPlotWidget(pg.GraphicsLayoutWidget):
    """Manages and displays multiple plots generated from the session data."""

    def __init__(self, parent=None, show=False, size=None, title=None, **kargs):
        super().__init__(parent, show, size, title, **kargs)

        self.nextid = 0  # The next ID to give to a plot
        self.plots = {}  # A dict of id (int) : plot (plotitem)
    
    def reset(self):
        """Resets all the plots"""
        for plot in self.plots:
            plot.reset()
    
    def removePlot(self, plotId: int):
        """Closes and removes a plot from the layout when given its ID"""
        plot = self.plots.pop(plotId)
        self.removeItem(plot)
    
    def addNewPlot(self, plot: TelemetryPlotItem):
        """Adds a new plot to the widget"""

        plotId = self.nextid
        self.nextid += 1
        
        # Connect the close action
        plot.wantToClose.connect(self.removePlot)

        self.plots[plotId] = plot
        self.addItem(plot)


class CaptureModeWidget(QtWidgets.QFrame):
    """Provides an interface that displays telemetry and previews from currently recorded packets and footage"""

    def __init__(self, parent = None):
        super().__init__(parent)

        lt = QtWidgets.QHBoxLayout()
        self.setLayout(lt)

        self.captureOverview = CaptureOverview()
        self.plots = MultiPlotWidget()

        splitter = QtWidgets.QSplitter()
        lt.addWidget(splitter)
        splitter.addWidget(self.captureOverview)
        splitter.addWidget(self.plots)
    
    def getVideoPreview(self) -> QVideoWidget:
        """Returns the video widget used as a preview for footage capture"""
        return self.captureOverview.getVideoWidget()
    

class WindowListModel(QAbstractListModel):
    """Contains a list of capturable windows currently open"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._window_list = QWindowCapture.capturableWindows()

    def rowCount(self, index: QModelIndex):
        return len(self._window_list)

    def data(self, index: QModelIndex, role: Qt.ItemDataRole):
        if role == Qt.ItemDataRole.DisplayRole:
            window = self._window_list[index.row()]
            return window.description()
        return None

    def window(self, index: QModelIndex):
        """Returns the QCapturableWindow object held at the index"""
        return self._window_list[index.row()]

    def populate(self):
        """Populates the model with all the currently capturable windows"""
        self.beginResetModel()
        self._window_list = QWindowCapture.capturableWindows()
        self.endResetModel()


class CameraDeviceListModel(QAbstractListModel):
    """Contains a list of available cameras"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._camera_list = QMediaDevices.videoInputs()

        # If the cameras change so do the list
        self.qmd = QMediaDevices()
        self.qmd.videoInputsChanged.connect(self.populate)

    def rowCount(self, index: QModelIndex):
        return len(self._camera_list)

    def data(self, index: QModelIndex, role: Qt.ItemDataRole):
        if role == Qt.ItemDataRole.DisplayRole:
            camera = self._camera_list[index.row()]
            return camera.description()
        return None

    def camera(self, index: QModelIndex):
        """Returns the QCameraDevice object held at the index"""
        return self._camera_list[index.row()]

    def populate(self):
        """Populates the model with all the currently capturable windows"""
        self.beginResetModel()
        self._camera_list = QMediaDevices.videoInputs()
        self.endResetModel()


class CameraFormatListModel(QAbstractListModel):
    """Contains a list of possible camera formats for a given camera device"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._camera_format_list = []
        self._camera_device: QCameraDevice = QCameraDevice()

    def rowCount(self, index: QModelIndex):
        return len(self._camera_format_list)

    def data(self, index: QModelIndex, role: Qt.ItemDataRole):
        if role == Qt.ItemDataRole.DisplayRole:
            format: QCameraFormat = self._camera_format_list[index.row()]
            frameRate = ""
            if format.minFrameRate() == format.maxFrameRate():
                frameRate = format.minFrameRate()
            else:
                frameRate = "{}-{}".format(format.minFrameRate(), format.maxFrameRate())
            result = "Resolution={}x{}, Frame Rate={}, Pixel Format={}".format(
                format.resolution().width(), format.resolution().height(),
                frameRate,
                format.pixelFormat().name)
            return result
        return None

    def cameraFormat(self, index: QModelIndex):
        """Returns the QCameraFormat object held at the index"""
        return self._camera_format_list[index.row()]
    
    def changeCameraDevice(self, cameraDevice: QCameraDevice):
        """Reset the format list to the formats found in the new camera device"""
        self._camera_device = cameraDevice
        self.beginResetModel()
        self._camera_format_list = self._camera_device.videoFormats()
        self.endResetModel()


class FootageCaptureSettingsWidget(QtWidgets.QWidget):
    """A widget to help configure settings for capturing race footage"""

    class SourceType(Enum):
        """The footage's source type"""
        Screen = auto()
        Window = auto()
        Camera = auto()


    def __init__(self, parent = None):
        super().__init__(parent)
        
        self._source_type = self.SourceType.Window
        self._window_label = QtWidgets.QLabel("Select window to capture:", self)
        self._camera_label = QtWidgets.QLabel("Select camera to capture:", self)
        self._camera_format_label = QtWidgets.QLabel("Select camera format:", self)
        self._media_capture_session = QMediaCaptureSession(self)  # The capture session that will handle window or camera capture
        self._video_widget_label = QtWidgets.QLabel("Capture output:", self)
        self._start_stop_button = QtWidgets.QPushButton("Select an input device", self)
        self._status_label = QtWidgets.QLabel(self)

        # Whether to capture footage or just telemetry
        formLayout = QtWidgets.QFormLayout()
        self._capture_footage = QtWidgets.QCheckBox()
        self._capture_footage.setChecked(True)
        self._capture_footage.checkStateChanged.connect(self.on_camera_footage_checked)
        formLayout.addRow("Record footage?", self._capture_footage)

        # Starts a preview of the capture session
        self._video_widget = QVideoWidget(self)
        self._media_capture_session.setVideoOutput(self._video_widget)

        # Gets a list of capturable windows
        self._window_list_view = QtWidgets.QListView(self)
        self._window_list_model = WindowListModel(self)
        self._window_list_view.setModel(self._window_list_model)

        self._window_capture = QWindowCapture(self)
        #self._window_capture.start()
        self._media_capture_session.setWindowCapture(self._window_capture)

        # Adds a context menu to the window list view with an action to refresh the capturable windows
        update_window_list_action = QAction("Update Windows List", self)
        update_window_list_action.triggered.connect(self._window_list_model.populate)
        self._window_list_view.addAction(update_window_list_action)
        self._window_list_view.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)

        # Gets a list of cameras that can be captured
        self._camera_device_list_view = QtWidgets.QListView(self)
        self._camera_device_list_model = CameraDeviceListModel(self)
        self._camera_device_list_view.setModel(self._camera_device_list_model)

        self._camera = QCamera(self)
        #self._camera.start()
        self._media_capture_session.setCamera(self._camera)

        # Adds a context menu to the camera list view with an action to refresh the available cameras
        update_camera_device_list_action = QAction("Update Camera List", self)
        update_camera_device_list_action.triggered.connect(self._camera_device_list_model.populate)
        self._camera_device_list_view.addAction(update_camera_device_list_action)
        self._camera_device_list_view.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)

        # Gets a list of all the possible camera formats for the selected camera
        self._camera_format_list_view = QtWidgets.QListView(self)
        self._camera_format_list_model = CameraFormatListModel(self)
        self._camera_format_list_view.setModel(self._camera_format_list_model)

        # Layout the dialog with the camera list, window list, and footage preview
        grid_layout = QtWidgets.QGridLayout(self)
        
        grid_layout.addLayout(formLayout, 0, 0)

        grid_layout.addWidget(self._window_label, 1, 0)
        grid_layout.addWidget(self._window_list_view, 2, 0)

        grid_layout.addWidget(self._camera_label, 3, 0)
        grid_layout.addWidget(self._camera_device_list_view, 4, 0)

        grid_layout.addWidget(self._video_widget_label, 1, 1)
        grid_layout.addWidget(self._video_widget, 2, 1, 1, 1)
        grid_layout.addWidget(self._start_stop_button, 5, 0)
        grid_layout.addWidget(self._status_label, 5, 1)

        grid_layout.addWidget(self._camera_format_label, 3, 1)
        grid_layout.addWidget(self._camera_format_list_view, 4, 1)

        grid_layout.setColumnStretch(1, 1)
        grid_layout.setRowStretch(1, 1)
        grid_layout.setColumnMinimumWidth(0, 400)
        grid_layout.setColumnMinimumWidth(1, 400)
        grid_layout.setRowMinimumHeight(3, 1)
        
        # Connect the functions that update the footage sources when a new window or camera is clicked
        selection_model = self._window_list_view.selectionModel()
        selection_model.selectionChanged.connect(self.on_current_window_selection_changed)

        selection_model = self._camera_device_list_view.selectionModel()
        selection_model.selectionChanged.connect(self.on_current_camera_device_selection_changed)

        selection_model = self._camera_format_list_view.selectionModel()
        selection_model.selectionChanged.connect(self.on_current_camera_format_selection_changed)

        self._start_stop_button.clicked.connect(self.on_start_stop_button_clicked)
        self._window_capture.errorOccurred.connect(self.on_window_capture_error_occured,
                                                   Qt.ConnectionType.QueuedConnection)
        self._camera.errorOccurred.connect(self.on_camera_error_occured,
                                                   Qt.ConnectionType.QueuedConnection)

    def on_camera_footage_checked(self, checked: Qt.CheckState):
        """Disables/enables the widget selection when the capture footage checkbox is clicked"""

        if checked == Qt.CheckState.Checked:
            # enable all the widgets
            self._start_stop_button.setEnabled(True)
            self._window_list_view.setEnabled(True)
            self._camera_device_list_view.setEnabled(True)
            self._camera_format_list_view.setEnabled(True)
        else:
            # disable all the widgets
            self._start_stop_button.setEnabled(False)
            self._window_list_view.setEnabled(False)
            self._camera_device_list_view.setEnabled(False)
            self._camera_format_list_view.setEnabled(False)

    def on_current_window_selection_changed(self, selection):
        self.clear_error_string()
        indexes = selection.indexes()
        if indexes:
            window = self._window_list_model.window(indexes[0])
            if not window.isValid():
                m = "The window is no longer valid. Update the list of windows?"
                answer = QtWidgets.QMessageBox.question(self, "Invalid window", m)
                if answer == QtWidgets.QMessageBox.Yes:
                    self.update_active(self.SourceType.Window, False)
                    self._window_list_view.clearSelection()
                    self._window_list_model.populate()
                    return
            self._window_capture.setWindow(window)
            self.update_active(self.SourceType.Window, self.is_active())
            self._camera_device_list_view.clearSelection()
        else:
            self._window_capture.setWindow(QCapturableWindow())
    
    def on_current_camera_device_selection_changed(self, selection):
        self.clear_error_string()
        indexes = selection.indexes()
        if indexes:
            cameraDevice = self._camera_device_list_model.camera(indexes[0])
            if cameraDevice.isNull():
                m = "The camera is no longer valid. Update the list of cameras?"
                answer = QtWidgets.QMessageBox.question(self, "Invalid camera", m)
                if answer == QtWidgets.QMessageBox.Yes:
                    self.update_active(self.SourceType.Camera, False)
                    self._camera_device_list_view.clearSelection()
                    self._camera_device_list_model.populate()
                    return
            self._camera.setCameraDevice(cameraDevice)
            self.update_active(self.SourceType.Camera, self.is_active())
            self._window_list_view.clearSelection()

            # Populate the camera format model with updated formats
            self._camera_format_list_model.changeCameraDevice(cameraDevice)

        else:
            self._camera.setCameraDevice(QCameraDevice())
            self._camera_format_list_model.changeCameraDevice(QCameraDevice())

    def on_current_camera_format_selection_changed(self, selection):
        """Updates the camera format for the currently selected camera"""
        indexes = selection.indexes()
        if indexes:
            cameraFormat: QCameraFormat = self._camera_format_list_model.cameraFormat(indexes[0])
            self._camera.setCameraFormat(cameraFormat)

    def on_window_capture_error_occured(self, error, error_string):
        self.set_error_string("QWindowCapture: Error occurred " + error_string)
    
    def on_camera_error_occured(self, error, error_string):
        self.set_error_string("QCamera: Error occurred " + error_string)
    
    def set_error_string(self, t):
        self._status_label.setStyleSheet("background-color: rgb(255, 0, 0);")
        self._status_label.setText(t)

    def clear_error_string(self):
        self._status_label.clear()
        self._status_label.setStyleSheet("")

    def on_start_stop_button_clicked(self):
        self.clear_error_string()
        self.update_active(self._source_type, not self.is_active())

    def update_start_stop_button_text(self):
        active = self.is_active()
        if self._source_type == self.SourceType.Window:
            m = "Stop window preview" if active else "Start window preview"
            self._start_stop_button.setText(m)
        elif self._source_type == self.SourceType.Camera:
            m = "Stop camera preview" if active else "Start camera preview"
            self._start_stop_button.setText(m)

    def update_active(self, source_type, active):
        self._source_type = source_type
        self._window_capture.setActive(active and source_type == self.SourceType.Window)
        self._camera.setActive(active and source_type == self.SourceType.Camera)

        self.update_start_stop_button_text()

    def is_active(self):
        if self._source_type == self.SourceType.Window:
            return self._window_capture.isActive()
        if self._source_type == self.SourceType.Camera:
            return self._camera.isActive()
        return False


class TelemetryCaptureSettingsWidget(QtWidgets.QWidget):
    """A widget to help configure settings to capture race telemetry"""
    
    def __init__(self, parent = None):
        super().__init__(parent)

        self._ipLabel = QtWidgets.QLabel("IP Address: " + TelemetryCaptureSettingsWidget.getIP(), self)
        self._testDisplay = QtWidgets.QPlainTextEdit("Waiting for packets...", self)
        self._testDisplay.setReadOnly(True)
        self._testDisplayLabel = QtWidgets.QLabel("Connection Test Output")
        self._testConnectionButton = QtWidgets.QPushButton("Test Connection", self)
        self._testConnectionButton.pressed.connect(self.startTest)
        self._status_label = QtWidgets.QLabel(self)

        self._portSpinBox = QtWidgets.QSpinBox(self)
        self._portSpinBox.setRange(1025, 65535)
        self._portSpinBox.setValue(1337)  # Hard code a default for now, but use value from a config file later

        self._directoryPath: str = None  # Directory the user has chosen
        self._directoryLabel = QtWidgets.QLabel("Choose a folder to save to")
        self._chooseDirectoryButton = QtWidgets.QPushButton("Choose Folder", self)
        self._chooseDirectoryButton.pressed.connect(self.onChooseFolderButtonPressed)

        self._telemetryCapture = TelemetryCapture()
        self._telemetryCapture.setPort(self._portSpinBox.value())
        self._telemetryCapture.signals.activeChanged.connect(self.onActiveChanged)
        self._telemetryCapture.signals.collected.connect(self.onCollected)
        self._portSpinBox.valueChanged.connect(self._telemetryCapture.setPort)

        # 7 second timer for the connection test
        self._timer = QTimer(self)
        self._timer.setInterval(7000)
        self._timer.timeout.connect(self._telemetryCapture.stop)

        self._formLayout = QtWidgets.QFormLayout()
        self._formLayout.addRow("Port", self._portSpinBox)
        self._formLayout.addRow("Destination Folder:", self._directoryLabel)
        
        lt = QtWidgets.QGridLayout()
        self.setLayout(lt)
        lt.addWidget(self._ipLabel, 0, 0)
        lt.addWidget(self._testDisplayLabel, 0, 1)
        lt.addLayout(self._formLayout, 1, 0, 2, 1)
        lt.addWidget(self._chooseDirectoryButton, 2, 0)
        lt.addWidget(self._testDisplay, 1, 1, 2, 1)
        lt.addWidget(self._status_label, 3, 0)
        lt.addWidget(self._testConnectionButton, 3, 1)

        lt.setColumnStretch(1, 1)
        lt.setRowStretch(1, 1)
        lt.setColumnMinimumWidth(0, 400)
        lt.setColumnMinimumWidth(1, 400)
        lt.setRowMinimumHeight(3, 1)

    def startTest(self):
        """Sets up and runs the thread to start listening for UDP Forza data packets"""

        self._portSpinBox.setEnabled(False)
        self._testConnectionButton.setEnabled(False)
        self._testDisplay.clear()
        self._testDisplay.insertPlainText("Running connection test...\n")
        self._testDisplay.insertPlainText("Make sure there is an active Forza race happening in order to receive data.\n\n")

        self._telemetryCapture.start()
        self._timer.start()
        logging.debug("Test started")
    
    def stopTest(self):
        """Stops the thread listening for Forza packets"""
        self._telemetryCapture.stop()
        self._timer.stop()

    def onChooseFolderButtonPressed(self):
        """Opens a file dialog and assigns a directory"""

        path = QtWidgets.QFileDialog.getExistingDirectory(self, "Choose Folder")
        self._directoryPath = path
        self._directoryLabel.setText(path)

    def onCollected(self, fdp: ForzaDataPacket):
        """Called when a single valid Forza Data Packet is collected"""

        logging.debug("onCollected: Received FDP")

        packets = self._telemetryCapture.getPacketsCollected()
        if packets % 60 == 0:
            self._testDisplay.insertPlainText(f"Collected {packets} packets\n")

    def onActiveChanged(self, active: bool):
        """Called when the telemetry capture object changes its active status"""
        if active:
            self._testDisplay.insertPlainText("Telemetry capture active.\n")
            startTime = self._telemetryCapture.getStartTime()
            self._testDisplay.insertPlainText(f"Test started at {startTime}.\n")
        else:
            self.onTestStopped()
            endTime = self._telemetryCapture.getEndTime()
            self._testDisplay.insertPlainText(f"Test finished at {endTime}.\n")

    def onTestStopped(self):
        """Called after the port is closed and the dashboard stops listening to packets"""
        logging.debug("Finished listening")
        self._testDisplay.insertPlainText("Finished test - ")

        valid = self._telemetryCapture.getPacketsCollected()
        invalid = self._telemetryCapture.getInvalidPacketsCollected()

        if valid == 0 and invalid == 0:
            self._testDisplay.insertPlainText("No packets detected. Try another port.\n")
        elif valid == 0 and invalid > 0:
            self._testDisplay.insertPlainText("Connection was established but packets couldn't be processed. Make sure the Packet Format is set to 'Dash'.\n")
        elif valid > 0 and invalid == 0:
            if valid > 350:
                self._testDisplay.insertPlainText("Good connection.\n")
            else:
                self._testDisplay.insertPlainText("Connection established but is poor.\n")
        else:
            self._testDisplay.insertPlainText("Connection was established but some packets couldn't be processed. Try another port.\n")

        self._portSpinBox.setEnabled(True)
        self._testConnectionButton.setEnabled(True)

    def onCaptureError(self, error: TelemetryCapture.Error):
        match error:
            case TelemetryCapture.Error.PortChangeError:
                self.set_error_string(error.value)
                self._portSpinBox.setValue(self._telemetryCapture.getPort())

            case TelemetryCapture.Error.CaptureFailed:
                self.set_error_string(error.value)

            case TelemetryCapture.Error.BadPacketReceived:
                self.set_error_string(error.value)

            case _:
                ...

    def set_error_string(self, t):
        self._status_label.setStyleSheet("background-color: rgb(255, 0, 0);")
        self._status_label.setText(t)

    def clear_error_string(self):
        self._status_label.clear()
        self._status_label.setStyleSheet("")

    def getIP():
        """Returns the local IP address as a string. If an error is encountered while trying to
        establish a connection, it will return None."""

        ip = None
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 1337))
                ip = s.getsockname()[0]
                s.close()
        except:
            return None
        return str(ip)


class CaptureDialog(QtWidgets.QDialog):
    """A dialog to help configure settings for capturing race footage and telemetry"""

    def __init__(self, parent = None):
        super().__init__(parent)

        self.footageWidget = FootageCaptureSettingsWidget()
        self.telemetryWidget = TelemetryCaptureSettingsWidget()
        tabs = QtWidgets.QTabWidget()
        tabs.addTab(self.footageWidget, "Footage Capture Settings")
        tabs.addTab(self.telemetryWidget, "Telemetry Settings")
        
        self._button_box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok |
            QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        self._button_box.accepted.connect(self.onAccepted)
        self._button_box.rejected.connect(self.onRejected)

        lt = QtWidgets.QVBoxLayout()
        self.setLayout(lt)
        lt.addWidget(tabs)
        lt.addWidget(self._button_box)
    
    def onAccepted(self):
        self.telemetryWidget.stopTest()
        sleep(1)
        self.accept()
    
    def onRejected(self):
        self.telemetryWidget.stopTest()
        sleep(1)
        self.reject()
