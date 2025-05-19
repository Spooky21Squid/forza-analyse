# A collection of utility functions

from PyQt6.QtMultimedia import QCameraFormat
import socket
from math import floor
from typing import Literal

class ColourPicker():
    """A class to manage the use of the 6 primary colours through their Hue values"""

    hueValueToWord = {0: "red", 60: "yellow", 120: "green", 180: "cyan", 240: "blue", 300: "magenta"}
    hueWordToValue = {"red": 0, "yellow": 60, "green": 120, "cyan": 180, "blue": 240, "magenta": 300}

    def __init__(self):
        self.available = [] # List of hues that maintains order from lowest to highest hue
        self._populate()

    def _populate(self):
        """Populates the available hues list"""
        self.available = [0, 60, 120, 180, 240, 300]

    def pick(self) -> str:
        """Picks a new colour that isn't already being used. Will always pick the lowest available Hue. Raises IndexError if there
        are no more colours to pick from."""
        hue = self.available.pop(0)
        return ColourPicker.hueValueToWord[hue]

    def putBack(self, hue: int | str):
        """Puts back a colour to be used again. Raises a ValueError if an invalid colour value (hue) is given (Eg. The value was not
        a primary colour). Does nothing if the colour to be put back was already available to use."""

        if isinstance(hue, int):
            if hue > 300 or hue < 0 or hue % 60 != 0:
                raise ValueError("Tried to put back invalid Hue: {}".format(hue))
            
            if hue in self.available:
                return
        else:
            if hue not in ColourPicker.hueValueToWord.values():
                raise ValueError("Tried to put back invalid Hue: {}".format(hue))
            hue = ColourPicker.hueWordToValue[hue]

        # Just use o(n) insert - there's only 6 elements
        index = 0
        while index < len(self.available):
            if self.available[index] > hue:
                self.available.insert(index, hue)
                return
            index += 1
        self.available.append(hue)

    def reset(self):
        """Resets the colour picker"""
        self._populate()


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

