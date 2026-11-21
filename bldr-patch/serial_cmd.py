#!/usr/bin/env python3
"""Send one or more shell commands over the already-booted serial console
and print the output. Assumes a live 'root@OpenWrt:~#'-style prompt is
already up (i.e. run right after netboot.py reports SHELL PROMPT REACHED).

Usage: serial_cmd.py "<cmd1>" ["<cmd2>" ...]
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from zhal_serial import DEFAULT_DEV, SerialSession


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)

    with SerialSession(dev=DEFAULT_DEV, char_delay=0.01, cr_delay=0, echo=False) as ser:
        # wake the prompt / drain any pending banner
        ser.send("")
        ser.pump(1, quiet=True)

        for cmd in sys.argv[1:]:
            ser.send(cmd)
            out = ser.pump(3, quiet=True)
            sys.stdout.write(out.decode(errors="replace"))
            sys.stdout.flush()


if __name__ == "__main__":
    main()
