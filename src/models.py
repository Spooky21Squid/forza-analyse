from PyQt6 import QtWidgets, QtMultimedia
from PyQt6.QtCore import Qt, QAbstractTableModel, QModelIndex
from PyQt6.QtGui import QColor

import pyqtgraph as pg
import numpy as np
import pandas as pd

import Utility

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


class LapDetailsModel(DataFrameModel):
    """A table model representing the data for a group of laps"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
    
    def data(self, index: QModelIndex, role):
        if self.frame is None:
                return None
        
        if role == Qt.ItemDataRole.DisplayRole:
            value = self.frame.iat[index.row(), index.column()]
            if index.column() == 5:
                value = Utility.formatLapTime(value)
            return str(value)
        
        if role == Qt.ItemDataRole.BackgroundRole:
            if index.column() == 4:
                value = self.frame.iat[index.row(), index.column()]
                minLapTime = self.frame["lap_time"].min()
                if value == minLapTime:
                    return QColor("purple")
