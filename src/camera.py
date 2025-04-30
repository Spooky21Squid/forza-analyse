# importing required libraries 
from PyQt6.QtWidgets import *
from PyQt6.QtGui import *
from PyQt6.QtCore import *
from PyQt6.QtMultimedia import *
from PyQt6.QtMultimediaWidgets import *
import os 
import sys 
import time
import logging

logging.basicConfig(level=logging.INFO)

"""
# Handle high resolution displays:
if hasattr(Qt, 'AA_EnableHighDpiScaling'):
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
if hasattr(Qt, 'AA_UseHighDpiPixmaps'):
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
"""

def cameraFormatToStr(format: QCameraFormat):
    """Turns qcameraformat into a readable string"""

    frameRate = ""
    if format.minFrameRate() == format.maxFrameRate():
        frameRate = format.minFrameRate()
    else:
        frameRate = "{}-{}".format(format.minFrameRate(), format.maxFrameRate())

    result = "QCameraFormat: Resolution={}x{}, Pixel Format={}, Frame Rate={}".format(
        format.resolution().width(), format.resolution().height(),
        format.pixelFormat().name,
        frameRate)
    return result
    
# Main window class 
class MainWindow(QMainWindow): 

    # constructor 
    def __init__(self): 
        super().__init__() 

        # setting geometry 
        self.setGeometry(100, 100, 
						800, 600) 

		# getting available cameras 
        self.available_cameras = QMediaDevices.videoInputs()

		# if no camera found 
        if len(self.available_cameras) == 0: 
			# exit the code 
            sys.exit() 

        self.videoWidget = QVideoWidget()
        self.setCentralWidget(self.videoWidget)

        self.camera = QCamera(self.available_cameras[0])
        self.camera.errorOccurred.connect(self.logError)
        self.camera.start()

        self.mediaCaptureSession = QMediaCaptureSession()
        self.mediaCaptureSession.setCamera(self.camera)
        self.mediaCaptureSession.setVideoOutput(self.videoWidget)

        toolbar = QToolBar()
        toolbar.setIconSize(QSize(16, 16))
        self.addToolBar(toolbar)

        changeCameraAction = QAction("Change Camera", self)
        changeCameraAction.triggered.connect(self.changeCamera)
        toolbar.addAction(changeCameraAction)
    
    def changeCamera(self):
        """Change camera"""
        #self.camera.stop()
        self.camera.setCameraDevice(self.available_cameras[1])
        lastFormat = self.available_cameras[1].videoFormats()[-1]
        self.camera.setCameraFormat(lastFormat)
        #self.camera.start()
        for format in self.available_cameras[1].videoFormats():
            logging.info(cameraFormatToStr(format))
            #logging.info("Format: {}x{}".format(format.resolution().width(), format.resolution().height()))
    
    def logError(error: QCamera.Error, desc: str):
        logging.info("Error: {}".format(desc))

		
# Driver code 
if __name__ == "__main__" : 
	
    # create pyqt app 
    App = QApplication(sys.argv) 

    # create the instance of our Window 
    window = MainWindow() 
    window.show()

    # start the app 
    #sys.exit(App.exec()) 
    App.exec()
