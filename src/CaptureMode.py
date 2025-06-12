from PyQt6 import QtWidgets, QtMultimedia
from PyQt6.QtCore import pyqtSlot, QThread, QObject, pyqtSignal, Qt, QSize, QUrl, QAbstractTableModel, QItemSelection, QModelIndex, QRunnable, QThreadPool, QTimer
from PyQt6.QtGui import QAction, QIcon, QKeySequence, QColor, QStandardItemModel, QStandardItem, QPixmap, QPen, QCloseEvent
from PyQt6.QtMultimedia import QMediaDevices, QCamera, QMediaCaptureSession, QCameraDevice, QCameraFormat
from PyQt6.QtMultimediaWidgets import QVideoWidget

import pyqtgraph as pg
import numpy as np
import pandas as pd

import distinctipy
from fdp import ForzaDataPacket

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
        lt.addWidget(self.videoPreview)

        self.laptimesView = QtWidgets.QTableView()
        self.laptimesModel = LaptimeModel()
        self.laptimesView.setModel(self.laptimesModel)
    
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