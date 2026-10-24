"""Disc and image helpers for Playstation Disc Dumper.

- Parsing .cue sheets and reading PlayStation serials (SYSTEM.CNF) from images.
- Raw reading of physical CD/DVD drives on Windows via SCSI pass-through,
  so PS1 discs can be ripped to a 2352-byte/sector .bin/.cue before chdman.
"""

import ctypes
import os
import re
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path

RAW_SECTOR = 2352
DVD_SECTOR = 2048


# ---------------------------------------------------------------------------
# Cue sheets and image inspection
# ---------------------------------------------------------------------------

# (sector size, offset of user data inside a sector) per cue track mode
CUE_MODES = {
    "MODE1/2048": (2048, 0),
    "MODE1/2352": (2352, 16),
    "MODE2/2336": (2336, 8),
    "MODE2/2352": (2352, 24),
}


@dataclass
class CueInfo:
    files: list = field(default_factory=list)  # Paths referenced by FILE lines
    tracks: list = field(default_factory=list)  # (number, mode)
    missing: list = field(default_factory=list)

    @property
    def size(self):
        return sum(f.stat().st_size for f in self.files if f.exists())


def parse_cue(cue_path):
    cue_path = Path(cue_path)
    info = CueInfo()
    text = cue_path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r'FILE\s+"(.+)"\s+\S+$', line, re.I) or re.match(r"FILE\s+(\S+)\s+\S+$", line, re.I)
        if m:
            f = cue_path.parent / m.group(1)
            info.files.append(f)
            if not f.exists():
                info.missing.append(f.name)
            continue
        m = re.match(r"TRACK\s+(\d+)\s+(\S+)", line, re.I)
        if m:
            info.tracks.append((int(m.group(1)), m.group(2).upper()))
    return info


def _iso_serial(read_sector):
    """Return (serial, volume_id) from an ISO9660 filesystem.

    read_sector(lba) must return the 2048 bytes of user data of that sector.
    """
    pvd = read_sector(16)
    if pvd[1:6] != b"CD001":
        return None, None
    volume = pvd[40:72].decode("ascii", "replace").strip()
    root_lba = int.from_bytes(pvd[156 + 2:156 + 6], "little")
    root_len = int.from_bytes(pvd[156 + 10:156 + 14], "little")
    for i in range(min((root_len + 2047) // 2048, 16)):
        sec = read_sector(root_lba + i)
        pos = 0
        while pos < 2048:
            rec_len = sec[pos]
            if rec_len == 0:
                break
            name_len = sec[pos + 32]
            name = sec[pos + 33:pos + 33 + name_len].decode("ascii", "replace").upper()
            if name.split(";")[0] == "SYSTEM.CNF":
                lba = int.from_bytes(sec[pos + 2:pos + 6], "little")
                size = int.from_bytes(sec[pos + 10:pos + 14], "little")
                cnf = read_sector(lba)[:min(size, 2048)].decode("ascii", "replace")
                m = re.search(r"BOOT2?\s*=\s*cdrom0?:\\?([A-Z]{4})[_-](\d{3})\.?(\d{2})", cnf, re.I)
                if m:
                    return f"{m.group(1).upper()}-{m.group(2)}{m.group(3)}", volume
                return None, volume
            pos += rec_len
    return None, volume


def image_serial(path):
    """Best-effort (serial, volume_id) for a .cue or .iso file."""
    path = Path(path)
    try:
        if path.suffix.lower() == ".cue":
            cue = parse_cue(path)
            if not cue.files or not cue.tracks or cue.missing:
                return None, None
            mode = cue.tracks[0][1]
            if mode not in CUE_MODES:
                return None, None
            size, offset = CUE_MODES[mode]
            data_file = cue.files[0]
        else:
            size, offset, data_file = DVD_SECTOR, 0, path
        with open(data_file, "rb") as f:
            def read_sector(lba):
                f.seek(lba * size + offset)
                return f.read(2048).ljust(2048, b"\0")
            return _iso_serial(read_sector)
    except (OSError, ValueError, IndexError):
        return None, None


# ---------------------------------------------------------------------------
# Windows SCSI pass-through
# ---------------------------------------------------------------------------

class ScsiError(Exception):
    def __init__(self, message, sense_key=None, asc=None):
        super().__init__(message)
        self.sense_key = sense_key
        self.asc = asc


class _SPTD(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ushort),
        ("ScsiStatus", ctypes.c_ubyte),
        ("PathId", ctypes.c_ubyte),
        ("TargetId", ctypes.c_ubyte),
        ("Lun", ctypes.c_ubyte),
        ("CdbLength", ctypes.c_ubyte),
        ("SenseInfoLength", ctypes.c_ubyte),
        ("DataIn", ctypes.c_ubyte),
        ("DataTransferLength", ctypes.c_ulong),
        ("TimeOutValue", ctypes.c_ulong),
        ("DataBuffer", ctypes.c_void_p),
        ("SenseInfoOffset", ctypes.c_ulong),
        ("Cdb", ctypes.c_ubyte * 16),
    ]


class _SPTDWithSense(ctypes.Structure):
    _fields_ = [("sptd", _SPTD), ("Filler", ctypes.c_ulong), ("Sense", ctypes.c_ubyte * 32)]


IOCTL_SCSI_PASS_THROUGH_DIRECT = 0x4D014
GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
FILE_SHARE_READ, FILE_SHARE_WRITE = 1, 2
OPEN_EXISTING = 3
MEM_COMMIT_RESERVE, MEM_RELEASE, PAGE_READWRITE = 0x3000, 0x8000, 0x04
DRIVE_CDROM = 5

_k32 = ctypes.WinDLL("kernel32", use_last_error=True) if os.name == "nt" else None
if _k32:
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                 wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _k32.DeviceIoControl.restype = wintypes.BOOL
    _k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                     ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.VirtualAlloc.restype = ctypes.c_void_p
    _k32.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
    _k32.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD]
    _k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]

