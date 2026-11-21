#!/usr/bin/env python3
"""Canonical, one-shot netboot ritual for the VMG8825-T50 bldr-patch chain.

Consolidates every proven step into a single reusable script:
  ZHAL> catch -> ATSE/ATENv3/ATEN debug unlock -> ATLD+TFTP kernel to
  0x80020000 -> ATGU to bldr> -> memwl combined relocate+cache+jump stub
  to KSEG1 -> jump -> poll for a live shell prompt (instead of a blind
  fixed-length capture).

PREREQUISITE (not automated here): power-cycle the device first (see
power_cycle.py) -- this script must be started in the SAME turn/
near-immediately after power-on, since the ZHAL> catch window is only
~90s from boot and any delay risks missing it.

Usage: netboot.py <kernel_initramfs.bin> [max_wait_seconds]
Exit codes: 0 = shell prompt reached, 2 = no ZHAL>, 3 = seed not found,
4 = no password, 5 = ATEN rejected, 6 = no bldr>, 7 = TFTP failed,
8 = jumped but no shell prompt within max_wait_seconds (may still be
booting -- check the log).
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import build_combined_kernel_stub as bks
from zhal_serial import DEFAULT_DEV, DEFAULT_SERVER, SerialSession, ZhalError

STUB_BASE = 0xA1000000
SHELL_MARKER = b"root@OpenWrt:~#"
LOGPATH = os.path.join(HERE, "netboot.log")
STUB = os.path.join(HERE, "combined_kernel_stub.bin")
CHAR_DELAY = float(os.environ.get("NETBOOT_CHAR_DELAY", "0.004"))


def die(msg, rc):
    print("\n[!] " + msg)
    sys.exit(rc)


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)

    kernel = sys.argv[1]
    max_wait = int(sys.argv[2]) if len(sys.argv) > 2 else 180

    klen = os.path.getsize(kernel)
    words = bks.build_combined(0x80020000, 0x80002000, klen, 0x80002000)
    open(STUB, "wb").write(b"".join(struct.pack(">I", w) for w in words))

    try:
        with SerialSession(
            dev=DEFAULT_DEV,
            logpath=LOGPATH,
            char_delay=CHAR_DELAY,
            cr_delay=0,
        ) as ser:
            print(ser.ts(), "kernel size = %d (0x%x) bytes" % (klen, klen))
            print(ser.ts(), "combined stub: %d words (%d bytes)" % (len(words), len(words) * 4))

            print(ser.ts(), "### catching ZHAL> (90s CR-spam)")
            if ser.catch_zhal(90) is None:
                die("no ZHAL> within 90s -- device power-cycle likely started too late relative to this script", 2)
            print("\n%s [+] ZHAL> reached" % ser.ts())
            ser.send("")
            ser.pump(1.5, "prompt-clear")

            ser.unlock_aten()

            kname = os.path.basename(kernel)
            print(ser.ts(), "### ATLD %s (to 0x80020000, TFTP)" % kname)
            ser.send("ATLD %s" % kname)
            ser.pump(2, "atld-cmd")
            ser.tftp_upload(kernel, kname, server=DEFAULT_SERVER, timeout=300)
            b = ser.pump(15, "tftp-confirm")
            if b"File download" not in b:
                die("no TFTP confirmation for kernel", 7)

            print(ser.ts(), "### ATGU (to bldr>)")
            ser.send("ATGU")
            b = ser.pump(3, "atgu")
            if b"bldr>" not in b:
                die("did NOT see bldr> after ATGU", 6)

            print(ser.ts(), "### memwl combined stub (%d words) to 0x%x (KSEG1)" % (len(words), STUB_BASE))
            for i, w in enumerate(words):
                addr = STUB_BASE + i * 4
                ser.send("memwl %x %x" % (addr, w))
                ser.pump(1.3, "memwl-%d" % i, until=b"bldr>")

            print(ser.ts(), "### JUMP %x  <<<<< SINGLE-SHOT: COPY + INVALIDATE + JUMP INTO KERNEL >>>>>" % STUB_BASE)
            ser.send("jump %x" % STUB_BASE)

            print(ser.ts(), "### polling for shell prompt (max %ds, CR keepalive every 10s)" % max_wait)
            b = ser.pump(max_wait, "boot-to-shell", keepalive_every=10, until=SHELL_MARKER)

            if SHELL_MARKER in b:
                print(ser.ts(), "[+][+][+] SHELL PROMPT REACHED -- netboot ritual complete")
                sys.exit(0)
            elif b"Undefined Exception" in b or b"Exception happen" in b:
                print(ser.ts(), "[!] EXCEPTION seen -- see", LOGPATH)
                sys.exit(8)
            elif b"Linux version" in b or b"Booting" in b:
                print(ser.ts(), "[~] kernel booted but shell prompt not seen within window -- may just need more time, see", LOGPATH)
                sys.exit(8)
            else:
                print(ser.ts(), "[!] unexpected/silent -- see", LOGPATH)
                sys.exit(8)
    except ZhalError as e:
        die(str(e), e.rc)


if __name__ == "__main__":
    main()
