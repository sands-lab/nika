"""Accept TCP probes on a data-plane address."""

import socket
import sys

with socket.socket() as listener:
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((sys.argv[1], 9116))
    listener.listen(128)
    while True:
        connection, _ = listener.accept()
        connection.close()