INVALID_HANDLE = wintypes.HANDLE(-1).value


def list_optical_drives():
    if not _k32:
        return []
    mask = _k32.GetLogicalDrives()
    return [f"{chr(65 + i)}:" for i in range(26)
            if mask & (1 << i) and _k32.GetDriveTypeW(f"{chr(65 + i)}:\\") == DRIVE_CDROM]


@dataclass
class Track:
    number: int
    lba: int
    is_data: bool
    end: int = 0  # first LBA after this track
    mode: str = "AUDIO"

    @property
    def length(self):
        return self.end - self.lba


class OpticalDrive:
    BUF_SIZE = 128 * 1024
    CHUNK_RAW = 26  # 26 * 2352 bytes stays below the common 64 KiB transfer limit
    CHUNK_DVD = 32

    def __init__(self, letter):
        self.letter = letter.rstrip("\\")
        self.handle = _k32.CreateFileW(f"\\\\.\\{self.letter}", GENERIC_READ | GENERIC_WRITE,
                                       FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None)
        if self.handle in (None, INVALID_HANDLE):
            raise ScsiError(f"Could not open drive {self.letter} (error {ctypes.get_last_error()})")
        # Page-aligned buffer satisfies any adapter alignment requirement
        self.buf = _k32.VirtualAlloc(None, self.BUF_SIZE, MEM_COMMIT_RESERVE, PAGE_READWRITE)

    def close(self):
        if self.buf:
            _k32.VirtualFree(self.buf, 0, MEM_RELEASE)
            self.buf = None
        if self.handle:
            _k32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _cmd(self, cdb, length, timeout=30):
        s = _SPTDWithSense()
        s.sptd.Length = ctypes.sizeof(_SPTD)
        s.sptd.CdbLength = len(cdb)
        s.sptd.SenseInfoLength = ctypes.sizeof(s.Sense)
        s.sptd.DataIn = 1  # SCSI_IOCTL_DATA_IN
        s.sptd.DataTransferLength = length
        s.sptd.TimeOutValue = timeout
        s.sptd.DataBuffer = self.buf
        s.sptd.SenseInfoOffset = _SPTDWithSense.Sense.offset
        for i, b in enumerate(cdb):
            s.sptd.Cdb[i] = b
        returned = wintypes.DWORD()
        ok = _k32.DeviceIoControl(self.handle, IOCTL_SCSI_PASS_THROUGH_DIRECT, ctypes.byref(s), ctypes.sizeof(s),
                                  ctypes.byref(s), ctypes.sizeof(s), ctypes.byref(returned), None)
        if not ok:
            raise ScsiError(f"DeviceIoControl failed (error {ctypes.get_last_error()})")
        if s.sptd.ScsiStatus != 0:
            key, asc = s.Sense[2] & 0x0F, s.Sense[12]
            if asc == 0x3A:
                raise ScsiError("No disc in the drive", key, asc)
            if key == 0x02:
                raise ScsiError("The drive is not ready (disc still loading?)", key, asc)
            raise ScsiError(f"SCSI error (sense {key:02X}/{asc:02X})", key, asc)
        return ctypes.string_at(self.buf, s.sptd.DataTransferLength)

    def inquiry(self):
        d = self._cmd(bytes([0x12, 0, 0, 0, 96, 0]), 96)
        return f"{d[8:16].decode('ascii', 'replace').strip()} {d[16:32].decode('ascii', 'replace').strip()}".strip()

    def is_dvd(self):
        """True if the loaded medium is a DVD (current profile 0x10-0x2F)."""
        d = self._cmd(bytes([0x46, 0x02, 0, 0, 0, 0, 0, 0, 8, 0]), 8)
        return 0x10 <= int.from_bytes(d[6:8], "big") < 0x40

    def capacity(self):
        d = self._cmd(bytes([0x25, 0, 0, 0, 0, 0, 0, 0, 0, 0]), 8)
        return int.from_bytes(d[0:4], "big") + 1

    def read_toc(self):
        d = self._cmd(bytes([0x43, 0, 0, 0, 0, 0, 0, 0x03, 0x24, 0]), 804)
        size = min(int.from_bytes(d[0:2], "big") + 2, len(d))
        tracks, leadout = [], None
        for pos in range(4, size - 7, 8):
            control, number = d[pos + 1] & 0x0F, d[pos + 2]
            lba = int.from_bytes(d[pos + 4:pos + 8], "big", signed=True)
            if number == 0xAA:
                leadout = lba
            elif 1 <= number <= 99:
                tracks.append(Track(number, lba, bool(control & 0x04)))
        if not tracks or leadout is None:
            raise ScsiError("Could not read the disc's table of contents (TOC)")
        for t, nxt in zip(tracks, tracks[1:] + [None]):
            t.end = nxt.lba if nxt else leadout
        return tracks

    def read_raw(self, lba, count):
        """READ CD: full 2352-byte sectors (sync, headers, user data, EDC/ECC)."""
        cdb = bytes([0xBE, 0x00, *lba.to_bytes(4, "big"), *count.to_bytes(3, "big"), 0xF8, 0x00, 0x00])
        return self._cmd(cdb, count * RAW_SECTOR, timeout=60)

    def read_user(self, lba, count):
        """READ(10): 2048-byte user data sectors."""
        cdb = bytes([0x28, 0, *lba.to_bytes(4, "big"), 0, *count.to_bytes(2, "big"), 0])
        return self._cmd(cdb, count * DVD_SECTOR, timeout=60)


