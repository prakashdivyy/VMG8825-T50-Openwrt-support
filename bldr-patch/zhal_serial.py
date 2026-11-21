#!/usr/bin/env python3
"""Shared UART / ZHAL bootloader helper for bldr-patch scripts.

Consolidates raw 115200 8N1 termios setup, paced line transmission,
ZHAL> catching, ATSE/ATENv3 debug-unlock, and TFTP upload so
dev_flash_cycle.py, netboot.py, and serial_cmd.py stay small and
consistent.

Device selection: pass dev=... (or --dev in CLI scripts) or set
SERIAL_DEV=/dev/cu.usbserial-... in the environment (default: /dev/ttyUSB0).
"""
import os
import re
import select
import subprocess
import sys
import termios
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

DEFAULT_DEV = os.environ.get("SERIAL_DEV", "/dev/ttyUSB0")
DEFAULT_SERVER = os.environ.get("ROUTER_IP", "192.168.1.1")
MODEL = "VMG8825-T50"

if HERE not in sys.path:
    sys.path.insert(0, HERE)
import atenv3


class ZhalError(RuntimeError):
    def __init__(self, msg: str, rc: int):
        super().__init__(msg)
        self.rc = rc


class SerialSession:
    def __init__(
        self,
        dev: str = None,
        logpath: str = None,
        char_delay: float = 0.02,
        cr_delay: float = 0.05,
        echo: bool = True,
    ):
        self.dev = dev or DEFAULT_DEV
        self.char_delay = char_delay
        self.cr_delay = cr_delay
        self.echo = echo
        self.t0 = time.time()
        self.logf = None

        self.fd = os.open(self.dev, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        a = termios.tcgetattr(self.fd)
        a[0] = a[1] = a[3] = 0
        a[4] = a[5] = termios.B115200
        a[6][termios.VMIN] = 0
        a[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, a)
        termios.tcflush(self.fd, termios.TCIOFLUSH)

        if logpath:
            self.logf = open(logpath, "wb")
            print(self.ts(), "[+] full raw session log: %s" % logpath)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def ts(self) -> str:
        return "%6.2fs" % (time.time() - self.t0)

    def _record(self, data: bytes) -> None:
        if self.logf:
            self.logf.write(data)
            self.logf.flush()
        if self.echo:
            sys.stdout.write(data.decode(errors="replace"))
            sys.stdout.flush()

    def send(self, s: str) -> None:
        for c in s.encode():
            os.write(self.fd, bytes([c]))
            if self.char_delay:
                time.sleep(self.char_delay)
        os.write(self.fd, b"\r")
        if self.cr_delay:
            time.sleep(self.cr_delay)

    def pump(
        self,
        t: float,
        tag: str = "",
        until=None,
        keepalive_every: float = 0,
        keepalive_bytes: bytes = b"\r",
        quiet: bool = False,
    ) -> bytes:
        marker = until.encode() if isinstance(until, str) else until
        buf = b""
        end = time.time() + t
        last_keepalive = time.time()
        while time.time() < end:
            r, _, _ = select.select([self.fd], [], [], 0.15)
            if r:
                try:
                    d = os.read(self.fd, 4096)
                except OSError:
                    d = b""
                if d:
                    buf += d
                    self._record(d)
                    if marker and marker in buf:
                        break
            if keepalive_every and (time.time() - last_keepalive) > keepalive_every:
                os.write(self.fd, keepalive_bytes)
                last_keepalive = time.time()
        if not quiet:
            print("\n[%s pump done, tag=%s, got %d bytes]" % (self.ts(), tag, len(buf)))
        return buf

    def pump_until_settled(
        self,
        max_t: float,
        settle_markers,
        quiet_s: float = 8,
        tag: str = "",
    ) -> bytes:
        """Like pump(), but stops early once any settle_marker is seen AND
        the line has then gone quiet for quiet_s seconds."""
        markers = [m.encode() if isinstance(m, str) else m for m in settle_markers]
        buf = b""
        end = time.time() + max_t
        seen_marker_at = None
        last_data = time.time()
        while time.time() < end:
            r, _, _ = select.select([self.fd], [], [], 0.2)
            now = time.time()
            if r:
                try:
                    d = os.read(self.fd, 4096)
                except OSError:
                    d = b""
                if d:
                    buf += d
                    last_data = now
                    self._record(d)
                    if seen_marker_at is None and any(m in buf for m in markers):
                        seen_marker_at = now
            if seen_marker_at is not None and (now - last_data) >= quiet_s:
                break
        print(
            "\n[%s pump_until_settled done, tag=%s, got %d bytes, early=%s]"
            % (self.ts(), tag, len(buf), seen_marker_at is not None)
        )
        return buf

    def catch_zhal(self, window: float = 90):
        end = time.time() + window
        buf = b""
        while time.time() < end:
            os.write(self.fd, b"\r")
            r, _, _ = select.select([self.fd], [], [], 0.1)
            if r:
                try:
                    d = os.read(self.fd, 4096)
                except OSError:
                    d = b""
                if d:
                    buf += d
                    self._record(d)
                    if b"ZHAL>" in buf:
                        return buf
        return None

    def unlock_aten(self, model: str = MODEL) -> None:
        print(self.ts(), "### ATSE %s (debug-unlock seed)" % model)
        self.send("ATSE %s" % model)
        b = self.pump(3, "atse")
        m = re.search(rb"([0-9A-Fa-f]{36})", b)
        if not m:
            raise ZhalError("no seed found in ATSE response", 3)
        seed = m.group(1).decode().upper()
        try:
            pw = atenv3.derive_password(seed)
        except ValueError as e:
            raise ZhalError("no usable password derived from seed: %s" % e, 4)
        print(self.ts(), "[+] seed=%s password=%s" % (seed, pw))
        if not pw or not pw.isdigit():
            raise ZhalError("no usable password derived from seed", 4)
        self.send("ATEN 1,%s" % pw)
        b = self.pump(2.5, "aten").lower()
        if b"incorrect" in b or b"invalid" in b or b"wrong" in b:
            raise ZhalError("ATEN rejected -- unlock failed", 5)
        print(self.ts(), "[+] ATEN unlock OK")

    def tftp_upload(
        self,
        local_path: str,
        remote_name: str,
        server: str = DEFAULT_SERVER,
        timeout: int = 180,
    ) -> None:
        tftp_done = False
        try:
            tools_dir = os.path.join(REPO, "tools")
            if tools_dir not in sys.path:
                sys.path.insert(0, tools_dir)
            import tftp_client
            tftp_client.tftp_put(server, local_path, remote_name=remote_name, quiet=True)
            tftp_done = True
            print(self.ts(), "tftp_client.py upload succeeded")
        except Exception as ex:
            print(self.ts(), "tftp_client.py fallback to atftp binary:", ex)
            r = subprocess.run(
                [
                    "atftp", "--put",
                    "--local-file", local_path,
                    "--remote-file", remote_name,
                    server,
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            print(self.ts(), "atftp rc:", r.returncode, (r.stdout + r.stderr).strip()[:200])
            tftp_done = (r.returncode == 0)
        if not tftp_done:
            raise ZhalError("TFTP transfer failed", 7)

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.logf is not None:
            self.logf.close()
            self.logf = None
