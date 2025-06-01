# Classes to help manage settings in a pyqt6 app

import pathlib
import configparser
import sys
import argparse
import logging

from enum import Enum, auto
from PyQt6 import QtWidgets
from PyQt6.QtCore import QObject, QUrl, pyqtSignal

class Setting(QObject):
    """A class to manage a single setting."""

    class Signals(QObject):
        changed = pyqtSignal(object)

    def __init__(self, section: str, key: str, value = None, parent = None):
        super().__init__(parent)
        self._section = section
        self._key = key
        self._value = value
        self.signals = self.Signals()
    
    def setValue(self, value):
        """Sets the value of the setting, and emits the changed signal"""
        if value != self._value:
            self._value = value
            self.signals.changed.emit(value)
    
    def getName(self) -> str:
        """Returns the name of the setting"""
        return self._key
    
    def getValue(self):
        """Returns the value of the setting"""
        return self._value


class SettingsManagerError(Enum):
    LoadingFailed = auto()
    SavingFailed = auto()
    EditFailed = auto()


class SettingsManager(QObject):
    """An object to help manage settings"""

    class Signals(QObject):
        errorOccurred = pyqtSignal(SettingsManagerError, str)  # Emits error, and a descriptive error string
        loaded = pyqtSignal()
        saved = pyqtSignal(str)  # Emits file path as a string

    def __init__(self, path: QUrl | str | pathlib.Path, parent = None):
        """
        Creates a SettingsManager object given a path to the .ini file. The settings in the .ini file will be read and stored
        in the object.
        
        Parameters
        ----------
        path : The path to a .ini settings file
        """
        super().__init__(parent)
        self.signals = self.Signals()
        self._config = configparser.ConfigParser()
        self._path = None
        self.load(path)
    
    def _setError(self, error: SettingsManagerError, description: str):
        """Emits the error"""
        self.signals.errorOccurred.emit(error, description)
    
    def load(self, path: QUrl | str | pathlib.Path):
        """Loads a new set of settings into the object given a path to a .ini file"""

        if isinstance(path, QUrl):
            path = path.toLocalFile()
        path = str(path)

        l = self._config.read(path)
        if len(l):
            self._path = path
            self.signals.loaded.emit()
        else:
            self._setError(SettingsManagerError.LoadingFailed, "Couldn't load settings")
    
    def getSetting(self, section: str, key: str, fallback = None) -> str:
        """
        Returns a setting value as a string when given its section and key. If the settings can't be found, the fallback value will
        be returned instead (deafult is None)
        
        Parameters
        ----------
        section : The section of the .ini file the setting is found in
        key : The key of the setting
        fallback : The value to return if the setting is not found
        """
        if section not in self._config:
            return fallback
        
        if key not in self._config[section]:
            return fallback
        
        return self._config[section][key]
    
    def setSetting(self, section: str, key: str, value) -> str:
        """Adds a new setting, and modifies the .ini file. Returns how it was stored as a string"""
        
        try:
            value = str(value)
        except:
            self._setError(SettingsManagerError.EditFailed, "Value could not be converted to a string")

        if section not in self._config:
            self._config[section] = {}
        
        self._config[section][key] = value
        return self._config[section][key]
    
    def save(self):
        """Overwrites the .ini file with new settings"""
        try:
            with open(self._path, "w") as f:
                self._config.write(f)
        except IOError as e:
            self._setError(SettingsManagerError.SavingFailed, "Settings couldn't save to file")
        except:
            self._setError(SettingsManagerError.SavingFailed, "Settings couldn't save to file")


class MainWindow(QtWidgets.QMainWindow):

    def __init__(self, settingsFilePath = str, parent = None):
        super().__init__(parent)
        self._settings = SettingsManager(settingsFilePath)
        logging.info(self._settings._config.sections())
        logging.info(self._settings.getSetting("recording", "recordFootage"))
        

def run():
    cli_parser = argparse.ArgumentParser(
        description="script that grabs data from a Forza Motorsport stream and dumps it to a TSV file"
    )

    cli_parser.add_argument('filepath', type=str, help='path to the settings .ini file')
    args = cli_parser.parse_args()

    app = QtWidgets.QApplication(sys.argv)

    db = MainWindow(args.filepath)
    db.showMaximized()

    sys.exit(app.exec())

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()