@dataclass
class DiscInfo:
    drive: str
    model: str
    is_dvd: bool
    tracks: list
    sectors: int
    serial: str = None
    volume: str = None

    @property
    def size(self):
        return self.sectors * (DVD_SECTOR if self.is_dvd else RAW_SECTOR)

    @property
    def summary(self):
        if self.is_dvd:
            return "DVD (PS2)"
        data = sum(t.is_data for t in self.tracks)
        audio = len(self.tracks) - data
        kind = "PlayStation CD" if self.serial else "CD"
        return f"{kind} · {data} data track{'s' if data != 1 else ''}" + (f" + {audio} audio track{'s' if audio != 1 else ''}" if audio else "")


def read_disc_info(letter):
    with OpticalDrive(letter) as drive:
        model = drive.inquiry()
        try:
            dvd = drive.is_dvd()
        except ScsiError as e:
            if e.asc == 0x3A or e.sense_key == 0x02:
                raise
            dvd = False
        if dvd:
            sectors = drive.capacity()
            tracks = [Track(1, 0, True, sectors, "DVD")]
            read_sector = lambda lba: drive.read_user(lba, 1)
        else:
            tracks = drive.read_toc()
            sectors = tracks[-1].end
            for t in tracks:
                if t.is_data:
                    head = drive.read_raw(t.lba, 1)
                    t.mode = "MODE1/2352" if head[15] == 1 else "MODE2/2352"
            first = tracks[0]
            offset = CUE_MODES.get(first.mode, (0, 24))[1]
            read_sector = lambda lba: drive.read_raw(first.lba + lba, 1)[offset:offset + 2048]
        serial, volume = None, None
        if tracks[0].is_data:
            try:
                serial, volume = _iso_serial(read_sector)
            except ScsiError:
                pass
        return DiscInfo(letter, model, dvd, tracks, sectors, serial, volume)


