#!/usr/bin/env python3
"""One-command dev loop for VMG8825-T50 OpenWrt kernel/image iteration:
build (optional) -> power-cycle (see power_cycle.py -- manual by default,
optional Home Assistant backend) -> ATSE/ATEN debug-unlock (fresh every
run; unnecessary if your bootloader has the RSA+CRC patches from
BOOTLOADER-PATCH.md installed, harmless to run anyway) -> flash MAIN via
ATER/ATWF (NOT ATUR, which silently no-ops after first use) -> ATGO ->
parse the boot console -> auto-recover a stuck boot flag once -> clear
PASS/FAIL verdict.

Safety invariants preserved and re-asserted here:
  - ONLY mtd3 (tclinux/MAIN, blocks 4-451 inclusive) is ever erased/written.
  - tclinux_slave is never referenced by any erase/write call in this file.

Usage:
    # Wrap + flash + boot-test an existing slim OpenWrt build:
    python3 dev_flash_cycle.py --slim \
        openwrt/bin/targets/econet/en751627/openwrt-econet-en751627-zyxel_vmg8825-t50-squashfs-tclinux.trx

    # Flash + boot-test an already-era-wrapped image (skip build_era_trx.py):
    python3 dev_flash_cycle.py --era vmg8825-t50-era.bin

    # Only re-run the boot-test against whatever is currently in MAIN
    # (no flash step -- useful for re-checking the boot flag / retrying ATGO):
    python3 dev_flash_cycle.py --boot-only
"""
import argparse
import hashlib
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import power_cycle
from zhal_serial import DEFAULT_DEV, DEFAULT_SERVER, SerialSession, ZhalError

BUILD_ERA_TRX = os.path.join(REPO, "build_era_trx.py")

RAM_ADDR = 0x80020000
MTD3_OFF = 0x80000
MTD3_BLK_LO = 4
MTD3_BLK_HI = 451  # INCLUSIVE. Re-derive (do not copy) if your unit's layout differs.
assert (MTD3_BLK_HI + 1 - MTD3_BLK_LO) * 0x20000 == 0x3800000, "block math sanity check failed"
assert (MTD3_BLK_HI + 1) * 0x20000 == 0x3880000, "must land exactly at mtd5 (tclinux_slave) boundary, not past it"
MTD3_MAX_LEN = (MTD3_BLK_HI + 1) * 0x20000 - MTD3_OFF


def die(msg, rc):
    print("\n[!] " + msg)
    sys.exit(rc)


def flash_main(ser: SerialSession, image_path: str, server: str) -> None:
    fw_bytes = open(image_path, "rb").read()
    fw_len = len(fw_bytes)
    fw_md5 = hashlib.md5(fw_bytes).hexdigest()
    if fw_len > MTD3_MAX_LEN:
        die("image (%d bytes) too big for mtd3 (%d bytes)" % (fw_len, MTD3_MAX_LEN), 6)

    fw_name = "devfw.bin"
    print(ser.ts(), "### ATLD %s -> RAM 0x%x (%d bytes, md5 %s)" % (fw_name, RAM_ADDR, fw_len, fw_md5))
    ser.send("ATLD %s" % fw_name)
    ser.pump(2, "atld-cmd")
    ser.tftp_upload(image_path, fw_name, server=server, timeout=180)
    b = ser.pump(20, "tftp-confirm")
    if b"File download" not in b:
        die("no TFTP confirmation for image", 7)

    print(
        ser.ts(),
        "### ATER %d,%d (erase mtd3 ONLY: 0x%x..0x%x)"
        % (MTD3_BLK_LO, MTD3_BLK_HI, MTD3_OFF, (MTD3_BLK_HI + 1) * 0x20000),
    )
    ser.send("ATER %d,%d" % (MTD3_BLK_LO, MTD3_BLK_HI))
    b = ser.pump(60, "ater", until="ZHAL>")
    if b"ZHAL>" not in b:
        die("no ZHAL> after ATER -- STOP, investigate before writing anything", 8)

    print(ser.ts(), "### ATWF 0x%x,0x%x,0x%x (RAM -> flash, mtd3 body)" % (RAM_ADDR, MTD3_OFF, fw_len))
    ser.send("ATWF 0x%x,0x%x,0x%x" % (RAM_ADDR, MTD3_OFF, fw_len))
    b = ser.pump(30, "atwf", until="ZHAL>")
    if b"ZHAL>" not in b:
        die("no ZHAL> after ATWF -- STOP, investigate before rebooting", 9)
    print(ser.ts(), "[+] mtd3 flashed (%d bytes)" % fw_len)


