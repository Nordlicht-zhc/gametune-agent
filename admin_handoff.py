"""A one-use localhost readiness signal; no commands or user data are exchanged."""
import secrets
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QHostAddress, QTcpServer, QTcpSocket


class AdminHandoff(QObject):
    ready = Signal()
    expired = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.token = secrets.token_hex(32)
        self.server = QTcpServer(self)
        self.server.newConnection.connect(self.accept)
        if not self.server.listen(QHostAddress.SpecialAddress.LocalHost, 0):
            raise ValueError('无法建立本地窗口交接，请稍后再试')
        self.port = self.server.serverPort()
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.timeout)

    def accept(self):
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            socket.setProperty('message', b'')
            socket.readyRead.connect(lambda socket=socket: self.read(socket))
            socket.disconnected.connect(socket.deleteLater)
            self.read(socket)

    def read(self, socket):
        message = bytes(socket.property('message') or b'') + bytes(socket.readAll())
        if len(message) > 256:
            socket.abort()
            return
        socket.setProperty('message', message)
        if b'\n' not in message:
            return
        valid = secrets.compare_digest(message.strip(), self.token.encode('ascii'))
        socket.disconnectFromHost()
        if valid and self.server.isListening():
            self.stop()
            self.ready.emit()

    def stop(self):
        self.timer.stop()
        self.server.close()

    def timeout(self):
        self.stop()
        self.expired.emit()


def signal_ready(parent, port, token):
    socket = QTcpSocket(parent)
    socket.connected.connect(lambda: socket.write((token + '\n').encode('ascii')))
    socket.disconnected.connect(socket.deleteLater)
    socket.connectToHost(QHostAddress.SpecialAddress.LocalHost, port)
    return socket