def _msf(frames):
    return f"{frames // 4500:02d}:{frames // 75 % 60:02d}:{frames % 75:02d}"


def write_cue(cue_path, bin_name, tracks):
    lines = [f'FILE "{bin_name}" BINARY']
    for t in tracks:
        lines.append(f"  TRACK {t.number:02d} {t.mode if t.is_data else 'AUDIO'}")
        lines.append(f"    INDEX 01 {_msf(t.lba)}")
    Path(cue_path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def rip_disc(info, out_base, progress, cancelled):
    """Rip a disc to out_base.bin/.cue (CD) or out_base.iso (DVD).

    progress(done_sectors, total_sectors, bytes_per_second) is called periodically,
    cancelled() is polled between reads. Returns (image_path, unreadable_sectors).
    """
    out_base = Path(out_base)
    dvd = info.is_dvd
    image = out_base.with_suffix(".iso" if dvd else ".bin")
    sector_size = DVD_SECTOR if dvd else RAW_SECTOR
    chunk = OpticalDrive.CHUNK_DVD if dvd else OpticalDrive.CHUNK_RAW
    total = info.sectors
    bad = 0
    start = time.monotonic()
    last_report = 0.0

    with OpticalDrive(info.drive) as drive, open(image, "wb") as out:
        read = drive.read_user if dvd else drive.read_raw

        def read_retry(lba, count, tries):
            for attempt in range(tries):
                try:
                    return read(lba, count)
                except ScsiError:
                    if attempt == tries - 1:
                        raise
                    time.sleep(0.2)

        lba = 0
        while lba < total:
            if cancelled():
                raise InterruptedError
            count = min(chunk, total - lba)
            try:
                out.write(read_retry(lba, count, 3))
            except ScsiError:
                # Fall back to single sectors so one bad spot doesn't lose a whole chunk
                for s in range(lba, lba + count):
                    try:
                        out.write(read_retry(s, 1, 4))
                    except ScsiError:
                        out.write(b"\0" * sector_size)
                        bad += 1
            lba += count
            now = time.monotonic()
            if now - last_report > 0.25 or lba >= total:
                last_report = now
                progress(lba, total, lba * sector_size / max(now - start, 1e-6))

    if not dvd:
        write_cue(out_base.with_suffix(".cue"), image.name, info.tracks)
        return out_base.with_suffix(".cue"), bad
    return image, bad
