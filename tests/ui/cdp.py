"""Cliente minimo del protocolo Chrome DevTools (solo libreria estandar).

Playwright no esta instalado en este entorno, pero si hay un Chromium de ``ms-playwright`` (o
Edge). Esto basta para pruebas DOM y capturas: lanza el navegador en modo headless con el puerto
de depuracion y habla con el por WebSocket.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import socket
import struct
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any


def find_browser() -> Path | None:
    """Chromium de ms-playwright (preferido) o Microsoft Edge."""
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    cands = sorted(
        (local / "ms-playwright").glob("chromium-*/chrome-win64/chrome.exe"), reverse=True
    )
    for pf in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(pf)
        if base:
            cands.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    for c in cands:
        if c.is_file():
            return c
    return None


class _WS:
    """WebSocket cliente minimo (texto, sin extensiones)."""

    def __init__(self, url: str) -> None:
        m = re.match(r"ws://([^:/]+):(\d+)(/.*)", url)
        if not m:
            raise ValueError(url)
        host, port, path = m.group(1), int(m.group(2)), m.group(3)
        self.sock = socket.create_connection((host, port), timeout=15)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            (
                f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("handshake failed")
            buf += chunk
        if b" 101 " not in buf.split(b"\r\n", 1)[0]:
            raise ConnectionError(buf[:200])
        self._rest = buf.split(b"\r\n\r\n", 1)[1]

    def _read(self, n: int) -> bytes:
        out = b""
        if self._rest:
            out, self._rest = self._rest[:n], self._rest[n:]
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise ConnectionError("closed")
            out += chunk
        return out

    def send(self, text: str) -> None:
        data = text.encode()
        header = bytearray([0x81])
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self) -> str:
        msg = b""
        while True:
            b1, b2 = self._read(2)
            fin, op = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(n)
            if op == 0x9:  # ping
                continue
            if op == 0x8:
                raise ConnectionError("closed by peer")
            msg += payload
            if fin:
                return msg.decode()

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.close()


class Browser:
    """Chromium headless gobernado por CDP. Usar como context manager."""

    def __init__(self, exe: Path | None = None) -> None:
        exe = exe or find_browser()
        if exe is None:
            raise FileNotFoundError("no Chromium/Edge found")
        self._tmp = tempfile.TemporaryDirectory(prefix="micnaga-cdp-")
        self.proc = subprocess.Popen(
            [
                str(exe),
                "--headless=new",
                "--remote-debugging-port=0",
                f"--user-data-dir={self._tmp.name}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-gpu",
                "--hide-scrollbars",
                "--force-color-profile=srgb",
                "--autoplay-policy=no-user-gesture-required",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        port_file = Path(self._tmp.name) / "DevToolsActivePort"
        deadline = time.time() + 20
        while not port_file.exists():
            if time.time() > deadline or self.proc.poll() is not None:
                self.close()
                raise TimeoutError("browser did not start")
            time.sleep(0.05)
        time.sleep(0.1)
        port, path = port_file.read_text().split("\n")[:2]
        self._ws = _WS(f"ws://127.0.0.1:{port}{path}")
        self._id = 0

    # ---- protocol
    def call(
        self, method: str, params: dict[str, Any] | None = None, session: str | None = None
    ) -> Any:
        self._id += 1
        msg: dict[str, Any] = {"id": self._id, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        self._ws.send(json.dumps(msg))
        while True:
            data = json.loads(self._ws.recv())
            if data.get("id") == self._id:
                if "error" in data:
                    raise RuntimeError(f"{method}: {data['error']}")
                return data.get("result", {})

    def new_page(self, width: int = 800, height: int = 600, dpr: float = 1.0) -> Page:
        tid = self.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        sid = self.call("Target.attachToTarget", {"targetId": tid, "flatten": True})["sessionId"]
        page = Page(self, sid, tid)
        page.set_viewport(width, height, dpr)
        return page

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._ws.close()
        self.proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.proc.wait(timeout=5)
        time.sleep(0.2)
        with contextlib.suppress(OSError):
            self._tmp.cleanup()

    def __enter__(self) -> Browser:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class Page:
    def __init__(self, browser: Browser, session: str, target: str) -> None:
        self.b, self.sid, self.target = browser, session, target
        self.call("Page.enable")
        self.call("Runtime.enable")

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        return self.b.call(method, params, self.sid)

    def set_viewport(self, w: int, h: int, dpr: float = 1.0) -> None:
        self.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": w, "height": h, "deviceScaleFactor": dpr, "mobile": False},
        )

    def emulate(self, *, dark: bool | None = None, reduced_motion: bool = False) -> None:
        feats = []
        if dark is not None:
            feats.append({"name": "prefers-color-scheme", "value": "dark" if dark else "light"})
        feats.append(
            {
                "name": "prefers-reduced-motion",
                "value": "reduce" if reduced_motion else "no-preference",
            }
        )
        self.call("Emulation.setEmulatedMedia", {"features": feats})

    def transparent(self, on: bool = True) -> None:
        self.call(
            "Emulation.setDefaultBackgroundColorOverride",
            {"color": {"r": 0, "g": 0, "b": 0, "a": 0}} if on else {},
        )

    def goto(self, url: str, wait: float = 0.6) -> None:
        self.call("Page.navigate", {"url": url})
        deadline = time.time() + 15
        while time.time() < deadline:
            if self.eval("document.readyState") == "complete":
                break
            time.sleep(0.05)
        time.sleep(wait)

    def eval(self, js: str, *, await_promise: bool = False) -> Any:
        r = self.call(
            "Runtime.evaluate",
            {"expression": js, "returnByValue": True, "awaitPromise": await_promise},
        )
        if "exceptionDetails" in r:
            raise RuntimeError(json.dumps(r["exceptionDetails"])[:500])
        return r["result"].get("value")

    def screenshot(
        self, path: Path, *, clip: tuple[float, float, float, float] | None = None
    ) -> Path:
        params: dict[str, Any] = {"format": "png", "captureBeyondViewport": False}
        if clip:
            params["clip"] = {
                "x": clip[0],
                "y": clip[1],
                "width": clip[2],
                "height": clip[3],
                "scale": 1,
            }
        data = self.call("Page.captureScreenshot", params)["data"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(data))
        return path

    # ---- input
    def key(self, key: str, code: str | None = None, vk: int = 0) -> None:
        base = {
            "key": key,
            "code": code or key,
            "windowsVirtualKeyCode": vk,
            "nativeVirtualKeyCode": vk,
        }
        self.call("Input.dispatchKeyEvent", {"type": "keyDown", **base})
        self.call("Input.dispatchKeyEvent", {"type": "keyUp", **base})

    def mouse_move(self, x: float, y: float) -> None:
        self.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})

    def click(self, x: float, y: float) -> None:
        self.mouse_move(x, y)
        for t in ("mousePressed", "mouseReleased"):
            self.call(
                "Input.dispatchMouseEvent",
                {"type": t, "x": x, "y": y, "button": "left", "clickCount": 1},
            )

    def center(self, selector: str) -> tuple[float, float]:
        r = self.eval(
            f"(() => {{ const r = document.querySelector({json.dumps(selector)}).getBoundingClientRect();"  # noqa: E501
            "return [r.x + r.width / 2, r.y + r.height / 2]; })()"
        )
        return float(r[0]), float(r[1])

    def wait_for(self, js: str, timeout: float = 5.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if self.eval(js):
                return True
            time.sleep(0.05)
        return False