def fix_boot_flag(ser: SerialSession) -> None:
    print(ser.ts(), "### boot flag stuck on slave -- power-cycling back to ZHAL> to fix it")
    if power_cycle.power_cycle_and_catch_zhal(lambda: ser.catch_zhal(90)) is None:
        die("no ZHAL> within 90s while trying to fix a stuck boot flag", 11)
    ser.send("")
    ser.pump(1.0)
    ser.unlock_aten()
    ser.send("ATBT 1")
    ser.pump(2.0, "atbt")
    ser.send("ATSW")
    ser.pump(3.0, "atsw")


def boot_and_analyze(ser: SerialSession, allow_flag_fix: bool = True) -> bool:
    print(ser.ts(), "### ATGO -- capturing boot console")
    ser.send("ATGO")
    settle_on = ("Mounted root", "root@OpenWrt", "login:", "Kernel panic")
    full = ser.pump_until_settled(150, settle_on, quiet_s=8, tag="atgo").decode(errors="replace")

    flags = re.findall(r"==> boot flag = (\d+)", full)
    booted_main = "from main" in full
    booted_slave = "from slave" in full
    hash_fail = "Wrong image hash" in full
    panic = "Kernel panic" in full
    mounted = "Mounted root" in full
    shell = "root@OpenWrt" in full or "login:" in full

    print("\n========== BOOT ANALYSIS ==========")
    print("boot flag occurrences:", flags)
    print(
        "booted_main=%s booted_slave=%s hash_fail=%s panic=%s mounted=%s shell=%s"
        % (booted_main, booted_slave, hash_fail, panic, mounted, shell)
    )

    if booted_slave and allow_flag_fix:
        print("[!] fell back to SLAVE -- attempting one automatic boot-flag fix + retry")
        fix_boot_flag(ser)
        ser.unlock_aten()  # fresh unlock right before the retry ATGO
        return boot_and_analyze(ser, allow_flag_fix=False)

    ok = booted_main and mounted and not panic and not hash_fail
    print("VERDICT: %s" % ("PASS" if ok else "FAIL"))
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slim", help="slim OpenWrt .trx to wrap via build_era_trx.py before flashing")
    ap.add_argument("--era", help="already era-wrapped image to flash directly (skips build step)")
    ap.add_argument("--boot-only", action="store_true", help="skip flashing, just power-cycle + ATGO + analyze")
    ap.add_argument("--off-seconds", type=int, default=15)
    ap.add_argument("--dev", default=DEFAULT_DEV, help="serial device (default: $SERIAL_DEV or /dev/ttyUSB0)")
    ap.add_argument("--server", default=DEFAULT_SERVER, help="router TFTP IP (default: 192.168.1.1)")
    args = ap.parse_args()

    if not args.boot_only and not args.slim and not args.era:
        die("need --slim, --era, or --boot-only", 1)

    image_path = args.era
    if args.slim:
        image_path = os.path.join(HERE, "dev-flash-era.bin")
        print("### building era image from %s" % args.slim)
        r = subprocess.run([sys.executable, BUILD_ERA_TRX, "--slim", args.slim, "-o", image_path])
        if r.returncode != 0:
            die("build_era_trx.py failed (rc=%d)" % r.returncode, 10)

    logpath = os.path.join(HERE, "dev_flash_cycle_%d.log" % int(time.time()))
    try:
        with SerialSession(dev=args.dev, logpath=logpath) as ser:
            print(ser.ts(), "### power-cycling router and catching ZHAL>")
            if power_cycle.power_cycle_and_catch_zhal(lambda: ser.catch_zhal(90), off_seconds=args.off_seconds) is None:
                die("no ZHAL> within 90s of power-on", 2)
            print("\n%s [+] ZHAL> reached" % ser.ts())
            ser.send("")
            ser.pump(1.0)

            if not args.boot_only:
                ser.unlock_aten()
                flash_main(ser, image_path, args.server)

            ser.unlock_aten()  # fresh unlock right before ATGO -- required every boot, see README §4
            ok = boot_and_analyze(ser)
            sys.exit(0 if ok else 1)
    except ZhalError as e:
        die(str(e), e.rc)


if __name__ == "__main__":
    main()
