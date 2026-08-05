from __future__ import annotations

import os
import socket


def systemd_notify(message: str) -> bool:
    address = os.getenv("NOTIFY_SOCKET")
    if not address:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as connection:
        connection.connect(address)
        connection.sendall(message.encode("utf-8"))
    return True