class ForzaSettings():
    """Static Forza settings"""

    # The name of each field that a plot can draw values from. Made from the regular Forza data out parameters
    # and a few custom convenience fields added on
    plotAxisTypes = Literal[
        'cur_lap_distance', 'session_no', 'lap_no', 'restart_no', 'filename'  # All custom field names
        'is_race_on', 'timestamp_ms',
        'engine_max_rpm', 'engine_idle_rpm', 'current_engine_rpm',
        'acceleration_x', 'acceleration_y', 'acceleration_z',
        'velocity_x', 'velocity_y', 'velocity_z',
        'angular_velocity_x', 'angular_velocity_y', 'angular_velocity_z',
        'yaw', 'pitch', 'roll',
        'norm_suspension_travel_FL', 'norm_suspension_travel_FR',
        'norm_suspension_travel_RL', 'norm_suspension_travel_RR',
        'tire_slip_ratio_FL', 'tire_slip_ratio_FR',
        'tire_slip_ratio_RL', 'tire_slip_ratio_RR',
        'wheel_rotation_speed_FL', 'wheel_rotation_speed_FR',
        'wheel_rotation_speed_RL', 'wheel_rotation_speed_RR',
        'wheel_on_rumble_strip_FL', 'wheel_on_rumble_strip_FR',
        'wheel_on_rumble_strip_RL', 'wheel_on_rumble_strip_RR',
        'wheel_in_puddle_FL', 'wheel_in_puddle_FR',
        'wheel_in_puddle_RL', 'wheel_in_puddle_RR',
        'surface_rumble_FL', 'surface_rumble_FR',
        'surface_rumble_RL', 'surface_rumble_RR',
        'tire_slip_angle_FL', 'tire_slip_angle_FR',
        'tire_slip_angle_RL', 'tire_slip_angle_RR',
        'tire_combined_slip_FL', 'tire_combined_slip_FR',
        'tire_combined_slip_RL', 'tire_combined_slip_RR',
        'suspension_travel_meters_FL', 'suspension_travel_meters_FR',
        'suspension_travel_meters_RL', 'suspension_travel_meters_RR',
        'car_ordinal', 'car_class', 'car_performance_index',
        'drivetrain_type', 'num_cylinders',
        'position_x', 'position_y', 'position_z',
        'speed', 'power', 'torque',
        'tire_temp_FL', 'tire_temp_FR',
        'tire_temp_RL', 'tire_temp_RR',
        'boost', 'fuel', 'dist_traveled',
        'best_lap_time', 'last_lap_time',
        'cur_lap_time', 'cur_race_time',
        'lap_no', 'race_pos',
        'accel', 'brake', 'clutch', 'handbrake',
        'gear', 'steer',
        'norm_driving_line', 'norm_ai_brake_diff',
        'tire_wear_FL', 'tire_wear_FR',
        'tire_wear_RL', 'tire_wear_RR',
        'track_ordinal'
    ]

    # All possible parameters sent by Forza data out in order
    params = Literal[
        'is_race_on', 'timestamp_ms',
        'engine_max_rpm', 'engine_idle_rpm', 'current_engine_rpm',
        'acceleration_x', 'acceleration_y', 'acceleration_z',
        'velocity_x', 'velocity_y', 'velocity_z',
        'angular_velocity_x', 'angular_velocity_y', 'angular_velocity_z',
        'yaw', 'pitch', 'roll',
        'norm_suspension_travel_FL', 'norm_suspension_travel_FR',
        'norm_suspension_travel_RL', 'norm_suspension_travel_RR',
        'tire_slip_ratio_FL', 'tire_slip_ratio_FR',
        'tire_slip_ratio_RL', 'tire_slip_ratio_RR',
        'wheel_rotation_speed_FL', 'wheel_rotation_speed_FR',
        'wheel_rotation_speed_RL', 'wheel_rotation_speed_RR',
        'wheel_on_rumble_strip_FL', 'wheel_on_rumble_strip_FR',
        'wheel_on_rumble_strip_RL', 'wheel_on_rumble_strip_RR',
        'wheel_in_puddle_FL', 'wheel_in_puddle_FR',
        'wheel_in_puddle_RL', 'wheel_in_puddle_RR',
        'surface_rumble_FL', 'surface_rumble_FR',
        'surface_rumble_RL', 'surface_rumble_RR',
        'tire_slip_angle_FL', 'tire_slip_angle_FR',
        'tire_slip_angle_RL', 'tire_slip_angle_RR',
        'tire_combined_slip_FL', 'tire_combined_slip_FR',
        'tire_combined_slip_RL', 'tire_combined_slip_RR',
        'suspension_travel_meters_FL', 'suspension_travel_meters_FR',
        'suspension_travel_meters_RL', 'suspension_travel_meters_RR',
        'car_ordinal', 'car_class', 'car_performance_index',
        'drivetrain_type', 'num_cylinders',
        'position_x', 'position_y', 'position_z',
        'speed', 'power', 'torque',
        'tire_temp_FL', 'tire_temp_FR',
        'tire_temp_RL', 'tire_temp_RR',
        'boost', 'fuel', 'dist_traveled',
        'best_lap_time', 'last_lap_time',
        'cur_lap_time', 'cur_race_time',
        'lap_no', 'race_pos',
        'accel', 'brake', 'clutch', 'handbrake',
        'gear', 'steer',
        'norm_driving_line', 'norm_ai_brake_diff',
        'tire_wear_FL', 'tire_wear_FR',
        'tire_wear_RL', 'tire_wear_RR',
        'track_ordinal'
    ]

def QCameraFormatToStr(format: QCameraFormat):
    """Turns qcameraformat into a readable string"""

    frameRate = ""
    if format.minFrameRate() == format.maxFrameRate():
        frameRate = format.minFrameRate()
    else:
        frameRate = "{}-{}".format(format.minFrameRate(), format.maxFrameRate())

    result = "Resolution: {}x{}, Pixel Format: {}, Frame Rate: {}".format(
        format.resolution().width(), format.resolution().height(),
        format.pixelFormat().name,
        frameRate)
    return result

def formatLapTime(lapTime: float) -> str:
    """Formats a lap time as a float type into a readable string format of type MM:ss.mmmm"""
    minutes, seconds = divmod(lapTime, 60)
    mseconds = str(seconds - floor(seconds))  # gets us the decimal part
    mseconds = mseconds[2:5]
    result = "{}:{}.{}".format(int(minutes), int(seconds), mseconds)
    return result
