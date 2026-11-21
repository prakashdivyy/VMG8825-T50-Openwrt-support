#!/usr/bin/env python3
"""Derive the Zyxel ATENv3 bootloader unlock password from a 36-char ATSE seed.

Direct Python port of tools/atenv3/atenv3_passwd.c (Caleb James DeLisle, GPL-2.0)
so bldr-patch scripts work natively on Linux (x86-64/ARM64) and macOS without a
precompiled x86-64 ELF binary.

Usage:
    python3 atenv3.py <atse-hex-36chars>
"""
import sys

MAGIC = (0x10F0A563, 0xBEAFBEAF, 0x14387052)


def _ror32(val: int, n: int) -> int:
    return ((val >> n) | (val << (32 - n))) & 0xFFFFFFFF


def derive_password(atse: str) -> str:
    atse = atse.strip()
    if len(atse) != 36:
        raise ValueError(f"expected 36 hex chars, got {len(atse)}")

    field0 = "".join(atse[i * 3 + 1] for i in range(12))
    field1 = "N" + "".join(atse[i * 3] for i in range(12))
    field3 = atse[2] + atse[5]
    field4 = atse[8] + atse[11]
    field5 = atse[14] + atse[17]
    field6 = "".join(atse[20 + i * 3] for i in range(6))

    u3 = int(field1[1:5] + field0[0:4], 16)
    u4 = int(field1[5:9] + field0[4:8], 16)
    u5 = int(field1[9:13] + field0[8:12], 16)

    u7 = int(field6, 16)
    u12 = (int(field3, 16) + int(field4, 16) + int(field5, 16)) & 0xFFFFFFFF

    u11 = ((u7 & 0xFFFFFF) + MAGIC[u12 % 3]) & 0xFFFFFFFF
    rounds = (u11 >> (u12 & 3)) & 0xFF

    for _ in range(rounds):
        u3 = _ror32(u3, 1)
        u4 = _ror32(u4, 2)
        u5 = _ror32(u5, 3)

    return f"{u3 ^ u11}{u4 ^ u11}{u5 ^ u11}"[:30]


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(f"usage: {sys.argv[0]} <atse-hex-36chars>")
    try:
        print(derive_password(sys.argv[1]))
    except ValueError as e:
        sys.exit(f"atenv3: {e}")


if __name__ == "__main__":
    main()
