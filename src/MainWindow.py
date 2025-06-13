from PyQt6 import QtWidgets, QtMultimedia
from PyQt6.QtCore import pyqtSlot, QThread, QObject, pyqtSignal, Qt, QSize, QUrl, QAbstractTableModel, QItemSelection, QModelIndex, QRunnable, QThreadPool, QTimer
from PyQt6.QtGui import QAction, QIcon, QKeySequence, QColor, QStandardItemModel, QStandardItem, QPixmap, QPen, QCloseEvent, QGuiApplication
from PyQt6.QtMultimedia import QMediaDevices, QCamera, QMediaCaptureSession, QCameraDevice, QCameraFormat, QScreenCapture, QWindowCapture, QMediaRecorder
from PyQt6.QtMultimediaWidgets import QVideoWidget

import pyqtgraph as pg
import numpy as np
import pandas as pd

import distinctipy
from fdp import ForzaDataPacket
from Utility import ForzaSettings
from CaptureMode import CaptureModeWidget
from Settings import SettingsManager

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
        self.sock.bind(('', self.port))
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
        self._recorder.setOutputLocation(QUrl.fromLocalFile(str(self._outputDirectory / pathlib.Path(filename))))
        self._setActive(True)
        self._recorder.record()
    
    def stop(self):
        """Stops recording footage"""
        self._setActive(False)
        self._recorder.stop()

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

    def stop(self):
        """Stops saving telemetry and closes the file"""
        
        if not self._active:
            return
        
        self._setActive(False)
        if self._file is not None:
            self._file.close()
    
    def savePacket(self, fdp: ForzaDataPacket):
        """Receives a single Forza Data Packet and decides how or if it should be saved, and saves it."""
        
        if not self._active:
            return
        
        # Discard packets that are received when the race is not on if they are not being saved
        if not fdp.is_race_on and self._onlySaveRaceOn:
            return

        # If the car ordinal or track ordinal is different, player has started a new session, so stop saving packets and restart
        if self._firstPacketReceived and (fdp.track_ordinal != self._currentTrackOrdinal or fdp.car_ordinal != self._currentCarOrdinal):
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
        # Make sure telemetry is being captured before trying to save
        if self._telemetryCapture is not None:
            self._telemetryCapture.start()
        if self._telemetryPersistence is not None:
            self._telemetryPersistence.start()
    
    def stopSaving(self):
        """Tells the telemetry persistence object to stop saving packets if one exists"""
        if self._telemetryPersistence is not None:
            self._telemetryPersistence.stop()


class CaptureManager(QObject):
    """Manages and coordinates the capture of Forza telemetry and footage"""

    def __init__(self, parent = None):
        super().__init__(parent)
        self._telemetryManager: TelemetryManager | None = None
        self._footageCapture: FootageCapture | None = None
        self._started: bool = False

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
        if self._telemetryManager is not None:
            self._telemetryManager.startSaving()
        
        if self._footageCapture is not None:
            self._footageCapture.start()
    
    def stopSaving(self):
        """Tells the CaptureManager to stop saving telemetry and recording footage"""
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


class AnalyseModeWidget(QtWidgets.QFrame):
    """Provides an interface for analysing forza telemetry files and footage"""

    def __init__(self, parent = None):
        super().__init__(parent)

        self.grid = QtWidgets.QGridLayout()
        self.setLayout(self.grid)

        self.placeholder = QtWidgets.QLabel("Analyse Mode")
        self.grid.addWidget(self.placeholder, 0, 0)


