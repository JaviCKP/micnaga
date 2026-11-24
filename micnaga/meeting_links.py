"""Citas de la biblioteca que abren una reunión en su minuto exacto."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from micnaga.events import OpenMeeting, encode_line, pipe_name

_LINK = re.compile(
    r"micnaga://reunion/([1-9][0-9]{0,18})\?t=([0-9]{1,9}(?:\.[0-9]{1,3})?)"
    r"&b=([a-f0-9]{16})(?:&d=([a-f0-9]{32}))?"
)


def library_id(root: Path) -> str:
    return hashlib.sha256(os.fsencode(os.path.normcase(str(root.resolve())))).hexdigest()[:16]


def meeting_link(root: Path, meeting_id: int, seconds: float = 0.0) -> str:
    return indexed_meeting_link(library_id(root), meeting_id, seconds)


def indexed_meeting_link(
    library: str, meeting_id: int, seconds: float, device_id: str | None = None,
) -> str:
    link = f"micnaga://reunion/{meeting_id}?t={seconds:.3f}&b={library}"
    return f"{link}&d={device_id}" if device_id is not None else link


def parse_meeting_link(value: str) -> OpenMeeting | None:
    match = _LINK.fullmatch(value)
    if match is None:
        return None
    meeting_id, seconds, library, device = match.groups()
    if int(meeting_id) >= 2**63:
        return None
    return OpenMeeting(int(meeting_id), float(seconds), library, device)


def link_pipe_name() -> str:
    return pipe_name() + "-links"


def send_meeting_link(link: OpenMeeting, *, timeout_s: float = 4.0) -> bool:
    """Reenvía una cita a la instancia abierta, también si está procesando una reunión."""
    deadline = time.monotonic() + timeout_s
    name = link_pipe_name()
    data = encode_line(link)
    while True:
        try:
            with open(name, "wb", buffering=0) as pipe:
                return pipe.write(data) == len(data)
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)


def open_in_micnaga(link: str) -> None:
    """Abre una cita cuando el cliente de Claude no ejecuta enlaces de Windows."""
    reference = parse_meeting_link(link)
    if reference is None:
        raise ValueError("cita de Micnaga no válida")
    if send_meeting_link(reference, timeout_s=0):
        return
    exe = Path(sys.executable)
    if getattr(sys, "frozen", False):
        command = [str(exe.with_name("micnaga.exe"))]
    else:
        command = [str(exe), "-m", "micnaga"]
    subprocess.Popen(
        [*command, "app", "--open-url", link],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
