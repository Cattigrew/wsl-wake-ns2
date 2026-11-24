#!/usr/bin/env python3
"""Broadcast the Switch wake frame using BlueZ btmgmt and a Linux HCI user socket."""

import argparse
import ctypes
from dataclasses import dataclass
import fcntl
import hashlib
import os
from pathlib import Path
import re
import signal
import socket
import struct
import subprocess
import sys
import time


DEFAULT_MAC = "02:00:00:00:00:01"
DEFAULT_ADV = "0201061BFFFFFF000000000000000000000000000000000000000000000000"
MAC_PATTERN = r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}"
SYSFS = Path("/sys/class/bluetooth")


def mgmt(hci, *args):
    result = subprocess.run(
        ["btmgmt", "-i", hci, *args], stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, env={**os.environ, "LC_ALL": "C"},
        timeout=15,
    )
    if result.returncode:
        raise RuntimeError(f"btmgmt {hci} {' '.join(args)}: {result.stdout.strip()}")
    return result.stdout


@dataclass
class State:
    address: str
    powered: bool
    name: str
    short_name: str


def controller_state(hci):
    info = mgmt(hci, "info")
    address = re.search(r"^\s*addr (" + MAC_PATTERN + r")\b", info, re.M)
    settings = re.search(r"^\s*current settings:[ \t]*(.*)$", info, re.M)
    name = re.search(r"^[ \t]*name(?: (.*))?$", info, re.M)
    short_name = re.search(r"^[ \t]*short name(?: (.*))?$", info, re.M)
    if not (address and settings and name and short_name):
        raise RuntimeError(f"Cannot read controller state for {hci}: {info.strip()}")
    return State(address[1].upper(), "powered" in settings[1].split(),
                 name[1] or "", short_name[1] or "")


def locate(parent, expected=None):
    # Set Public Address can remove and re-add the controller with a new index.
    deadline = time.monotonic() + 10
    detail = "controller unavailable"
    while time.monotonic() < deadline:
        matches = [p for p in SYSFS.glob("hci[0-9]*") if p.resolve().parent == parent]
        if len(matches) > 1:
            raise RuntimeError(f"Ambiguous controller at {parent}")
        if matches:
            hci = matches[0].name
            try:
                state = controller_state(hci)
                if expected is None or expected == state.address:
                    return hci, state
                detail = f"read {state.address}, expected {expected}"
            except RuntimeError as error:
                detail = str(error)
        time.sleep(0.2)
    raise RuntimeError(f"Controller at {parent}: {detail}")


def configure_address(parent, address):
    hci, state = locate(parent)
    if state.powered:
        mgmt(hci, "power", "off")
    if state.address != address:
        mgmt(hci, "public-addr", address)
        hci, state = locate(parent, address)
        if state.powered:
            mgmt(hci, "power", "off")
    return hci


def bind_user_channel(sock, index):
    # Older CPython versions only expose the raw HCI channel in socket.bind().
    # sockaddr_hci has three native uint16 fields: family, device and channel.
    sockaddr = struct.pack("=HHH", socket.AF_BLUETOOTH, index, 1)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.bind(sock.fileno(), sockaddr, len(sockaddr)) < 0:
        err = ctypes.get_errno()
        raise OSError(err, f"Opening hci{index} user channel: {os.strerror(err)}")


def command(sock, opcode, data=b""):
    packet = struct.pack("<BHB", 1, opcode, len(data)) + data
    if sock.send(packet) != len(packet):
        raise RuntimeError(f"Short write for HCI 0x{opcode:04X}")
    deadline = time.monotonic() + 5
    while (remaining := deadline - time.monotonic()) > 0:
        sock.settimeout(remaining)
        event = sock.recv(260)
        if not event:
            raise RuntimeError("HCI channel closed")
        if len(event) < 7 or event[0] != 4 or len(event) != 3 + event[2]:
            continue
        if event[1] == 0x0E and int.from_bytes(event[4:6], "little") == opcode:
            status = event[6]
            if status == 0:
                return event[7:]
        elif event[1] == 0x0F and int.from_bytes(event[5:7], "little") == opcode:
            status = event[3]
            if status == 0:
                continue  # Wait for Command Complete.
        else:
            continue
        raise RuntimeError(f"HCI 0x{opcode:04X} rejected with status 0x{status:02X}")
    raise TimeoutError(f"No completion for HCI 0x{opcode:04X}")


def advertise(sock, mac, payload, seconds):
    command(sock, 0x0C03)  # Reset into legacy advertising mode.
    address = command(sock, 0x1009)[::-1].hex(":").upper()  # Read BD_ADDR.
    if address != mac:
        raise RuntimeError(f"Controller returned {address}, expected {mac}")
    # Same parameters as wake-v4: 20–40 ms, ADV_NONCONN_IND, own public
    # address, no peer, channels 37/38/39, no filter.
    params = struct.pack("<HHBBB6xBB", 0x20, 0x40, 3, 0, 0, 7, 0)
    command(sock, 0x2006, params)
    command(sock, 0x2008, bytes([len(payload)]) + payload)
    command(sock, 0x2009, bytes(32))  # Empty scan response.
    try:
        command(sock, 0x200A, b"\x01")
        print(f"Advertising as {address} for {seconds}s: {payload.hex().upper()}", flush=True)
        time.sleep(seconds)
    finally:
        command(sock, 0x200A, b"\x00")


def run(args, payload):
    parent = (SYSFS / args.hci).resolve(strict=True).parent
    key = hashlib.sha256(os.fsencode(parent)).hexdigest()[:20]
    with open(f"/run/bluez-wake-{key}.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _, original = locate(parent)
        try:
            hci = configure_address(parent, args.mac)
            with socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI) as sock:
                bind_user_channel(sock, int(hci[3:]))
                advertise(sock, args.mac, payload, args.seconds)
        finally:
            try:
                hci, state = locate(parent)
                if state.address != original.address:
                    configure_address(parent, original.address)
                    hci, state = locate(parent, original.address)
                if state.powered != original.powered:
                    mgmt(hci, "power", "on" if original.powered else "off")
                state = controller_state(hci)
                if (state.name, state.short_name) != (original.name, original.short_name):
                    mgmt(hci, "name", original.name, original.short_name)
                if controller_state(hci) != original:
                    raise RuntimeError("Controller state differs from its initial state")
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                raise RuntimeError(f"Restore failed (original MAC {original.address}, "
                                   f"powered={original.powered}): {error}") from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hci", default="hci0", help="Linux adapter (default: hci0)")
    parser.add_argument("--mac", default=DEFAULT_MAC, type=str.upper, help="public MAC to transmit as")
    parser.add_argument("--adv", default=DEFAULT_ADV, help="exactly 31 bytes of hex advertising data")
    parser.add_argument("--seconds", type=int, default=1, help="broadcast duration (default: 1 second)")
    args = parser.parse_args()
    if not re.fullmatch(r"hci[0-9]+", args.hci) or not re.fullmatch(MAC_PATTERN, args.mac):
        parser.error("expected --hci hciN and --mac AA:BB:CC:DD:EE:FF")
    hex_data = "".join(args.adv.split())
    if not re.fullmatch(r"[0-9A-Fa-f]{62}", hex_data):
        parser.error("--adv must contain exactly 31 bytes (62 hexadecimal digits)")
    if not 1 <= args.seconds <= 65535:
        parser.error("--seconds must be between 1 and 65535")
    if os.geteuid() != 0:
        parser.error("run with sudo on Linux")

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    run(args, bytes.fromhex(hex_data))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        sys.exit(f"Error: {error}")