class CaptureStatusBar(QtWidgets.QFrame):
    """A horizontal status bar for capturing telemetry and footage"""

    class FootageCaptureStatus(Enum):
        NotCapturing = auto()  # No valid footage source set up
        Capturing = auto()  # Footage source detected but not recording to a file yet
        Recording = auto()  # Footage being recorded to a file


    def __init__(self, parent = None):
        super().__init__(parent)
        
        lt = QtWidgets.QHBoxLayout()
        self.setLayout(lt)

        self.port = 0
        self.telemetryCaptureStatus = TelemetryCapture.Status.NotListening
        self.telemetryStatusLabel = QtWidgets.QLabel()
        lt.addWidget(self.telemetryStatusLabel)

        self.footageCaptureStatus = self.FootageCaptureStatus.NotCapturing
        self.footageStatusLabel = QtWidgets.QLabel()
        lt.addWidget(self.footageStatusLabel)

        self.updateFootageStatusLabel()
        self.updateTelemetryStatusLabel()
    
    def updateFootageStatusLabel(self):
        """Update the footage capture label with the currently held status"""
        if self.footageCaptureStatus is self.FootageCaptureStatus.NotCapturing:
            self.footageStatusLabel.setText(f"Footage Status: Not Capturing")
        elif self.footageCaptureStatus is self.FootageCaptureStatus.Capturing:
            self.footageStatusLabel.setText(f"Footage Status: Footage detected.")
        elif self.footageCaptureStatus is self.FootageCaptureStatus.Recording:
            self.footageStatusLabel.setText(f"Footage Status: Recording footage.")

    def setFootageCaptureStatus(self, status):
        """Set the footage capture status"""
        if self.footageCaptureStatus is not status:
            self.footageCaptureStatus = status
            self.updateFootageStatusLabel()

    def updateTelemetryStatusLabel(self):
        """Update the telemetry capture label with the currently held status"""
        if self.telemetryCaptureStatus is TelemetryCapture.Status.NotListening:
            self.telemetryStatusLabel.setText(f"Telemetry Status: Not Listening")
        elif self.telemetryCaptureStatus is TelemetryCapture.Status.Listening:
            self.telemetryStatusLabel.setText(f"Telemetry Status: Listening on port {self.port}.")
        elif self.telemetryCaptureStatus is TelemetryCapture.Status.Capturing:
            self.telemetryStatusLabel.setText(f"Telemetry Status: Capturing Forza data on port {self.port}.")
    
    def setTelemetryCaptureStatus(self, status):
        """Set the telemetry capture status"""
        self.telemetryCaptureStatus = status
        self.updateTelemetryStatusLabel()
    
    def setPort(self, port: int):
        """Sets a new port to be displayed with the status"""
        self.port = port
        self.updateTelemetryStatusLabel()


