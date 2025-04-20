# A collection of utility functions

import socket

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