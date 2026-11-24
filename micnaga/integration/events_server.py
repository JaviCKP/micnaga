"""Servidor de la tuberia de eventos worker -> UI (``micnaga.events.pipe_name``).

La app es el **servidor** y el worker el cliente (``PipeEventSink``). Un hilo mantiene siempre
una instancia escuchando; al desconectarse el worker vuelve a aceptar. E/S solapada para poder
parar en cualquier momento (esperando conexion o lectura) sin colgar el cierre. La tuberia solo
admite clientes locales y las lineas mal formadas se descartan.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable
from typing import Any

from micnaga.events import Event, UnknownEventError, decode_line, pipe_name

log = logging.getLogger(__name__)

MAX_LINE = 64 * 1024
ERROR_IO_PENDING = 997
ERROR_PIPE_CONNECTED = 535
PIPE_REJECT_REMOTE_CLIENTS = 0x8


class EventsServer:
    def __init__(self, handler: Callable[[Event], None], name: str | None = None) -> None:
        self._handler = handler
        self._name = name or pipe_name()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop_event: Any = None
        self.name = self._name

    def start(self, timeout_s: float = 5.0) -> None:
        if self._thread is not None:
            return
        import win32event

        self._stop_event = win32event.CreateEvent(None, True, False, None)
        self._thread = threading.Thread(target=self._run, name="micnaga-events-srv", daemon=True)
        self._thread.start()
        self._ready.wait(timeout_s)

    def stop(self) -> None:
        thread = self._thread
        if thread is None:
            return
        import win32event

        self._stop.set()
        win32event.SetEvent(self._stop_event)
        thread.join(3.0)
        self._thread = None

    # ------------------------------------------------------------------ hilo

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                if not self._serve_one():
                    break
        except Exception:
            log.exception("events server crashed")
        finally:
            self._ready.set()

    def _serve_one(self) -> bool:
        import pywintypes
        import win32event
        import win32file
        import win32pipe

        try:
            handle = win32pipe.CreateNamedPipe(
                self._name,
                win32pipe.PIPE_ACCESS_INBOUND | win32file.FILE_FLAG_OVERLAPPED,
                win32pipe.PIPE_TYPE_BYTE
                | win32pipe.PIPE_READMODE_BYTE
                | win32pipe.PIPE_WAIT
                | PIPE_REJECT_REMOTE_CLIENTS,
                1,
                65536,
                65536,
                0,
                None,  # type: ignore[arg-type]
            )
        except pywintypes.error:
            log.exception("cannot create the events pipe")
            return False
        ov = pywintypes.OVERLAPPED()
        ov.hEvent = win32event.CreateEvent(None, True, False, None)
        try:
            try:
                err = win32pipe.ConnectNamedPipe(handle, ov)
            except pywintypes.error as exc:
                err = exc.winerror
            self._ready.set()
            if err == ERROR_IO_PENDING:
                which = win32event.WaitForMultipleObjects(
                    [ov.hEvent, self._stop_event], False, win32event.INFINITE
                )
                if which != win32event.WAIT_OBJECT_0:
                    return True
            elif err not in (0, ERROR_PIPE_CONNECTED):
                return True
            self._read_loop(handle)
        finally:
            with contextlib.suppress(Exception):
                win32file.CloseHandle(handle)
            with contextlib.suppress(Exception):
                win32file.CloseHandle(ov.hEvent)
        return True

    def _read_loop(self, handle: Any) -> None:
        import pywintypes
        import win32event
        import win32file

        buffer = bytearray()
        while not self._stop.is_set():
            ov = pywintypes.OVERLAPPED()
            ov.hEvent = win32event.CreateEvent(None, True, False, None)
            try:
                buf = win32file.AllocateReadBuffer(4096)
                try:
                    _hr, buf = win32file.ReadFile(handle, buf, ov)
                except pywintypes.error as exc:
                    if exc.winerror != ERROR_IO_PENDING:
                        return  # el cliente colgo
                which = win32event.WaitForMultipleObjects(
                    [ov.hEvent, self._stop_event], False, win32event.INFINITE
                )
                if which != win32event.WAIT_OBJECT_0:
                    return
                try:
                    n = win32file.GetOverlappedResult(handle, ov, False)
                except pywintypes.error:
                    return
                if n == 0:
                    return
                buffer.extend(bytes(buf[:n]))
            finally:
                with contextlib.suppress(Exception):
                    win32file.CloseHandle(ov.hEvent)
            self._consume(buffer)

    def _consume(self, buffer: bytearray) -> None:
        while True:
            idx = buffer.find(b"\n")
            if idx < 0:
                if len(buffer) > MAX_LINE:
                    buffer.clear()
                return
            line = bytes(buffer[:idx])
            del buffer[: idx + 1]
            if not line.strip() or len(line) > MAX_LINE:
                continue
            try:
                event = decode_line(line)
            except (UnknownEventError, UnicodeDecodeError):
                log.debug("bad event line dropped")
                continue
            try:
                self._handler(event)
            except Exception:
                log.exception("events handler failed")