class MainWindow(QtWidgets.QMainWindow):

    class ModeIndex(Enum):
        AnalyseMode = 0
        CaptureMode = 1


    def __init__(self):
        super().__init__()

        parentDir = pathlib.Path(__file__).parent.parent.resolve()

        # Import the app settings
        settingsManager = SettingsManager()
        settingsManager.load(str(parentDir / pathlib.Path("config/config.json")))

        # Get the details of the directory to save telemetry and footage to
        parentFolder = settingsManager.get("common", "parentFolder", default="default")
        if parentFolder == "default":
            parentFolder = pathlib.Path().home()
        else:
            parentFolder = pathlib.Path(parentFolder)
        folderName = settingsManager.get("common", "folderName", default="forza-analyse")
        saveDirectory = parentFolder / pathlib.Path(folderName)

        self._closeTimer = QTimer()
        self._closeTimer.timeout.connect(self._onCloseTimerTimeout)

        # A DataFrame containing all the track details
        trackDetailsPath = parentDir / pathlib.Path("config/track-details.csv")
        self.forzaTrackDetails = pd.read_csv(str(trackDetailsPath), index_col="ordinal")

        # Set the icon and title
        self.setWindowIcon(QIcon(str(parentDir / pathlib.Path("assets/images/Forza-logo-512.png"))))
        self.setWindowTitle("Forza Analyse")

        mainWidget = QtWidgets.QWidget()
        self.setCentralWidget(mainWidget)
        vbLayout = QtWidgets.QVBoxLayout()
        mainWidget.setLayout(vbLayout)

        # Create a capture status bar
        self.captureStatus = CaptureStatusBar()
        vbLayout.addWidget(self.captureStatus)

        # Set up the footage capture using the directories from the settings file
        self.footageCapture = FootageCapture(footageDirectory=str(saveDirectory.resolve()))

        # Add display widgets for each mode
        self.analyseMode = AnalyseModeWidget()
        self.captureMode = CaptureModeWidget()
        self.footageCapture.setVideoPreview(self.captureMode.getVideoPreview())

        # Create a stacked widget - each child is a different mode (eg. analyse, record)
        self.stackedModes = QtWidgets.QStackedWidget()
        vbLayout.addWidget(self.stackedModes)
        self.stackedModes.addWidget(self.analyseMode)
        self.stackedModes.addWidget(self.captureMode)
        self.mode = self.ModeIndex.AnalyseMode
        self.setModeCapture()

        # Set up telemetry capture and try to start capturing
        p = settingsManager.get("recording", "port", default=7676)
        self.telemetryCapture = TelemetryCapture()
        self.telemetryCapture.signals.statusChanged.connect(self.captureStatus.setTelemetryCaptureStatus)
        self.telemetryCapture.signals.portChanged.connect(self.captureStatus.setPort)
        self.telemetryCapture.setPort(p)
        self.telemetryCapture.start()

        # Set up the telemetry persistence
        self.telemetryPersistence = TelemetryDSVFilePersistence()
        self.telemetryPersistence.setPath(str(saveDirectory))

        # Add and configure the Manager objects
        self.captureManager = CaptureManager()
        self.telemetryManager = TelemetryManager()
        self.telemetryManager.setTelemetryCapture(self.telemetryCapture)
        self.telemetryManager.setTelemetryPersistence(self.telemetryPersistence)
        self.captureManager.setTelemetryManager(self.telemetryManager)
        
        # Add the Toolbar and Actions --------------------------

        toolbar = QtWidgets.QToolBar()
        toolbar.setIconSize(QSize(16, 16))
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.addToolBar(toolbar)

        # Status bar at the bottom of the application
        self.setStatusBar(QtWidgets.QStatusBar(self))

        # Action to configure capture settings - open a dialog to set port number, footage source etc
        configureCaptureAction = QAction(QIcon(str(parentDir / pathlib.Path("assets/icons/gear.png"))), "Capture Settings", self)
        configureCaptureAction.setStatusTip("Configure Capture Settings: Change the settings used for capturing race footage and telemetry.")
        #configureCaptureAction.triggered.connect(self.captureManager.openConfigureDialog)
        toolbar.addAction(configureCaptureAction)

        # Action to start or stop telemetry and footage recording (Actually saving to files, not just capturing packets)
        toggleCaptureAction = QAction(QIcon(str(parentDir / pathlib.Path("assets/icons/control-record.png"))), "Capture", self)
        toggleCaptureAction.setStatusTip("Start/Stop Capture: Start/Stop capturing race footage and telemetry data.")
        toggleCaptureAction.setCheckable(True)
        toggleCaptureAction.triggered.connect(self.captureManager.toggleCapture)
        toolbar.addAction(toggleCaptureAction)

        toolbar.addSeparator()

        # Action to open new sessions and replace any opened ones, to load the telemetry csv files and the associated mp4 video with the same name
        openNewSessionsAction = QAction(QIcon(str(parentDir / pathlib.Path("assets/icons/folder-open-document.png"))), "New Sessions", self)
        openNewSessionsAction.setShortcut(QKeySequence("Ctrl+O"))
        openNewSessionsAction.setStatusTip("Open New Sessions: Opens new CSV telemetry files (and video if there is one) to be analysed, replacing any currently opened sessions.")
        #openNewSessionsAction.triggered.connect(self.sessionManager.openNewSessions)
        toolbar.addAction(openNewSessionsAction)

        # Action to add sessions to be analysed
        addNewSessionsAction = QAction(QIcon(str(parentDir / pathlib.Path("assets/icons/folder--plus.png"))), "Add Sessions", self)
        #addNewSessionsAction.setShortcut(QKeySequence("Ctrl+O"))
        addNewSessionsAction.setStatusTip("Add Sessions: Adds new CSV telemetry files (and video if there is one) to be analysed.")
        #openNewSessionsAction.triggered.connect(self.sessionManager.openNewSessions)
        toolbar.addAction(addNewSessionsAction)

        toolbar.addSeparator()

        # Action to play/pause the videos and animate the graphs
        playPauseAction = QAction(QIcon(str(parentDir / pathlib.Path("assets/icons/control-play-pause.png"))), "Play/Pause", self)
        playPauseAction.setCheckable(True)
        playPauseAction.setShortcut(QKeySequence("Space"))
        playPauseAction.setStatusTip("Play/Pause Button: Plays or pauses the footage and the telemetry graphs.")
        #playPauseAction.triggered.connect(self.videoPlayer.playPause)
        toolbar.addAction(playPauseAction)

        # Action to stop and skip to the beginning of the footage
        stopAction = QAction(QIcon(str(parentDir / pathlib.Path("assets/icons/control-stop.png"))), "Stop", self)
        stopAction.setStatusTip("Stop Button: Stops the footage and skips to the beginning.")
        #stopAction.triggered.connect(self.videoPlayer.stop)
        toolbar.addAction(stopAction)

        # Add a new toolbar for switching between different modes
        modeBar = QtWidgets.QToolBar()
        modeBar.setIconSize(QSize(16, 16))
        modeBar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.addToolBar(modeBar)

        # Action to switch to the analyse mode
        analyseModeAction = QAction(QIcon(str(parentDir / pathlib.Path("assets/icons/magnifier.png"))), "Analyse Mode", self)
        analyseModeAction.setStatusTip("Analyse Mode: Switch to Analyse Mode to view footage and telemetry from saved sessions.")
        #analyseModeAction.setCheckable(True)
        analyseModeAction.setChecked(True)
        analyseModeAction.triggered.connect(self.setModeAnalyse)
        modeBar.addAction(analyseModeAction)

        # Action to switch to the record mode
        captureModeAction = QAction(QIcon(str(parentDir / pathlib.Path("assets/icons/script-attribute-c.png"))), "Capture Mode", self)
        captureModeAction.setStatusTip("Capture Mode: Switch to Capture Mode to view live footage and telemetry.")
        #captureModeAction.setCheckable(True)
        captureModeAction.triggered.connect(self.setModeCapture)
        modeBar.addAction(captureModeAction)

        # Add the menu bar and connect actions ----------------------------
        menu = self.menuBar()

        fileMenu = menu.addMenu("&File")
        fileMenu.addAction(openNewSessionsAction)
        fileMenu.addAction(addNewSessionsAction)

        captureMenu = menu.addMenu("&Capture")
        captureMenu.addAction(configureCaptureAction)
        captureMenu.addAction(toggleCaptureAction)

        playbackMenu = menu.addMenu("&Playback")
        playbackMenu.addAction(playPauseAction)
        playbackMenu.addAction(stopAction)

        modeMenu = menu.addMenu("&Mode")
        modeMenu.addAction(analyseModeAction)
        modeMenu.addAction(captureModeAction)

        # Add the Dock widgets, eg. graph and data table ---------------------

        # Contains actions to open/close the dock widgets
        viewMenu = menu.addMenu("&View")
    
    def _onCloseTimerTimeout(self):
        """Refires a close event"""
        self._closeTimer.stop()
        logging.info("Closing")
        self.close()
    
    def closeEvent(self, event: QCloseEvent):
        """Closes if all processes are finished else closes all processes, sets a timer and tries again."""
        if self.telemetryCapture.isActive():
            self.telemetryCapture.stop()
            logging.info("Stopping Telemetry capture...")
            self._closeTimer.start(500)
            event.ignore()
        else:
            event.accept()

    def setModeAnalyse(self):
        """Switched to Analyse Mode"""
        if self.mode is not self.ModeIndex.AnalyseMode:
            self.stackedModes.setCurrentIndex(self.ModeIndex.AnalyseMode.value)
            self.mode = self.ModeIndex.AnalyseMode

    def setModeCapture(self):
        """Switched to Capture Mode"""
        if self.mode is not self.ModeIndex.CaptureMode:
            self.stackedModes.setCurrentIndex(self.ModeIndex.CaptureMode.value)
            self.mode = self.ModeIndex.CaptureMode

