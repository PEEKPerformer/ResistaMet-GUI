"""A Prologix GPIB-ETHERNET adapter, as far as pyvisa-py talks to one.

A TCP listener on localhost that swallows the adapter's ``++`` commands,
remembers the addressed instrument, and answers ``*IDN?`` on the next
``++read``. Enough to take the real pyvisa-py Prologix sessions through
open, address, write, read and close without hardware. It is a stand-in
for the protocol pyvisa-py speaks, not for a real adapter's timing.
"""
import socket
import threading
from typing import List, Optional

IDN = 'KEITHLEY INSTRUMENTS INC.,MODEL 2400,1234567,C32   Oct  4 2010 14:20:11/A02  /K/J'


class FakePrologix:
    def __init__(self) -> None:
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.bind(('127.0.0.1', 0))
        self._server.listen(4)
        self.port: int = self._server.getsockname()[1]
        #: Every line received, in order, without its terminator.
        self.lines: List[str] = []
        #: How many times a client connected.
        self.connections = 0
        self._thread = threading.Thread(target=self._accept, name='fake-prologix', daemon=True)
        self._thread.start()

    def resource(self, board: str = '') -> str:
        return f'PRLGX-TCPIP{board}::127.0.0.1::{self.port}::INTFC'

    def close(self) -> None:
        try:
            self._server.close()
        except OSError:
            pass

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        buffer = b''
        pending: Optional[bytes] = None
        with conn:
            while True:
                try:
                    data = conn.recv(4096)
                except OSError:
                    return
                if not data:
                    return
                buffer += data
                while b'\n' in buffer:
                    raw, buffer = buffer.split(b'\n', 1)
                    line = raw.decode('ascii', 'replace').strip()
                    if not line:
                        continue
                    self.lines.append(line)
                    if line.startswith('++read'):
                        if pending is not None:
                            conn.sendall(pending)
                            pending = None
                    elif line.startswith('++'):
                        continue
                    elif line == '*IDN?':
                        pending = (IDN + '\n').encode('ascii')
