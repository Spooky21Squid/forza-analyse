# A module to help load, save and sync settings in pyqt applications

from PyQt6 import QtWidgets
from PyQt6.QtCore import QObject, QUrl, pyqtSignal

import argparse
import sys
import logging
import json
import pathlib

"""
Settings should:
- be read from json
- be saved to json

"""


class SettingsManager(QObject):
    """Manages settings across a pyqt app. Can Load settings from a json file at application start-up, and save settings back
    to that json file. Keeps settings updated and synchronised with widgets so that any save operation will save the most
    up to date settings"""

    def __init__(self, parent = None):
        super().__init__(parent)
        self._settings = None
        self._filepath = None
    
    def load(self, filepath: str):
        """Loads a JSON settings file. Overwrites any existing settings. Returns True if successful, False otherwise"""
        try:
            with open(filepath) as f:
                self._settings = json.load(f)
            self._filepath = filepath
            return True
        except:
            return False
    
    def save(self, filepath: str | None = None):
        """Saves all the currently held settings to a file. Will overwrite the original file, unless a new filepath is given.
        Returns True if successful, False otherwise"""
        try:
            if filepath is None:
                filepath = self._filepath
            with open(filepath, "w") as f:
                json.dump(self._settings, f, indent="\t")
            self._filepath = filepath
            return True
        except:
            return False
    
    def get(self, *args, default = None):
        """Returns a single setting when given its keys. Returns a default value (default is None) if setting isn't found"""
        if len(args) == 0:
            return default
        d = self._settings
        for key in args:
            d = d.get(key)
            if d is None:
                return default
        return d
    
    def update(self, value, *args):
        """Saves a single setting when given its keys. Will overwrite an existing setting if the keys match, or will create
        a new setting"""
        if len(args) == 0:
            return
        self._updateHelper(value, self._settings, *args)
    
    def _updateHelper(self, value, root, *args):
        """Recursive helper to the update function"""
        keys = list(args)
        key = keys.pop(0)

        if len(keys) == 0:
            root[key] = value
            return
        else:
            if root.get(key) == None:
                root[key] = dict()
            self._updateHelper(value, root[key], *keys)
    
    def __str__(self):
        return str(self._settings)
    
    def pp(self):
        """Returns a pretty print version of the JSON dictionary"""
        return json.dumps(self._settings, indent=4)
    

class MainWindow(QtWidgets.QMainWindow):
    """Main window"""

    def __init__(self, parent = None):
        super().__init__(parent)

        parentDir = pathlib.Path(__file__).parent.parent.resolve()
        settingsManager = SettingsManager()
        settingsManager.load(str(parentDir / pathlib.Path("config\config.json")))
        print(settingsManager.pp())

        settingsManager.update("YAHOO", "recording", "mario", "speech", "catchphrase")
        mario = settingsManager.get("recording", "mario", "speech", "catchphrase")
        print(settingsManager.save(str(parentDir / pathlib.Path("config/config-copy.json"))))
        print(f"mario: {mario}")


def run():
    cli_parser = argparse.ArgumentParser(
        description="script that grabs data from a Forza Motorsport stream and dumps it to a TSV file"
    )

    cli_parser.add_argument('filepath', type=str, help='path to the settings .json file')
    args = cli_parser.parse_args()

    app = QtWidgets.QApplication(sys.argv)

    db = MainWindow()
    db.showMaximized()

    sys.exit(app.exec())

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()