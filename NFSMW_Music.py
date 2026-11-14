#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NFSMW Custom Music Manager — logic file. Keep NFSMW_Music_data.py beside it.

The patch (ASI loader, native ASI bridge, EA-XA encoder, playlist GUI and
launcher) is stored in the separate NFSMW_Music_data.py. The compact audio converter (FFmpeg) is stored in compressed form and is
unpacked into a local temp folder when needed. No network download is needed.

The patch is NOT auto-installed on first run — the default state is «не
установлено», and «Установить» / «Удалить патч» reflect and change that state.
A clean game needs no third-party ASI loader: the embedded dinput8.dll proxy is
installed with the patch, and uninstall removes only that own proxy. Stock EA
TRAX tracks are listed with their real names in game order and can be disabled
so they disappear from the in-game list entirely.

In the playlist: click the «Статус ▾» cell for an in-place dropdown, click an
artist/title cell (own tracks) to rename it inline, and double-click a row to
open the file in your default music player.

Native EA TRAX bridge derived from MW-EA-TRAX-Expansion 0.5.0 (MIT, Zakkey250).
All audio conversion happens here, before speed.exe starts. The ASI only reads
the prepared cache; it never launches a converter and never closes the game.

Drop the compiled .exe into the game folder (next to speed.exe) and it finds the
game by itself. Track names are always kept as written (Cyrillic included);
volume is left to the in-game slider.

Requires: Windows, Python 3.10+, Tkinter (standard on python.org installers).
Usage:
    python NFSMW_Music.py                 # GUI
    python NFSMW_Music.py --launch        # sync + build + start game
    python NFSMW_Music.py --build         # sync + build only
    python NFSMW_Music.py --uninstall     # remove the patch; keep CustomMusic
    python NFSMW_Music.py --game "D:\\...\\NFS Most Wanted" --launch

Compile to a single .exe (run on Windows, in the folder with this file):
    pip install pyinstaller
    pyinstaller --onefile --noconsole --name NFSMW_Music NFSMW_Music.py
The resulting dist/NFSMW_Music.exe carries the patch and compact FFmpeg.
FFmpeg is unpacked locally into temp when first needed.
"""
from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import math
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import wave
import zipfile
from pathlib import Path

# Ресурсы находятся в соседнем файле. Декодеры и игровой патч остаются
# локальными: интернет для работы не требуется.
try:
    from NFSMW_Music_data import (EMBEDDED_PAYLOAD_B64, EMBEDDED_LOGO_B64,
                                  EMBEDDED_BACKGROUND_B64, EMBEDDED_FFMPEG_XZ_B64,
                                  _VGMSTREAM_ZIP_B64)
except ModuleNotFoundError as exc:
    if exc.name != "NFSMW_Music_data":
        raise
    message = ("Не найден файл ресурсов NFSMW_Music_data.py. "
               "Поместите его рядом с NFSMW_Music.py и запустите программу снова.")
    if __name__ == "__main__":
        try:
            import tkinter as _tk
            from tkinter import messagebox as _messagebox
            _root = _tk.Tk()
            _root.withdraw()
            _messagebox.showerror("NFSMW Music — нет файла ресурсов", message, parent=_root)
            _root.destroy()
        except Exception:
            print(message, file=sys.stderr)
    raise SystemExit(message) from exc

APP = "NFSMW Custom Music Manager"
VERSION = "9.6"
MAX_CUSTOM_TRACKS = 120
MAX_TOTAL_TRACKS = 120
MAX_TRACKS = MAX_CUSTOM_TRACKS  # backward-compatible alias
STOCK_TRACK_COUNT = 26
# Reference EA TRAX playlist (game order). Used until the game writes real names.
STOCK_REFERENCE = [
    ("Styles of Beyond", "Nine Thou (Superstars Remix)"),
    ("T.I. Presents the P$C", "Do Ya Thang"),
    ("Rock", "I Am Rock"),
    ("Suni Clay", "In A Hood Near You"),
    ("The Perceptionists", "Let's Move"),
    ("Juvenile", "Sets Go Up"),
    ("Hush", "Fired Up"),
    ("DJ Spooky and Dave Lombardo", "B-Side Wins Again (feat. Chuck D)"),
    ("Celldweller", "Shapeshifter (feat. Styles of Beyond)"),
    ("Lupe Fiasco", "Tilted"),
    ("Ils", "Feed the Addiction"),
    ("Celldweller", "One Good Reason"),
    ("Hyper", "We Control"),
    ("Static-X", "Skinnyman"),
    ("Dieselboy + Kaos", "Barrier Break"),
    ("Disturbed", "Decadence"),
    ("The Prodigy", "You'll Be Under My Wheels"),
    ("The Roots and BT", "Tao of the Machine (Scott Humphrey's Remix)"),
    ("Stratus", "You Must Follow (Evol Intent VIP)"),
    ("Mastodon", "Blood and Thunder"),
    ("Evol Intent, Mayhem & Thinktank", "Broken Sword"),
    ("Bullet for My Valentine", "Hand of Blood"),
    ("Paul Linford and Chris Vrenna", "The Mann"),
    ("Avenged Sevenfold", "Blinded in Chains"),
    ("Jamiroquai", "Feels Just Like It Should (Timo Maas Remix)"),
    ("Paul Linford and Chris Vrenna", "Most Wanted Mash Up"),
]
AUDIO_EXTS = {".mp3", ".wav", ".m4a"}
# Payload и изображения импортируются из соседнего NFSMW_Music_data.py.
# Branding, also filled by the build step: the NFS MW logo (white, alpha) shown in
# the header and the car photo used as the window background. Base64 PNG literals.
# Minimal FFmpeg is embedded compressed; it is unpacked locally, never downloaded.
EMBEDDED_FFMPEG_SHA256 = "180249706715372cff68bc928ca8520e8d504199ee33832cc2413ccf7467126e"
MOD_NAME = "NFSMWCustomMusic"
ASI_NAME = MOD_NAME + ".asi"
OLD_ASI = "NFSMWEATraxExpansion.asi"
LOADER_NAME = "dinput8.dll"
LOADER_TAG = b"NFSMWCustomMusic.AsiLoader/"
KNOWN_LOADERS = ("dinput8.dll", "dsound.dll", "d3d9.dll", "winmm.dll", "version.dll")
LEGACY_ALBUM = "NFSMW Custom Music"
STOCK_LAYOUT = (3681, 70, 3257)
MAX_BANK_BYTES = 0x7FFFFF80
CACHE_VERSION = 1

# 29 hook guards from MW-EA-TRAX-Expansion RuntimeHooks.cpp (MIT).
GUARDS = [
    (0x00688230, bytes.fromhex("8b4154c3")),
    (0x004AE340, bytes.fromhex("51568d442404508bf1")),
    (0x0065FAF0, bytes.fromhex("56578bf9518b0f")),
    (0x005CC240, bytes.fromhex("8b442404")),
    (0x00409400, bytes.fromhex("d9411cc3")),
    (0x00433C90, bytes.fromhex("8b4120c3")),
    (0x00433B60, bytes.fromhex("8b8118020000c3")),
    (0x006881A0, bytes.fromhex("d94178c3")),
    (0x006880B0, bytes.fromhex("8b8194000000c3")),
    (0x005D59F0, bytes.fromhex("83ec08568b7108")),
    (0x004B6310, bytes.fromhex("a1e8219100")),
    (0x004DFB80, bytes.fromhex("a1fc868f0085c0")),
    (0x0071B1AE, bytes.fromhex("8b8f3001000085c9")),
    (0x004DF2D0, bytes.fromhex("568bf1680000000f")),
    (0x0081D5F2, bytes.fromhex("558bec51803d88219c0000")),
    (0x0081BCCB, bytes.fromhex("ff15e8bc9000")),
    (0x0081BCD9, bytes.fromhex("802d89219c0001")),
    (0x005AB450, bytes.fromhex("6aff68773d8700")),
    (0x004DF330, bytes.fromhex("a190cf91008b4810")),
    (0x004F4CD0, bytes.fromhex("53568bf18b8650010000")),
    (0x004F6C80, bytes.fromhex("55568bf18b8614010000")),
    (0x0082BFD0, bytes.fromhex("558beca164299c00")),
    (0x0082B4A0, bytes.fromhex("558bec568bf1")),
    (0x0082B5A0, bytes.fromhex("56578bf1")),
    (0x0082A580, bytes.fromhex("558bec568bf1")),
    (0x0082B460, bytes.fromhex("558bec8b4508")),
    (0x0082B290, bytes.fromhex("558bec538b5d08")),
    (0x004B24F0, bytes.fromhex("568bf1e8d3973600")),
    (0x004B2510, bytes.fromhex("568bf1e8b3973600")),
]


def log_default(msg):
    print(msg, flush=True)


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest().upper()


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0


def file_stamp(path):
    """Return (size, Windows FILETIME) for the native bridge manifest."""
    st = Path(path).stat()
    ft = st.st_mtime_ns // 100 + 116444736000000000
    return st.st_size, ft


def clean_text(value):
    value = str(value or "").replace("\x00", "").replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s+", " ", value).strip()[:512]


CYR = {
    "А": "A", "Б": "B", "В": "V", "Г": "G", "Д": "D", "Е": "E", "Ё": "Yo",
    "Ж": "Zh", "З": "Z", "И": "I", "Й": "Y", "К": "K", "Л": "L", "М": "M",
    "Н": "N", "О": "O", "П": "P", "Р": "R", "С": "S", "Т": "T", "У": "U",
    "Ф": "F", "Х": "Kh", "Ц": "Ts", "Ч": "Ch", "Ш": "Sh", "Щ": "Shch",
    "Ъ": "", "Ы": "Y", "Ь": "", "Э": "E", "Ю": "Yu", "Я": "Ya",
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    "Ґ": "G", "ґ": "g", "Є": "Ye", "є": "ye", "І": "I", "і": "i",
    "Ї": "Yi", "ї": "yi", "Ў": "U", "ў": "u",
}


def transliterate(value, fallback=""):
    value = clean_text(value)
    out = []
    for ch in value:
        if ord(ch) < 128:
            out.append(ch)
        elif ch in CYR:
            out.append(CYR[ch])
        else:
            d = unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode()
            out.append(d if d else ch)
    result = clean_text("".join(out))
    return (result or clean_text(fallback))[:95]


def parse_filename(stem):
    stem = clean_text(stem)
    for sep in (" - ", " – ", " — ", " ― ", " ‒ "):
        if sep in stem:
            left, right = stem.split(sep, 1)
            if left.strip() and right.strip():
                return clean_text(left), clean_text(right)
    if "-" in stem:
        left, right = stem.split("-", 1)
        if left.strip() and right.strip():
            return clean_text(left), clean_text(right)
    return "Unknown Artist", stem


def _syncsafe(b):
    return ((b[0] & 0x7F) << 21) | ((b[1] & 0x7F) << 14) | ((b[2] & 0x7F) << 7) | (b[3] & 0x7F)


def _decode_text_frame(payload, encoding):
    if not payload:
        return ""
    try:
        if encoding == 0:
            return clean_text(payload.decode("latin-1"))
        if encoding == 1:
            return clean_text(payload.decode("utf-16"))
        if encoding == 2:
            return clean_text(payload.decode("utf-16-be"))
        if encoding == 3:
            return clean_text(payload.decode("utf-8"))
    except Exception:
        return ""
    return ""


def read_id3(path):
    result = {"title": "", "artist": "", "album": "", "source": ""}
    try:
        data = Path(path).read_bytes()
    except OSError:
        return result
    if data[:3] == b"ID3" and len(data) >= 10:
        ver, flags = data[3], data[5]
        size = _syncsafe(data[6:10])
        body = data[10:10 + size]
        if flags & 0x80:
            body = body.replace(b"\xff\x00", b"\xff")
        pos = 0
        if flags & 0x40 and len(body) >= 4:
            ext = _syncsafe(body[:4]) if ver == 4 else struct.unpack(">I", body[:4])[0]
            pos = 4 + ext
        while pos + (6 if ver == 2 else 10) <= len(body):
            if ver == 2:
                fid = body[pos:pos + 3]
                fsize = int.from_bytes(body[pos + 3:pos + 6], "big")
                hdr = 6
            else:
                fid = body[pos:pos + 4]
                fsize = _syncsafe(body[pos + 4:pos + 8]) if ver == 4 else int.from_bytes(body[pos + 4:pos + 8], "big")
                hdr = 10
            if not fid or fid[0:1] == b"\x00":
                break
            payload = body[pos + hdr:pos + hdr + fsize]
            key = {b"TIT2": "title", b"TPE1": "artist", b"TALB": "album",
                   b"TT2": "title", b"TP1": "artist", b"TAL": "album"}.get(fid)
            if key and payload:
                value = _decode_text_frame(payload[1:], payload[0])
                if value:
                    result[key] = value
            pos += hdr + fsize
        if result["title"] or result["artist"] or result["album"]:
            result["source"] = "ID3v2"
            return result
    if len(data) >= 128 and data[-128:-125] == b"TAG":
        tag = data[-128:]

        def field(a, b):
            raw = tag[a:b].split(b"\x00")[0]
            for enc in ("utf-8", "cp1251", "latin-1"):
                try:
                    return clean_text(raw.decode(enc))
                except Exception:
                    pass
            return clean_text(raw.decode("latin-1", errors="replace"))

        result["title"], result["artist"], result["album"] = field(3, 33), field(33, 63), field(63, 93)
        if result["title"] or result["artist"] or result["album"]:
            result["source"] = "ID3v1"
    return result


MP3_BITRATES = {
    3: {1: [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
        2: [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384],
        3: [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448]},
    2: {1: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
        2: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
        3: [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256]},
}
MP3_SAMPLE_RATES = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


def mp3_duration(path):
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    pos = 0
    if data[:3] == b"ID3" and len(data) >= 10:
        pos = 10 + _syncsafe(data[6:10]) + (10 if data[5] & 0x10 else 0)
    total, rate = 0, None
    while pos + 4 <= len(data):
        if data[pos] != 0xFF or (data[pos + 1] & 0xE0) != 0xE0:
            pos += 1
            continue
        h = int.from_bytes(data[pos:pos + 4], "big")
        verbits, layer = (h >> 19) & 3, (h >> 17) & 3
        bridx, sridx, pad = (h >> 12) & 0xF, (h >> 10) & 3, (h >> 9) & 1
        if verbits == 1 or layer == 0 or bridx in (0, 15) or sridx == 3:
            pos += 1
            continue
        ver = 3 if verbits == 3 else (2 if verbits == 2 else 0)
        try:
            br = MP3_BITRATES[ver][layer][bridx] * 1000
            sr = MP3_SAMPLE_RATES[ver][sridx]
        except Exception:
            pos += 1
            continue
        if layer == 3:
            samples, length = 384, (12 * br // sr + pad) * 4
        elif layer == 2:
            samples, length = 1152, 144 * br // sr + pad
        else:
            samples = 1152 if ver == 3 else 576
            length = (72 if ver != 3 else 144) * br // sr + pad
        if length <= 4:
            break
        total += samples
        rate = sr
        pos += length
    return total / rate if rate and total else None


def wav_duration(path):
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        return None


def ffmpeg_duration(path, ffmpeg):
    try:
        p = subprocess.run([str(ffmpeg), "-hide_banner", "-i", str(path)],
                           capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        m = re.search(rb"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", p.stderr)
        if m:
            return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    except Exception:
        pass
    return None


def duration_seconds(path, ffmpeg=None):
    ext = Path(path).suffix.lower()
    if ext == ".mp3":
        d = mp3_duration(path)
        if d:
            return d
    if ext == ".wav":
        d = wav_duration(path)
        if d:
            return d
    if ffmpeg:
        return ffmpeg_duration(path, ffmpeg)
    return None


def format_duration(seconds):
    if seconds is None:
        return "?:??"
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def parse_metadata(path, native_names=True):
    tags = read_id3(path) if Path(path).suffix.lower() == ".mp3" else {"title": "", "artist": "", "album": "", "source": ""}
    f_artist, f_title = parse_filename(Path(path).stem)
    artist = clean_text(tags.get("artist") or f_artist)
    title = clean_text(tags.get("title") or f_title)
    album = clean_text(tags.get("album") or "")
    if album == LEGACY_ALBUM:
        album = ""
    source = tags.get("source") or "filename"
    display_artist = artist if native_names else transliterate(artist, "Unknown Artist")
    display_title = title if native_names else transliterate(title, "Untitled")
    display_album = album if native_names else transliterate(album, "")
    return {"artist": artist, "title": title, "album": album,
            "display_artist": display_artist, "display_title": display_title,
            "display_album": display_album, "source": source}


# ---------------------------------------------------------------------------
# Pathfinder v5.1 bank serialization (derived from native_bank.py, MIT)
# ---------------------------------------------------------------------------
def u16(b, o): return struct.unpack_from("<H", b, o)[0]
def u32(b, o): return struct.unpack_from("<I", b, o)[0]
def p32(b, o, v): struct.pack_into("<I", b, o, v)

FIELDS = dict(nodes=20, nodedata=24, events=28, eventdata=32, vars=36,
              routers=40, tracks=44, trackdata=48, samples=52, eof=56)


def layout(b):
    if b[:6] != b"xDFP\x05\x01":
        raise ValueError("Unsupported Pathfinder bank magic")
    l = {k: u32(b, o) for k, o in FIELDS.items()}
    if l["eof"] != len(b):
        raise ValueError("Truncated Pathfinder bank")
    return l


def nodes(b):
    l = layout(b)
    offs = [u16(b, l["nodes"] + i * 2) * 4 for i in range(u16(b, 18))] + [l["events"]]
    return [bytearray(b[s:e]) for s, e in zip(offs, offs[1:])]


def events(b):
    l = layout(b)
    out = []
    for i in range(b[15]):
        off = u16(b, l["events"] + i * 2) * 4
        out.append(bytearray(b[off:off + 20 + b[off + 15] * 12]))
    return out


def samples(b):
    l = layout(b)
    return [struct.unpack_from("<II", b, o) for o in range(l["samples"], l["eof"], 8)]


def serialize(base, ns, es, ss):
    if len(es) > 255 or len(ns) > 32767 or len(ss) > 32767:
        raise ValueError("Native bank capacity exceeded")
    old = layout(base)
    out = bytearray(base[:72])
    loc = {}

    def table(key, count):
        loc[key] = len(out)
        out.extend(bytes((count * 2 + 3) & ~3))

    def records(key, tablekey, recs):
        loc[key] = len(out)
        for i, r in enumerate(recs):
            if len(out) // 4 > 65535 or len(r) % 4:
                raise ValueError("Native bank record limit")
            struct.pack_into("<H", out, loc[tablekey] + i * 2, len(out) // 4)
            out.extend(r)

    table("nodes", len(ns)); records("nodedata", "nodes", ns)
    table("events", len(es)); records("eventdata", "events", es)
    loc["vars"] = len(out)
    delta = loc["vars"] - old["vars"]
    out.extend(base[old["vars"]:old["samples"]])
    for k in ("routers", "tracks", "trackdata"):
        loc[k] = old[k] + delta
    for k, count in (("routers", base[16] + 1), ("tracks", base[13])):
        for i in range(count):
            o = loc[k] + 4 * i
            p32(out, o, u32(out, o) + delta // 4)
    loc["samples"] = len(out)
    for offset, duration in ss:
        out.extend(struct.pack("<II", offset, duration))
    loc["eof"] = len(out)
    for k, o in FIELDS.items():
        p32(out, o, loc[k])
    struct.pack_into("<H", out, 18, len(ns))
    out[15] = len(es)
    return out


def positive_patch(tag, value):
    if not 0 <= value <= 0x7FFFFFFF:
        raise ValueError("SCHl positive value out of range")
    width = max(1, (value.bit_length() + 1 + 7) // 8)
    return bytes([tag, width]) + value.to_bytes(width, "big")


def repair_frame_header(body):
    if body[:4] != b"PT\x00\x00":
        return body, None
    result = bytearray(body[:4])
    i, expected = 4, None
    while i < len(body):
        start = i
        tag = body[i]; i += 1
        if tag == 255:
            result.append(tag)
            result.extend(bytes((-len(result)) % 4))
            return (bytes(result), expected) if expected is not None else (body, None)
        if tag in (252, 253, 254):
            result.append(tag); continue
        if i >= len(body):
            raise ValueError("Truncated SCHl patch length")
        length = body[i]; i += 1
        if length == 255:
            if i + 4 > len(body):
                raise ValueError("Truncated SCHl extended length")
            length = int.from_bytes(body[i:i + 4], "big"); i += 4
        if i + length > len(body):
            raise ValueError("Truncated SCHl patch payload")
        value = body[i:i + length]; i += length
        if tag == 0x85 and 1 <= length < 4 and value[0] & 128:
            if expected is not None:
                raise ValueError("Duplicate SCHl frame count")
            expected = int.from_bytes(value, "big")
            result.extend(positive_patch(tag, expected))
        else:
            result.extend(body[start:i])
    raise ValueError("Missing SCHl header terminator")


def copy_stream(source, destination, offset):
    source.seek(offset)
    first, expected, frames = True, None, 0
    while True:
        header = source.read(8)
        if len(header) != 8:
            raise ValueError("Truncated cached stream")
        tag, size = struct.unpack("<4sI", header)
        if size < 8 or size > 1048576 or (first and tag != b"SCHl"):
            raise ValueError("Invalid cached stream")
        body = source.read(size - 8)
        if len(body) != size - 8:
            raise ValueError("Truncated cached block")
        if first:
            body, expected = repair_frame_header(body)
            header = struct.pack("<4sI", tag, 8 + len(body))
        elif tag == b"SCDl":
            if len(body) < 4:
                raise ValueError("Truncated cached frame count")
            frames += u32(body, 0)
        destination.write(header); destination.write(body)
        first = False
        if tag == b"SCEl":
            if expected is not None and frames != expected:
                raise ValueError("SCHl repair frame count does not match audio blocks")
            return


def mpf_counts(b):
    l = layout(b)
    return u16(b, 18), b[15], (l["eof"] - l["samples"]) // 8


# ---------------------------------------------------------------------------
# Sidecar / library
# ---------------------------------------------------------------------------
def sidecar_path(audio):
    return Path(audio).with_suffix(".ini")


def write_sidecar(audio, entry):
    lines = [
        "; Generated by NFSMW Custom Music Manager.",
        "[Track]",
        f"Title={entry.get('display_title', '')}",
        f"Artist={entry.get('display_artist', '')}",
        f"Album={entry.get('display_album', '')}",
        f"Mode={entry.get('mode', 'ALL')}",
        "",
        "[OriginalMetadata]",
        f"Title={entry.get('title', '')}",
        f"Artist={entry.get('artist', '')}",
        f"Album={entry.get('album', '')}",
        "",
    ]
    with open(sidecar_path(audio), "w", encoding="utf-16", newline="\r\n") as f:
        f.write("\n".join(lines))


def read_sidecar(audio):
    p = sidecar_path(audio)
    if not p.exists():
        return None
    for enc in ("utf-16", "utf-16-le", "utf-8-sig", "cp1251", "latin-1"):
        try:
            cp = configparser.ConfigParser(interpolation=None, strict=False)
            cp.optionxform = str
            with open(p, "r", encoding=enc) as f:
                cp.read_file(f)
            if cp.has_section("Track"):
                return {k.lower(): v for k, v in cp.items("Track")}
        except Exception:
            continue
    return None


def load_library(path):
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict) and isinstance(data.get("tracks"), list):
                return data
        except Exception:
            pass
    return {"format": 2, "tracks": []}


def save_library(path, data):
    data["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    data["track_count"] = len(data.get("tracks", []))
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# The event traversal and EA-XA layout below are adapted from the user's
# earlier Pytho2n.py. The game banks are opened read-only.
_STOCK_ATTR_TO_EVENT = (15,17,8,13,2,16,1,3,14,5,21,7,10,11,22,12,6,20,24,18,23,4,26,9,19,25)
_STOCK_EVENT_IDS = (0x020AD947,0x0259C841,0x02F6C15D,0x02E10499,0x025C0092,0x0226ADA3,0x02AB73E9,0x025254DA,0x02CAE6A5,0x023618D6,0x025C9F2E,0x028CBA15,0x029DAB22,0x028B365E,0x02EAF18B,0x020B97E1,0x02121590,0x02211DD5,0x02F1B18D,0x02540E5C,0x02BA79C9,0x02715C30,0x02E686EA,0x02BEA71D,0x0206A402,0x020FF61F)


def _canonical_stock_event(value, index):
    # Game export may use another namespace byte (e.g. 01 instead of 02).
    # Match only a known event's 24-bit key; never let empty/unknown exports
    # turn a previously playable stock song into an extraction error.
    try:
        v = int(str(value).strip(), 16)
        hits = [x for x in _STOCK_EVENT_IDS if (x & 0xFFFFFF) == (v & 0xFFFFFF)]
        if len(hits) == 1:
            return hits[0]
    except (TypeError, ValueError):
        pass
    if not isinstance(index, int) or not 0 <= index < len(_STOCK_ATTR_TO_EVENT):
        raise ValueError('Неверный номер оригинального трека.')
    return _STOCK_EVENT_IDS[_STOCK_ATTR_TO_EVENT[index]-1]


def _original_identity(track):
    return f"{_canonical_stock_event(track.get('event'), track['index']):08X}"


# Curated 1-based vgmstream subsong ranges for the unmodified PC retail bank.
# Source: independent PC rip's Song 1..26 TXTP playlists, cross-checked against
# the original MPF event roots and audio against named reference recordings.
# A range is meaningful only for the verified bank, never for a modified MPF.
_ORIGINAL_MPF_SHA256 = '15c7fdaa626940319a74965d4d68db1507475ba743d574c5aa6de214482e785b'
_ORIGINAL_MUS_SHA256 = 'bb191d34c4c3ac3b5bd33a9e02049a08df13014e4fb5c8d7d07320abae1c5811'
_ORIGINAL_PAIRS = {_ORIGINAL_MPF_SHA256: _ORIGINAL_MUS_SHA256,
 '6db1c144753c05b451a8cbcb3ed8d5dcf16132e8d7b77970302100bcc2e69998': 'ec3dc118582d33f6d71a2f78adf1d6739e16929ac1451f467bdead9c8ca64647'}
_ORIGINAL_RANGES = ((1316,1379),(1381,1417),(1419,1522),(1524,1597),
 (1599,1676),(1678,1787),(1789,1942),(1951,2000),(2003,2130),
 (2135,2203),(2206,2253),(2256,2293),(2295,2348),(2351,2431),
 (2433,2479),(2482,2533),(2534,2618),(2620,2667),(2668,2711),
 (2714,2767),(2769,2833),(2836,2916),(2922,3047),(3049,3129),
 (3131,3184),(3185,3239))
_EVENT_TO_RANGE = (1,2,3,4,5,6,7,8,9,11,12,10,13,15,16,17,14,18,19,20,21,22,23,24,25,26)
_ORIGINAL_VERIFIED_MUS = {}


def _original_manifest(music):
    path = Path(music) / 'Original' / '.original-cache-v3.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('version') == 3 and isinstance(data.get('tracks'), dict):
            return data['tracks']
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return {}


def original_cache_path(music, track):
    folder = Path(music) / 'Original'
    entry = _original_manifest(music).get(_original_identity(track), {})
    if isinstance(entry, dict):
        name = entry.get('file', '')
        if isinstance(name, str) and Path(name).name == name and name.lower().endswith('.wav'):
            return folder / name
    name = f"{track.get('artist', '')} — {track.get('title', '')}".strip(' .')
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', name).rstrip(' .')[:180] or f"Трек {track['index']+1}"
    return folder / (name + '.wav')


def original_cache_valid(music, track):
    entry = _original_manifest(music).get(_original_identity(track), {})
    if not isinstance(entry, dict): return False
    name = entry.get('file', '')
    if not isinstance(name, str) or Path(name).name != name or not name.lower().endswith('.wav'): return False
    try:
        path = Path(music) / 'Original' / name
        return path.is_file() and path.stat().st_size == entry.get('size') and entry.get('size',0) > 44
    except OSError:
        return False


def _original_destination(music, track):
    path = original_cache_path(music,track)
    if not path.exists(): return path
    # Files created by prior versions are known from their manifest and may be
    # atomically replaced. Preserve unrelated user files with the same name.
    try:
        old = json.loads((path.parent / '.original-cache.json').read_text(encoding='utf-8'))
        if isinstance(old, dict) and path.name in old.values(): return path
    except (OSError,ValueError): pass
    current = _original_manifest(music).get(_original_identity(track), {})
    if isinstance(current, dict) and current.get('file') == path.name: return path
    for i in range(2,10000):
        candidate = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not candidate.exists(): return candidate
    raise OSError('Не удалось подобрать имя файла оригинального трека.')


def original_stream_offsets(mpf, track):
    if hashlib.sha256(mpf).hexdigest() not in _ORIGINAL_PAIRS:
        raise ValueError('MPF отличается от проверенного оригинала PC-версии. Смешивать фрагменты из изменённого архива небезопасно.')
    event_id = int(_original_identity(track),16)
    try:
        event_slot = _STOCK_EVENT_IDS.index(event_id)
    except ValueError:
        raise ValueError('Этот оригинальный трек отсутствует в проверенной карте MPF.')
    start,end = _ORIGINAL_RANGES[_EVENT_TO_RANGE[event_slot]-1]
    ss = samples(mpf)
    if start < 1 or end > len(ss) or start > end:
        raise ValueError('Повреждена карта фрагментов MPF.')
    return [ss[i-1][0]*128 for i in range(start,end+1)]


def _verify_original_mus(path, cancel, expected):
    st = path.stat()
    key = (str(path.resolve()),st.st_size,st.st_mtime_ns,expected)
    if _ORIGINAL_VERIFIED_MUS.get(key): return
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            if cancel.is_set(): raise BuildCancelled('Остановлено')
            chunk=f.read(1024*1024)
            if not chunk: break
            h.update(chunk)
    if h.hexdigest() != expected:
        raise ValueError('MUS отличается от проверенного оригинала PC-версии; извлечение остановлено, чтобы не перепутать фрагменты.')
    _ORIGINAL_VERIFIED_MUS.clear()
    _ORIGINAL_VERIFIED_MUS[key] = True


# vgmstream r2168 (ISC license); included in this file for offline decoding.
# Copyright 2008-2025 Adam Gashlin and vgmstream contributors.
# Source and full license: https://github.com/vgmstream/vgmstream
_VGM_LOCK = threading.Lock()


def _ensure_vgmstream(cancel):
    import base64, io
    while not _VGM_LOCK.acquire(timeout=0.2):
        if cancel.is_set(): raise BuildCancelled('Остановлено')
    try:
        directory = temp_dir() / 'vgmstream-r2168'
        directory.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(base64.b85decode(_VGMSTREAM_ZIP_B64))) as archive:
            for item in archive.infolist():
                if cancel.is_set(): raise BuildCancelled('Остановлено')
                expected = archive.read(item)
                target = directory / item.filename
                if target.is_file() and target.stat().st_size == len(expected) and sha256_file(target) == hashlib.sha256(expected).hexdigest():
                    continue
                fd, path = tempfile.mkstemp(prefix='.vgm_', dir=directory)
                try:
                    with os.fdopen(fd,'wb') as f: f.write(expected)
                    os.replace(path,target)
                finally:
                    if os.path.exists(path): os.unlink(path)
        return directory/'vgmstream-cli.exe'
    finally:
        _VGM_LOCK.release()


_ORIGINAL_EXTRACT_LOCK = threading.Lock()


def extract_original_track(game, music, track, cancel, background=False):
    while not _ORIGINAL_EXTRACT_LOCK.acquire(timeout=0.2):
        if cancel.is_set(): raise BuildCancelled('Остановлено')
    try:
        if cancel.is_set(): raise BuildCancelled('Остановлено')
        if original_cache_valid(music, track):
            return original_cache_path(music,track)
        dest = _extract_original_locked(game, music, track, cancel, background)
        manifest = dest.parent / '.original-cache-v3.json'
        mapping = _original_manifest(music)
        mapping[_original_identity(track)] = {'file':dest.name, 'size':dest.stat().st_size}
        fd, name = tempfile.mkstemp(prefix='.map_', suffix='.tmp', dir=dest.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump({'version':3,'tracks':mapping}, f, ensure_ascii=False, indent=2)
            os.replace(name, manifest)
        finally:
            if os.path.exists(name): os.unlink(name)
        return dest
    finally:
        _ORIGINAL_EXTRACT_LOCK.release()


def _extract_original_locked(game, music, track, cancel, background=False):
    bank = Path(game) / 'SOUND' / 'PFDATA'
    candidates = [(bank/'MW_Music.mpf', bank/'MW_Music.mus'),
                  (bank/'MW_Music.mpf.original_bak', bank/'MW_Music.mus.original_bak'),
                  (bank/'MW_Music.mpf', bank/'MW_Music.mus.original_bak'),
                  (bank/'MW_Music.mpf.original_bak', bank/'MW_Music.mus')]
    errors = []
    for mpf_path,mus_path in candidates:
        if not mpf_path.is_file() or not mus_path.is_file(): continue
        try:
            mpf_data = mpf_path.read_bytes()
            offsets = original_stream_offsets(mpf_data,track)
            _verify_original_mus(mus_path, cancel, _ORIGINAL_PAIRS[hashlib.sha256(mpf_data).hexdigest()])
            break
        except (ValueError, OSError, struct.error) as e:
            errors.append(str(e))
    else:
        if errors:
            detail = '; '.join(dict.fromkeys(errors))
        else:
            present_mpf = any(x.is_file() for x, _ in candidates)
            present_mus = any(y.is_file() for _, y in candidates)
            missing = []
            if not present_mpf: missing.append('MW_Music.mpf')
            if not present_mus: missing.append('MW_Music.mus')
            detail = ('Не найдены: ' + ', '.join(missing) + '. Папка поиска: ' + str(bank)) if missing else 'Нет читаемой пары файлов.'
        raise ValueError('Не удалось открыть оригинальную музыку. ' + detail)
    vgm = _ensure_vgmstream(cancel)
    dest = _original_destination(music,track)
    dest.parent.mkdir(parents=True,exist_ok=True)
    fd, tmpname = tempfile.mkstemp(prefix='.original_',suffix='.wav',dir=dest.parent)
    os.close(fd); tmp = Path(tmpname)
    try:
        # Limit the number of open streams, even for tracks with many fragments.
        with tempfile.TemporaryDirectory(prefix='nfsmw_stock_') as workdir, open(mus_path,'rb') as source, wave.open(str(tmp),'wb') as result:
            work = Path(workdir)
            file_size = mus_path.stat().st_size
            fmt = None
            for base in range(0,len(offsets),48):
                names = []
                for j,offset in enumerate(offsets[base:base+48]):
                    if cancel.is_set(): raise BuildCancelled('Остановлено')
                    if offset < 0 or offset+8 > file_size: raise ValueError('Неверное смещение в MUS.')
                    source.seek(offset)
                    name = f'{j:03d}.asf'
                    names.append(name)
                    with open(work/name,'wb') as fragment:
                        first=True
                        for _ in range(100000):
                            if cancel.is_set(): raise BuildCancelled('Остановлено')
                            header=source.read(8)
                            if len(header)!=8: raise ValueError('Оборванный фрагмент MUS.')
                            kind,size=struct.unpack('<4sI',header)
                            if size<8 or size>16*1024*1024 or source.tell()+size-8>file_size:
                                raise ValueError('Повреждённый блок MUS.')
                            if first and kind != b'SCHl': raise ValueError('По указанному MPF смещению нет заголовка SCHl.')
                            first=False
                            fragment.write(header)
                            remaining=size-8
                            while remaining:
                                if cancel.is_set(): raise BuildCancelled('Остановлено')
                                data=source.read(min(remaining,1024*1024))
                                if not data: raise ValueError('Оборванный блок MUS.')
                                fragment.write(data); remaining-=len(data)
                            if kind==b'SCEl': break
                        else: raise ValueError('Не найден конец фрагмента MUS.')
                playlist = work/'group.txtp'
                playlist.write_text(''.join(n+'\n' for n in names), encoding='utf-8')
                group_wav = work/'group.wav'
                run_tool([vgm, '-i', '-o',group_wav,playlist], timeout=900,cancel=cancel, low_priority=background)
                with wave.open(str(group_wav),'rb') as decoded:
                    current=(decoded.getnchannels(),decoded.getsampwidth(),decoded.getframerate())
                    if decoded.getnframes() == 0 or current[1] != 2:
                        raise ValueError('Декодер не вернул PCM-звук.')
                    if fmt is None:
                        fmt=current; result.setnchannels(fmt[0]); result.setsampwidth(fmt[1]); result.setframerate(fmt[2])
                    elif current != fmt:
                        raise ValueError('Фрагменты трека имеют разный формат звука.')
                    while True:
                        if cancel.is_set(): raise BuildCancelled('Остановлено')
                        pcm=decoded.readframes(65536)
                        if not pcm: break
                        result.writeframesraw(pcm)
                group_wav.unlink(missing_ok=True)
                for name in names: (work/name).unlink(missing_ok=True)
        if cancel.is_set(): raise BuildCancelled('Остановлено')
        os.replace(tmp,dest)
        return dest
    finally:
        tmp.unlink(missing_ok=True)


def scan_audio(music):
    out = []
    for p in sorted(Path(music).iterdir(), key=lambda x: str(x).lower()):
        if not p.is_file() or p.suffix.lower() not in AUDIO_EXTS or p.name.startswith("."):
            continue
        rel = p.relative_to(music)
        if (rel.parts[0].casefold() == "original" or
                any(part.startswith(("_", ".")) for part in rel.parts[:-1])):
            continue
        out.append(p)
    return out


def stable_key(rel):
    return "external:" + rel.replace("\\", "/").lower()


def read_state_modes(state_path):
    modes = {}
    if not Path(state_path).exists():
        return modes
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str
    for enc in ("utf-16", "utf-8-sig", "cp1251", "latin-1"):
        try:
            cp.read(state_path, encoding=enc)
            break
        except Exception:
            continue
    for sec in cp.sections():
        if cp.has_option(sec, "Mode"):
            modes[sec] = cp.get(sec, "Mode").strip().upper()
    return modes


def stock_paths(game):
    mod = Path(game) / "scripts" / MOD_NAME
    return mod / "StockTracks.ini", mod / "StockModes.ini"


def _read_ini_any(path):
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str
    if not Path(path).exists():
        return cp
    for enc in ("utf-16", "utf-8-sig", "cp1251", "latin-1"):
        try:
            cp.read(path, encoding=enc)
            return cp
        except Exception:
            continue
    return cp


def read_stock_catalog(game):
    """Return 26 stock track dicts. Names appear after the first game launch."""
    tracks_ini, modes_ini = stock_paths(game)
    cat = _read_ini_any(tracks_ini)
    modes = _read_ini_any(modes_ini)
    out = []
    for i in range(STOCK_TRACK_COUNT):
        sec = f"Stock_{i:02d}"
        mode = "ALL"
        if modes.has_option(sec, "Mode"):
            mode = modes.get(sec, "Mode").strip().upper()
        elif cat.has_option(sec, "Mode"):
            mode = cat.get(sec, "Mode").strip().upper()
        exported_event = cat.get(sec, "EventID", fallback="").strip()
        canonical = _canonical_stock_event(exported_event, i)
        # Use the verified name tied to the event, not unreliable or empty
        # game-exported fields. The fallback is the known stock game order.
        slot = _STOCK_EVENT_IDS.index(canonical) + 1
        ref_index = _STOCK_ATTR_TO_EVENT.index(slot)
        ref_artist, ref_title = STOCK_REFERENCE[ref_index]
        named = bool(exported_event)
        out.append({
            "index": i,
            "title": ref_title,
            "artist": ref_artist,
            "album": cat.get(sec, "Album", fallback="EA TRAX").strip() or "EA TRAX",
            "event": f"{canonical:08X}",
            "mode": mode if mode in ("ALL", "FE", "IG", "OFF") else "ALL",
            "named": named,
        })
    return out


def write_stock_modes(game, tracks):
    _, modes_ini = stock_paths(game)
    modes_ini.parent.mkdir(parents=True, exist_ok=True)
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str
    for t in tracks:
        cp[f"Stock_{t['index']:02d}"] = {"Mode": t.get("mode", "ALL")}
    with open(modes_ini, "w", encoding="utf-16", newline="\r\n") as f:
        cp.write(f)


def stock_enabled_count(game):
    return sum(1 for t in read_stock_catalog(game) if t.get("mode", "ALL") != "OFF")


def sync_library(game, music, settings, log=log_default):
    music = Path(music)
    music.mkdir(parents=True, exist_ok=True)
    lib_path = music / "MusicLibrary.json"
    lib = load_library(lib_path)
    old = {e.get("file"): e for e in lib.get("tracks", []) if isinstance(e, dict) and e.get("file")}
    ffmpeg = ffmpeg_path(game)
    state_modes = read_state_modes(Path(game) / "scripts" / MOD_NAME / "State.ini")
    native_names = bool(settings.get("native_names", True))
    tracks, seen = [], set()
    for path in scan_audio(music):
        rel = path.relative_to(music).as_posix()
        seen.add(rel)
        prev = old.get(rel)
        st = path.stat()
        unchanged = prev and prev.get('_file_size') == st.st_size and prev.get('_file_mtime_ns') == st.st_mtime_ns
        if unchanged:
            meta = {k: prev.get(k, '') for k in ('artist', 'title', 'album', 'display_artist', 'display_title', 'display_album', 'source')}
            dur = prev.get('duration_seconds')
        else:
            meta = parse_metadata(path, native_names)
            dur = duration_seconds(path, ffmpeg if ffmpeg.exists() else None)
        side = read_sidecar(path) if not prev else None
        if prev:
            entry = dict(prev)
            for k in ("artist", "title", "album"):
                entry[k] = prev.get(k) or meta[k]
            for k in ("display_artist", "display_title", "display_album"):
                entry[k] = prev.get(k) or meta[k]
            if entry.get("album") == LEGACY_ALBUM:
                entry["album"] = ""
            if entry.get("display_album") == LEGACY_ALBUM:
                entry["display_album"] = ""
            entry["mode"] = prev.get("mode", "ALL")
            entry["enabled"] = bool(prev.get("enabled", True))
        else:
            entry = {"file": rel, "enabled": True, "mode": "ALL"}
            entry.update(meta)
        # The sidecar is a portable fallback for files that are new to the library.
        # Once a track is managed by the program, MusicLibrary.json is authoritative.
        if side and not prev:
            for k, dk in (("title", "display_title"), ("artist", "display_artist"), ("album", "display_album")):
                if side.get(k):
                    val = clean_text(side[k])
                    if k == "album" and val == LEGACY_ALBUM:
                        val = ""
                    entry[dk] = val
            if side.get("mode"):
                entry["mode"] = side["mode"].strip().upper()
        key = stable_key(rel)
        if key in state_modes and state_modes[key] in ("ALL", "FE", "IG", "OFF"):
            entry["mode"] = state_modes[key]
            entry["enabled"] = entry["mode"] != "OFF"
        # "mode" is the single source of truth: OFF always means disabled.
        if str(entry.get("mode", "ALL")).upper() == "OFF":
            entry["enabled"] = False
        elif not entry.get("enabled", True):
            entry["mode"] = "OFF"
        entry['_file_size'] = st.st_size
        entry['_file_mtime_ns'] = st.st_mtime_ns
        entry["duration_seconds"] = round(dur, 3) if dur is not None else None
        entry["duration"] = format_duration(dur)
        entry["key"] = key
        entry["sidecar"] = Path(rel).with_suffix(".ini").as_posix()
        write_sidecar(path, entry)
        tracks.append(entry)
    # preserve previous order, append new files
    order = {e["file"]: i for i, e in enumerate(lib.get("tracks", [])) if e.get("file")}
    tracks.sort(key=lambda e: (order.get(e["file"], 10**9), e["file"].lower()))
    removed = sorted(set(old) - seen)
    for r in removed:
        log(f"Убран из библиотеки (файл отсутствует): {r}")
    lib.update({"format": 2, "generator": f"{APP} {VERSION}", "game": str(game),
                "music_folder": str(music), "tracks": tracks})
    save_library(lib_path, lib)
    log(f"MusicLibrary.json: {len(tracks)} треков ({format_duration(sum(e.get('duration_seconds') or 0 for e in tracks))})")
    return lib


# ---------------------------------------------------------------------------
# Settings / installation
# ---------------------------------------------------------------------------
def settings_path(game):
    return Path(game) / "scripts" / MOD_NAME / "Manager.ini"


def load_settings(game):
    p = settings_path(game)
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str
    if p.exists():
        cp.read(p, encoding="utf-8")
    s = {"volume": 1.0, "native_names": True, "stock_mode": "KEEP"}
    if cp.has_section("Manager"):
        try:
            s["volume"] = float(cp.get("Manager", "Volume", fallback="1.0"))
        except Exception:
            pass
        s["native_names"] = cp.getboolean("Manager", "NativeNames", fallback=True)
        s["stock_mode"] = cp.get("Manager", "StockMode", fallback="KEEP").upper()
        ff = cp.get("Manager", "FFmpeg", fallback="").strip()
        if ff:
            s["ffmpeg"] = ff
    return s


def save_settings(game, s):
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str
    cp["Manager"] = {"Volume": f"{float(s.get('volume', 1.0)):.3f}",
                     "NativeNames": "1" if s.get("native_names") else "0",
                     "StockMode": str(s.get("stock_mode", "KEEP")).upper()}
    if s.get("ffmpeg"):
        cp["Manager"]["FFmpeg"] = str(s["ffmpeg"])
    p = settings_path(game)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        cp.write(f)


def verify_guards(exe_data):
    for addr, expected in GUARDS:
        off = addr - 0x400000
        if exe_data[off:off + len(expected)] != expected:
            return False, addr
    return True, None


def validate_game(game, log=log_default):
    game = Path(game)
    exe = game / "speed.exe"
    mpf = game / "SOUND" / "PFDATA" / "MW_Music.mpf"
    mus = game / "SOUND" / "PFDATA" / "MW_Music.mus"
    for p in (exe, mpf, mus):
        if not p.is_file():
            raise FileNotFoundError(f"Не найден файл игры: {p}")
    data = exe.read_bytes()
    ok, bad = verify_guards(data)
    if not ok:
        raise RuntimeError(f"speed.exe не совпадает с ожидаемым layout (guard 0x{bad:08X}). "
                           "Нужен NFSMW 1.3 EN / совместимый NFSPatcher exe.")
    log(f"speed.exe: {len(data)} байт, SHA-256 {sha256_file(exe)}; guards 29/29 OK")
    counts = mpf_counts(mpf.read_bytes())
    if counts != STOCK_LAYOUT:
        raise RuntimeError(f"Неподдерживаемая структура MW_Music.mpf: nodes={counts[0]}, events={counts[1]}, samples={counts[2]}. "
                           f"Ожидается {STOCK_LAYOUT}. Восстановите штатные MW_Music.mpf/.mus.")
    log(f"MW_Music.mpf: nodes={counts[0]}, events={counts[1]}, samples={counts[2]}")
    loader = next((n for n in KNOWN_LOADERS if (game / n).is_file()), None)
    if loader:
        log(f"ASI-загрузчик: {loader}")
    else:
        log("ASI-загрузчик будет установлен вместе с патчем (dinput8.dll).")
    return {"exe_size": len(data), "exe_sha": sha256_file(exe),
            "stock_mpf_sha": sha256_file(mpf), "stock_mus_sha": sha256_file(mus)}


def game_is_running():
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq speed.exe"],
                             capture_output=True, text=True, encoding="mbcs", errors="replace",
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return "speed.exe" in (out.stdout or "").lower()
    except Exception:
        return False


def is_own_loader(path):
    """True only for the proxy shipped by this manager (stable embedded tag)."""
    try:
        return LOADER_TAG in Path(path).read_bytes()
    except OSError:
        return False


def install_loader_file(game, src, log=log_default):
    """Install our dinput8.dll proxy only when the game has no other loader.

    Never overwrites a third-party dinput8.dll / dsound.dll / etc. Returns True
    if our proxy is present afterwards (installed or already up to date).
    """
    game = Path(game)
    dst = game / LOADER_NAME
    data = Path(src).read_bytes()
    if dst.exists():
        if is_own_loader(dst):
            if dst.read_bytes() != data:
                shutil.copyfile(src, dst)
                log(f"ASI-загрузчик обновлён: {dst}")
            else:
                log(f"ASI-загрузчик уже установлен: {dst}")
            return True
        log(f"Сторонний {LOADER_NAME} уже есть — свой загрузчик не устанавливается.")
        return False
    other = next((n for n in KNOWN_LOADERS if n != LOADER_NAME and (game / n).is_file()), None)
    if other:
        log(f"Обнаружен сторонний ASI-загрузчик {other} — свой dinput8.dll не устанавливается.")
        return False
    shutil.copyfile(src, dst)
    log(f"Установлен ASI-загрузчик: {dst}")
    return True


def extract_payload(payload, dest, log=log_default):
    tmp = None
    if payload is None:
        if not EMBEDDED_PAYLOAD_B64:
            raise FileNotFoundError("Встроенный payload отсутствует. Положите payload.zip рядом со скриптом или укажите --payload.")
        import base64
        tmp = tempfile.mkdtemp(prefix="nfsmw_payload_")
        zip_path = Path(tmp) / "payload.zip"
        zip_path.write_bytes(base64.b85decode(EMBEDDED_PAYLOAD_B64.encode("ascii")))
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(tmp)
        root = Path(tmp)
    else:
        payload = Path(payload)
        if payload.is_dir():
            root = payload
        elif payload.suffix.lower() == ".zip":
            tmp = tempfile.mkdtemp(prefix="nfsmw_payload_")
            with zipfile.ZipFile(payload, "r") as z:
                z.extractall(tmp)
            root = Path(tmp)
        else:
            raise ValueError("payload должен быть папкой или .zip")
    try:
        scripts = dest / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        asi_src = root / "scripts" / ASI_NAME
        if not asi_src.is_file():
            raise FileNotFoundError(f"В payload нет {asi_src}")
        shutil.copy2(asi_src, scripts / ASI_NAME)
        mod_src = root / "scripts" / MOD_NAME
        mod_dst = scripts / MOD_NAME
        mod_dst.mkdir(parents=True, exist_ok=True)
        for p in sorted(mod_src.rglob("*")):
            if p.is_file():
                rel = p.relative_to(mod_src)
                dst = mod_dst / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                if not dst.exists() or dst.stat().st_size != p.stat().st_size or sha256_file(dst) != sha256_file(p):
                    shutil.copy2(p, dst)
        log(f"Установлен мост: {scripts / ASI_NAME}")
        loader_src = root / LOADER_NAME
        if loader_src.is_file():
            install_loader_file(dest, loader_src, log)
        else:
            log("ВНИМАНИЕ: в payload нет dinput8.dll; чистой игре понадобится сторонний ASI-загрузчик.")
        # Disable the old expansion ASI so the two mods do not hook the same game.
        old = scripts / OLD_ASI
        if old.exists():
            disabled = old.with_suffix(".asi.disabled")
            if not disabled.exists():
                old.rename(disabled)
                log(f"Старый мод отключён: {old.name} -> {disabled.name}")
            else:
                old.unlink()
                log(f"Старый мод удалён (уже был disabled): {old.name}")
        (dest / "CustomMusic").mkdir(parents=True, exist_ok=True)
        return mod_dst
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def default_payload():
    if EMBEDDED_PAYLOAD_B64:
        return None
    p = Path(__file__).resolve().with_name("payload.zip")
    return p if p.exists() else None


# ---------------------------------------------------------------------------
# Compact offline FFmpeg converter. Distributed under its own LGPL license;
# project source and license: https://ffmpeg.org/ and https://ffmpeg.org/legal.html
# The exact build configuration is embedded in the ffmpeg executable (-buildconf).
# It is restricted to the project's supported audio formats, not full FFmpeg.
# ---------------------------------------------------------------------------
def temp_dir():
    base = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parent)
    d = base / "temp"
    try:
        d.mkdir(parents=True, exist_ok=True)
        return d
    except OSError:
        d = Path(tempfile.gettempdir()) / "nfsmw_temp"
        d.mkdir(parents=True, exist_ok=True)
        return d


def ffmpeg_path(game=None):
    return temp_dir() / "ffmpeg.exe"


def ensure_converter(game=None, log=log_default, progress=None, ffmpeg_override=None, cancel=None):
    """Use an existing converter or unpack the compact embedded one; no network."""
    tmp = temp_dir()
    ff = ffmpeg_path(game)
    if cancel is not None and cancel.is_set():
        raise BuildCancelled("Остановлено пользователем")
    if ffmpeg_override:
        src = Path(ffmpeg_override)
        if src.is_file():
            if not ff.exists() or sha256_file(src) != sha256_file(ff):
                shutil.copy2(src, ff)
            return ff
    if ff.exists() and ff.stat().st_size > 100000:
        return ff
    if EMBEDDED_FFMPEG_XZ_B64:
        import base64
        import lzma
        log("Распаковываю встроенный компактный FFmpeg в temp — интернет не нужен…")
        try:
            data = lzma.decompress(base64.b85decode(EMBEDDED_FFMPEG_XZ_B64))
        except Exception as ex:
            raise RuntimeError(f"Не удалось распаковать встроенный FFmpeg: {ex}") from ex
        if hashlib.sha256(data).hexdigest() != EMBEDDED_FFMPEG_SHA256:
            raise RuntimeError("Ошибка проверки встроенного FFmpeg (SHA-256).")
        if cancel is not None and cancel.is_set():
            raise BuildCancelled("Остановлено пользователем")
        fd, name = tempfile.mkstemp(prefix="ffmpeg_", suffix=".tmp", dir=tmp)
        stage = Path(name)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(data)
                out.flush()
            try:
                os.replace(stage, ff)
            except PermissionError:
                # Another instance may have already installed and started it.
                if not ff.is_file() or sha256_file(ff) != EMBEDDED_FFMPEG_SHA256:
                    raise
            log(f"Встроенный FFmpeg готов: {ff}")
        finally:
            stage.unlink(missing_ok=True)
        return ff
    # Development source tree only; the delivered single-file build embeds it.
    local = Path(__file__).resolve().parent / "tools" / "ffmpeg_win_reference.exe"
    if local.is_file():
        shutil.copy2(local, ff)
        return ff
    raise RuntimeError("Встроенный FFmpeg отсутствует. Укажите свой через --ffmpeg "
                       "или поместите ffmpeg.exe в папку temp рядом с программой.")


def _game_choice_path():
    # User-level preference, outside the game folder; no registry access.
    base = os.environ.get('APPDATA') if os.name == 'nt' else None
    return Path(base) / 'NFSMWCustomMusicManager' / 'game.json' if base else None


def remember_game(game):
    pref = _game_choice_path()
    if not pref or not (Path(game) / 'speed.exe').is_file(): return
    try:
        pref.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix='.game_', suffix='.tmp', dir=pref.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump({'game': str(Path(game).resolve())}, f, ensure_ascii=False)
            os.replace(temp, pref)
        finally:
            if os.path.exists(temp): os.unlink(temp)
    except OSError:
        pass


def find_game(explicit=None):
    if explicit:
        p = Path(explicit)
        if p.is_file() and p.name.lower() == "speed.exe":
            p = p.parent
        return p.resolve()
    candidates = []
    pref = _game_choice_path()
    if pref:
        try:
            saved = json.loads(pref.read_text(encoding='utf-8')).get('game')
            if isinstance(saved, str) and saved:
                candidates.append(Path(saved))
        except (OSError, ValueError, AttributeError):
            pass
    if getattr(sys, "frozen", False):  # PyInstaller onefile: look next to the .exe / cwd
        candidates += [Path(sys.executable).resolve().parent, Path.cwd()]
    else:
        candidates += [Path(__file__).resolve().parent, Path.cwd()]
    candidates += [Path(r"D:\Program Files (x86)\NFS Most Wanted HQ_code"),
                   Path(r"D:\Program Files (x86)\NFS Most Wanted HQ"),
                   Path(r"C:\Program Files (x86)\EA Games\Need for Speed Most Wanted"),
                   Path(r"C:\Program Files\EA Games\Need for Speed Most Wanted")]
    for p in candidates:
        if (p / "speed.exe").is_file():
            return p.resolve()
    return None


# ---------------------------------------------------------------------------
# Cache conversion (runs in Python, before the game)
# ---------------------------------------------------------------------------
class BuildCancelled(RuntimeError):
    """Raised when the user presses the emergency stop button."""


def _kill_proc(p):
    try:
        p.terminate()
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=3)
    except Exception:
        pass


def run_tool(args, timeout=900, cancel=None, low_priority=False):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if low_priority and os.name == "nt":
        flags |= getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000)
    p = subprocess.Popen([str(a) for a in args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         creationflags=flags)
    deadline = time.monotonic() + timeout
    while True:
        if cancel is not None and cancel.is_set():
            _kill_proc(p)
            raise BuildCancelled("Остановлено пользователем")
        try:
            out, err = p.communicate(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            if time.monotonic() > deadline:
                _kill_proc(p)
                raise RuntimeError(f"{Path(args[0]).name}: превышено время ожидания ({timeout} с)")
    if p.returncode:
        name = Path(args[0]).name
        raise RuntimeError(f"{name}: " + err.decode(errors="replace")[-1500:] +
                           out.decode(errors="replace")[-500:])
    return subprocess.CompletedProcess(p.args, p.returncode, out, err)


def encode_eaxa(wav, destination, encoder, cancel=None):
    raw = Path(destination).with_suffix(".pcm")
    try:
        with wave.open(str(wav)) as w, open(raw, "wb") as f:
            if w.getparams()[:3] != (2, 2, 36000):
                raise ValueError("Ожидается stereo PCM16 36 kHz")
            frames = w.getnframes()
            while True:
                data = w.readframes(65536)
                if not data:
                    break
                f.write(data)
        run_tool([encoder, raw, 0, frames, destination], cancel=cancel)
        return round(frames * 1000 / 36000)
    finally:
        raw.unlink(missing_ok=True)


class FileLock:
    def __init__(self, path):
        self.path = Path(path)
        self.f = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(self.path, "a+b")
        self.f.seek(0)
        self.f.write(b"0")
        self.f.flush()
        self.f.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return self

    def __exit__(self, *exc):
        try:
            if os.name == "nt":
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.f.fileno(), fcntl.LOCK_UN)
        finally:
            self.f.close()


def write_custom_state_modes(game, changes):
    """Update the same State.ini keys the native bridge reads, without audio work."""
    if not changes:
        return
    path = Path(game) / 'scripts' / MOD_NAME / 'State.ini'
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        if not path.exists():
            try:
                with path.open('xb') as f: f.write(b'\xff\xfe')
            except FileExistsError:
                pass
        api = ctypes.windll.kernel32.WritePrivateProfileStringW
        api.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR]
        api.restype = wintypes.BOOL
        for key, mode in changes.items():
            if not api(key, 'Mode', mode, str(path.resolve())):
                raise OSError('Не удалось обновить State.ini')
    else:
        cp = _read_ini_any(path)
        for key, mode in changes.items():
            if not cp.has_section(key): cp.add_section(key)
            cp.set(key, 'Mode', mode)
        fd, name = tempfile.mkstemp(prefix='.state_', suffix='.tmp', dir=path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-16', newline='\r\n') as f: cp.write(f)
            os.replace(name, path)
        finally:
            if os.path.exists(name): os.unlink(name)


def published_track_files(game, music, entries, hashes_cache=None):
    """Verify the installed cache and compare source hashes; never change files."""
    cache = Path(game) / 'scripts' / MOD_NAME / 'Cache'
    hashes_cache = hashes_cache if hashes_cache is not None else {}
    try:
        rec = json.loads((cache / 'Build.json').read_text(encoding='utf-8'))
        for name in ('EA_TRAX.mpf', 'EA_TRAX.mus'):
            if tuple(rec.get('output_stats', {}).get(name, ())) != file_stamp(cache/name):
                return set(), {}
        records = {x['rel']: x['hash'] for x in rec.get('measurements', [])}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return set(), {}
    matched, hashes = set(), {}
    for e in entries:
        rel = e.get('file', '')
        if rel not in records: continue
        try:
            p = Path(music) / rel
            st = p.stat()
            key = (str(p.resolve()), st.st_size, st.st_mtime_ns)
            h = hashes_cache.get(key)
            if h is None:
                h = sha256_file(p)
                # Do not trust the result if the file changed while hashing.
                st2 = p.stat()
                if (st.st_size, st.st_mtime_ns) != (st2.st_size, st2.st_mtime_ns): continue
                hashes_cache[key] = h
            hashes[rel] = h
            if h == records[rel]: matched.add(rel)
        except (OSError, ValueError):
            continue
    return matched, hashes


def publish_cached_modes(game, music, lib, settings):
    """Republish only previously built entries, without encoding or banking audio."""
    game = Path(game); mod = game / 'scripts' / MOD_NAME; cache = mod / 'Cache'
    manifest = cache / 'Playlist.ini'
    if not manifest.is_file() or not (cache/'Build.json').is_file(): return 0
    with FileLock(cache/'build.lock'):
        cp = _read_ini_any(manifest)
        if not cp.has_section('Native'): return 0
        try:
            # The bridge's recorded identity must still match the actual banks.
            for label, path in (('CacheMpf', cache/'EA_TRAX.mpf'), ('CacheMus', cache/'EA_TRAX.mus'),
                                ('StockMpf', game/'SOUND/PFDATA/MW_Music.mpf'), ('StockMus', game/'SOUND/PFDATA/MW_Music.mus')):
                size, stamp = file_stamp(path)
                if (int(cp.get('Native',label+'Bytes')), int(cp.get('Native',label+'WriteTime'))) != (size, stamp):
                    return 0
            identity = {'exe_size': int(cp.get('Native','ExeBytes')), 'exe_sha': cp.get('Native','ExeSHA256')}
        except (OSError, ValueError, configparser.Error):
            return 0
        try:
            record = json.loads((cache/'Build.json').read_text(encoding='utf-8'))
            if any(tuple(record.get('output_stats', {}).get(n, ())) != file_stamp(cache/n)
                   for n in ('EA_TRAX.mpf', 'EA_TRAX.mus')):
                return 0
        except (OSError, ValueError, TypeError, AttributeError):
            return 0
        valid, hashes = published_track_files(game, music, lib.get('tracks', []))
        safe = dict(lib)
        safe['tracks'] = [e for e in lib.get('tracks', []) if e.get('file') in valid]
        write_manifest(game, mod, safe, settings, identity, log=lambda *_:None, hashes=hashes)
        return len(valid)


def build_cache(game, mod, lib, settings, log=log_default, progress=None, cancel=None, identity=None):
    """Convert enabled tracks to EA-XA, committing each track incrementally.

    Returns (built, cache_hit). After every track the published Cache/EA_TRAX.*
    and Build.json describe a consistent, playable subset, so an emergency stop
    (or a sudden power loss) never breaks the cache and the next run resumes
    exactly where this one stopped. Event IDs are assigned by content hash so
    that reordering/editing metadata does not re-encode audio.
    """
    game, mod = Path(game), Path(mod)
    cache = mod / "Cache"
    cache.mkdir(parents=True, exist_ok=True)
    ffmpeg = ffmpeg_path(game)
    encoder = mod / "EAxAEncode.exe"
    if not ffmpeg.exists() or not encoder.exists():
        raise FileNotFoundError("Конвертер не установлен. Нажмите «Установить» — FFmpeg докачается в папку temp.")
    volume = float(settings.get("volume", 1.0))
    if not math.isfinite(volume) or not 0 <= volume <= 2:
        raise ValueError("Volume должен быть 0..2")
    enabled = [e for e in lib.get("tracks", []) if e.get("enabled", True)]
    if len(enabled) > MAX_CUSTOM_TRACKS:
        raise ValueError(f"Слишком много своих треков: {len(enabled)} > {MAX_CUSTOM_TRACKS}")
    stock_on = stock_enabled_count(game)
    if stock_on + len(enabled) > MAX_TOTAL_TRACKS:
        raise ValueError(f"Всего видимых треков {stock_on + len(enabled)} > {MAX_TOTAL_TRACKS}. "
                         "Отключите часть оригинальных или своих треков.")
    jobs = []
    for e in enabled:
        path = (Path(lib["music_folder"]) / e["file"]).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Трек не найден: {path}")
        jobs.append({"path": str(path), "rel": e["file"], "hash": sha256_file(path), "entry": e})
    # Stable audio order: content hash, then relative path.
    jobs.sort(key=lambda j: (j["hash"], j["rel"].lower()))
    for i, j in enumerate(jobs):
        j["event"] = 0xE00000 + i
        j["sample"] = None

    stock = game / "SOUND" / "PFDATA" / "MW_Music.mpf"
    stockmus = stock.with_suffix(".mus")
    # validate_game already hashes these files for this build.
    basis = {"mpf": identity["stock_mpf_sha"], "mus": identity["stock_mus_sha"]} if identity else {"mpf": sha256_file(stock), "mus": sha256_file(stockmus)}
    full_sig = {"version": CACHE_VERSION, "basis": basis, "volume": volume,
                "jobs": [{"hash": j["hash"], "rel": j["rel"]} for j in jobs]}
    fingerprint = hashlib.sha256(json.dumps(full_sig, sort_keys=True).encode()).hexdigest()
    record = cache / "Build.json"
    hashes = {j["rel"]: j["hash"] for j in jobs}
    mus_path, mpf_path = cache / "EA_TRAX.mus", cache / "EA_TRAX.mpf"

    def stopped():
        return cancel is not None and cancel.is_set()

    with FileLock(cache / "build.lock"), open(cache / "Build.log", "a", encoding="utf-8", buffering=1) as lf:
        def say(msg):
            lf.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
            log(msg)
            if progress:
                progress(msg)

        previous = {}
        if record.exists():
            try:
                previous = json.loads(record.read_text(encoding="utf-8"))
            except Exception:
                previous = {}
        prev_meas = previous.get("measurements", [])
        prev_mus_bytes = previous.get("mus_bytes")
        # Repair a torn .mus tail left by a hard power loss: those bytes are not
        # referenced by any sample, so truncating back to the last commit is safe.
        prev_ok = False
        if prev_mus_bytes and mus_path.exists():
            try:
                if mus_path.stat().st_size > prev_mus_bytes:
                    with open(mus_path, "r+b") as f:
                        f.truncate(prev_mus_bytes)
                    say("Восстановление: обрезан незавершённый хвост после аварийной остановки.")
                    # Refresh the recorded stamp so a repaired complete cache still hits.
                    previous.setdefault("output_stats", {})["EA_TRAX.mus"] = list(file_stamp(mus_path))
                    tmp = cache / "Build.tmp"
                    tmp.write_text(json.dumps(previous, ensure_ascii=False, indent=2), encoding="utf-8")
                    os.replace(tmp, record)
                prev_ok = mus_path.stat().st_size == prev_mus_bytes
            except OSError:
                prev_ok = False

        def stamp(name):
            return tuple(previous.get("output_stats", {}).get(name, ()))

        if (previous.get("fingerprint") == fingerprint and previous.get("complete")
                and prev_ok and mpf_path.exists()
                and file_stamp(mpf_path) == stamp("EA_TRAX.mpf")
                and file_stamp(mus_path) == stamp("EA_TRAX.mus")):
            say(f"CACHE HIT: {len(jobs)} треков; перекодирование не требуется")
            _assign_samples_from_record(jobs, previous)
            return False, True
        if stopped():
            raise BuildCancelled("Остановлено пользователем")

        prev_sig = previous.get("signature", {})
        # Never reuse an altered or mismatched bank. A changed stamp triggers
        # a full checksum, so valid old caches from earlier versions still migrate.
        bank_valid = False
        if prev_ok and mpf_path.exists():
            try:
                bank_valid = (sha256_file(mpf_path) == previous.get("outputs", {}).get("EA_TRAX.mpf") and
                              (file_stamp(mus_path) == stamp("EA_TRAX.mus") or
                               sha256_file(mus_path) == previous.get("outputs", {}).get("EA_TRAX.mus")))
            except OSError:
                pass
        same_basis = (prev_ok and bank_valid and prev_sig.get("version") == CACHE_VERSION
                      and prev_sig.get("basis") == basis and prev_sig.get("volume") == volume)
        # Continuation: the previous partial build is an exact prefix of this list.
        start, continuation = 0, False
        if same_basis and mpf_path.exists() and previous.get("outputs", {}).get("EA_TRAX.mpf"):
            try:
                pj = prev_sig.get("jobs", [])
                k = len(prev_meas)
                if (k <= len(jobs) and sha256_file(mpf_path) == previous["outputs"]["EA_TRAX.mpf"]
                        and all(pj[i]["hash"] == jobs[i]["hash"] and pj[i]["rel"] == jobs[i]["rel"]
                                for i in range(k))):
                    continuation, start = True, k
            except Exception:
                continuation = False
        reusable = {}
        if same_basis:
            try:
                old_ss = samples(mpf_path.read_bytes())
                # The pool also retains removed/disabled songs so they can be
                # re-enabled later without re-encoding (until a full rebuild).
                candidates = list(previous.get("stream_pool", {}).values()) + list(prev_meas)
                with open(mus_path, "rb") as cached:
                    for m in candidates:
                        off = m.get("offset")
                        if off is None:
                            off = old_ss[m["sample"] - 1][0] * 128
                        if not isinstance(off, int) or off < stockmus.stat().st_size or off + 8 > prev_mus_bytes or off % 128:
                            continue
                        cached.seek(off)
                        if cached.read(4) != b'SCHl':
                            continue
                        if isinstance(m.get('hash'), str) and isinstance(m.get('duration_ms'), int):
                            reusable[m['hash']] = (off, {'hash':m['hash'], 'offset':off, 'duration_ms':m['duration_ms']})
            except (OSError, ValueError, KeyError, TypeError, IndexError, struct.error) as ex:
                reusable = {}
                say(f"Не удалось переиспользовать старый кэш: {ex}")

        # Running SHA-256 of the published .mus so each per-track commit is O(track)
        # instead of re-hashing the whole growing bank.
        mus_hash = hashlib.sha256()
        if continuation:
            say(f"Продолжение сборки: готово {start}/{len(jobs)}, осталось {len(jobs) - start}.")
            b = mpf_path.read_bytes()
            ns, es, ss = nodes(b), events(b), samples(b)
            for i in range(start):
                jobs[i]["sample"] = prev_meas[i]["sample"]
                jobs[i]["event"] = prev_meas[i]["event"]
            measurements = list(prev_meas)
            with open(mus_path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    mus_hash.update(chunk)
            prior_mus, initialized = None, True
            committed_bytes = prev_mus_bytes
        else:
            say(f"Сборка кэша: {len(jobs)} треков. Штатные файлы не изменяются.")
            b = stock.read_bytes()
            ns, es, ss = nodes(b), events(b), samples(b)
            if (len(ns), len(es), len(ss)) != STOCK_LAYOUT:
                raise ValueError("Stock layout mismatch")
            # Keep the existing MUS in place: its encoded streams remain at
            # stable offsets. Only the small MPF index needs rebuilding.
            reuse_bank = same_basis and mus_path.exists()
            source_bank = mus_path if reuse_bank else stockmus
            with open(source_bank, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    mus_hash.update(chunk)
            measurements, initialized = [], bool(reuse_bank)
            committed_bytes = prev_mus_bytes if reuse_bank else None
            prior_mus = None
            if reuse_bank:
                say(f"Используется готовый кэш: {len(reusable)} закодированных треков.")
        template = next((e for e in es if u32(e, 12) & 0xFFFFFF == 0xAD947), None)
        if template is None:
            raise ValueError("В штатном банке не найден шаблон события 0xAD947")
        templates = [bytearray(ns[i]) for i in (1490, 1491, 1492)]

        def commit():
            out = serialize(b, ns, es, ss)
            (cache / "EA_TRAX.mpf.tmp").write_bytes(out)
            os.replace(cache / "EA_TRAX.mpf.tmp", mpf_path)
            complete = len(measurements) == len(jobs)
            sub_sig = {"version": CACHE_VERSION, "basis": basis, "volume": volume,
                       "jobs": [{"hash": j["hash"], "rel": j["rel"]} for j in jobs[:len(measurements)]]}
            payload = {
                "fingerprint": hashlib.sha256(json.dumps(sub_sig, sort_keys=True).encode()).hexdigest(),
                "target_fingerprint": fingerprint, "complete": complete,
                "signature": sub_sig, "mus_bytes": mus_path.stat().st_size,
                "outputs": {"EA_TRAX.mpf": sha256_file(mpf_path), "EA_TRAX.mus": mus_hash.hexdigest().upper()},
                "output_stats": {name: list(file_stamp(cache / name)) for name in ("EA_TRAX.mpf", "EA_TRAX.mus")},
                "measurements": measurements,
                "stream_pool": {h: data for h, (_, data) in reusable.items()},
                "nodes": len(ns), "samples": len(ss), "events": len(es)}
            tmp = cache / "Build.tmp"
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, record)
            if identity is not None:
                try:
                    write_manifest(game, mod, lib, settings, identity, log=lambda *_: None, hashes=hashes)
                except Exception:
                    pass
            return complete

        def report_audio_progress():
            # Count cached audio by content, independently of sorted job order.
            # A stream can be ready before the small game index is rebuilt.
            ready = sum(1 for j in jobs if j['hash'] in reusable)
            say(f"Аудио готово: {ready}/{len(jobs)}.")
            if progress:
                progress(f"AUDIO_PROGRESS {ready}/{len(jobs)} {len(jobs)-ready}")

        report_audio_progress()
        with tempfile.TemporaryDirectory(prefix="build-", dir=cache) as td:
            work = Path(td)
            if prior_mus == "snapshot":
                prior_mus = work / "prior.mus"
                shutil.copyfile(mus_path, prior_mus)
            try:
                for index in range(start, len(jobs)):
                    if stopped():
                        raise BuildCancelled("Остановлено пользователем")
                    j = jobs[index]
                    pcm, normalized, stream = work / "source.wav", work / "normalized.wav", work / "track.asf"
                    reused = reusable.get(j["hash"])
                    if reused:
                        offset, meas = reused
                        duration = meas["duration_ms"]
                    else:
                        run_tool([ffmpeg, "-v", "error", "-y", "-threads", "1", "-i", j["path"],
                                  "-vn", "-ac", "2", "-ar", "36000", "-c:a", "pcm_s16le", pcm], cancel=cancel)
                        text = run_tool([ffmpeg, "-hide_banner", "-nostats", "-threads", "1", "-i", pcm,
                                         "-af", "loudnorm=I=-8.6:TP=-1:LRA=11:print_format=json",
                                         "-f", "null", "-"], cancel=cancel).stderr.decode(errors="replace")
                        m = re.search(r'\{\s*"input_i".*?\}', text, re.S)
                        lufs = float(json.loads(m[0])["input_i"]) if m else float("-inf")
                        gain = max(-60, min(12, -8.6 - lufs)) if math.isfinite(lufs) else 0
                        amplitude = 10 ** (gain / 20) * volume
                        run_tool([ffmpeg, "-v", "error", "-y", "-threads", "1", "-i", pcm,
                                  "-af", f"volume={amplitude:.9f},alimiter=limit=0.98:level=0:latency=1",
                                  "-c:a", "pcm_s16le", normalized], cancel=cancel)
                        duration = encode_eaxa(normalized, stream, encoder, cancel=cancel)
                    if stopped():
                        raise BuildCancelled("Остановлено пользователем")
                    # Append the fully-prepared stream to the published .mus. The
                    # append handle is short-lived so the file is never left open
                    # between tracks; a stop between tracks leaves no torn tail.
                    if not reused:
                        stream_bytes = stream.read_bytes()
                        if not initialized:
                            shutil.copyfile(stockmus, work / "init.mus")
                            with open(work / "init.mus", "ab") as d:
                                pad = bytes((-d.tell()) % 128); d.write(pad); offset = d.tell()
                                d.write(stream_bytes)
                                d.flush(); os.fsync(d.fileno())
                            os.replace(work / "init.mus", mus_path)
                            initialized = True
                        else:
                            with open(mus_path, "ab") as d:
                                pad = bytes((-d.tell()) % 128); d.write(pad); offset = d.tell()
                                d.write(stream_bytes)
                                d.flush(); os.fsync(d.fileno())
                        mus_hash.update(pad); mus_hash.update(stream_bytes)
                        reusable[j['hash']] = (offset, {'hash':j['hash'], 'offset':offset, 'duration_ms':duration})
                    if mus_path.stat().st_size > MAX_BANK_BYTES:
                        raise ValueError("Кэш превышает 2 GiB. Уменьшите число треков.")
                    ss.append((offset // 128, duration))
                    sample = len(ss)
                    j["sample"] = sample
                    root = len(ns)
                    event = j["event"]
                    for pos, t in enumerate(templates):
                        r = bytearray(t)
                        p32(r, 0, ([0, -1, sample][pos] & 65535) | (5 << 21))
                        struct.pack_into("<H", r, 8, root if pos != 1 else root + 1)
                        if pos != 1:
                            struct.pack_into("<H", r, 18, root + 2 if pos == 0 else root + 1)
                        ns.append(r)
                    e = bytearray(template)
                    p32(e, 12, (u32(e, 12) & 0xFF000000) | event)
                    struct.pack_into("<H", e, 40, root)
                    es.append(e)
                    measurements.append({"hash": j["hash"], "rel": j["rel"], "sample": sample,
                                         "duration_ms": duration, "event": event, "offset": offset})
                    complete = commit()
                    committed_bytes = mus_path.stat().st_size
                    say(f"{'Reused' if reused else 'Encoded'} {index + 1}/{len(jobs)}: {Path(j['path']).name}")
                    if not reused:
                        report_audio_progress()
                    if complete:
                        say(f"COMPLETE: nodes={len(ns)} events={len(es)} samples={len(ss)}")
                        return True, False
                # Empty library: publish a stock-only cache once.
                if not jobs:
                    if not initialized:
                        shutil.copyfile(stockmus, mus_path)
                    commit()
                    say("COMPLETE: nodes=%d events=%d samples=%d" % (len(ns), len(es), len(ss)))
                    return True, False
                return True, False
            except BaseException:
                # Roll the .mus back to the last committed track so the cache stays
                # playable, then refresh the manifest stamps to match. Only act once
                # a commit succeeded this run; otherwise the next run rebuilds safely.
                if committed_bytes is not None:
                    try:
                        if mus_path.exists() and mus_path.stat().st_size > committed_bytes:
                            with open(mus_path, "r+b") as f:
                                f.truncate(committed_bytes)
                        if identity is not None and record.exists():
                            write_manifest(game, mod, lib, settings, identity, log=lambda *_: None, hashes=hashes)
                    except Exception:
                        pass
                raise



def _assign_samples_from_record(jobs, record):
    by_rel = {(m["hash"], m["rel"]): m for m in record.get("measurements", [])}
    for j in jobs:
        m = by_rel.get((j["hash"], j["rel"]))
        if m:
            j["sample"] = m["sample"]
            j["event"] = m["event"]


def write_manifest(game, mod, lib, settings, identity, log=log_default, hashes=None):
    """Write Cache/Playlist.ini consumed by the native bridge, plus NativeMusic.ini.

    Only tracks present in Build.json are emitted, so a partially-built cache
    (after an emergency stop) still yields a valid, playable playlist.
    """
    game, mod = Path(game), Path(mod)
    cache = mod / "Cache"
    record = json.loads((cache / "Build.json").read_text(encoding="utf-8"))
    by_rel = {m["rel"]: m for m in record.get("measurements", [])}
    enabled = [e for e in lib.get("tracks", []) if e.get("enabled", True)]
    entries = []
    for e in enabled:
        m = by_rel.get(e["file"])
        if not m:
            continue  # not built yet (incremental build in progress / stopped)
        h = (hashes or {}).get(e["file"])
        if h is None:
            h = sha256_file((Path(lib["music_folder"]) / e["file"]).resolve())
        if m["hash"] != h:
            log(f"Трек изменился после сборки и ждёт публикации: {e['file']}")
            continue
        entries.append((e, m))
    if stock_enabled_count(game) + len(entries) > MAX_TOTAL_TRACKS:
        raise ValueError(f"В плейлисте больше {MAX_TOTAL_TRACKS} треков. Отключите часть треков перед запуском.")
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str
    unicode_needed = bool(settings.get("native_names")) or any(
        any(ord(ch) > 127 for ch in str(e.get(k, "")))
        for e, _ in entries for k in ("display_title", "display_artist", "display_album"))
    native = {"Version": "1", "TrackCount": str(len(entries)),
              "UnicodeMetadata": "1" if unicode_needed else "0",
              "ExeBytes": str(identity["exe_size"]), "ExeSHA256": identity["exe_sha"]}
    for label, path in (("CacheMpf", cache / "EA_TRAX.mpf"), ("CacheMus", cache / "EA_TRAX.mus"),
                        ("StockMpf", game / "SOUND/PFDATA/MW_Music.mpf"), ("StockMus", game / "SOUND/PFDATA/MW_Music.mus")):
        size, ft = file_stamp(path)
        native[label + "Bytes"] = str(size)
        native[label + "WriteTime"] = str(ft)
    cp["Native"] = native
    for i, (e, m) in enumerate(entries):
        sec = f"Track_{i:03d}"
        cp[sec] = {"Key": e.get("key") or stable_key(e["file"]),
                   "Title": e.get("display_title") or e.get("title") or "Untitled",
                   "Artist": e.get("display_artist") or e.get("artist") or "Custom",
                   "Album": e.get("display_album") or e.get("album") or "",
                   "Mode": e.get("mode", "ALL"), "EventID": hex(m["event"]), "File": e["file"]}
    manifest = cache / "Playlist.ini"
    tmp = cache / "Playlist.tmp"
    with open(tmp, "w", encoding="utf-16", newline="\r\n") as f:
        cp.write(f)
    os.replace(tmp, manifest)
    nm = configparser.ConfigParser(interpolation=None)
    nm.optionxform = str
    nm["NativeMusic"] = {"Version": "2", "LegacyPursuitSources": "0",
                         "VolumeMultiplier": str(settings.get("volume", 1.0)),
                         "TrackCount": str(len(entries)),
                         "MpfSHA256": record["outputs"]["EA_TRAX.mpf"],
                         "MusSHA256": record["outputs"]["EA_TRAX.mus"]}
    for m in record.get("measurements", []):
        nm[f"Source_{m['hash']}_0_{m['event'] - 0xE00000}"] = {"EventID": hex(m["event"]), "Sample": str(m["sample"])}
    nmtmp = cache / "NativeMusic.tmp"
    with open(nmtmp, "w", encoding="ascii") as f:
        nm.write(f, space_around_delimiters=False)
    os.replace(nmtmp, cache / "NativeMusic.ini")
    log(f"Манифест плейлиста: {manifest} ({len(entries)} треков)")
    return manifest



def verify_cache(game, mod, identity, log=log_default):
    cache = Path(mod) / "Cache"
    record = json.loads((cache / "Build.json").read_text(encoding="utf-8"))
    mus_bytes = record.get("mus_bytes")
    mus = cache / "EA_TRAX.mus"
    if mus_bytes and mus.exists() and mus.stat().st_size > mus_bytes:
        with open(mus, "r+b") as f:
            f.truncate(mus_bytes)
    for name, want in record.get("outputs", {}).items():
        got = sha256_file(cache / name)
        if got != want:
            raise RuntimeError(f"Хэш кэша не совпадает: {name}")
    if sha256_file(Path(game) / "SOUND/PFDATA/MW_Music.mpf") != identity["stock_mpf_sha"]:
        raise RuntimeError("Штатный MW_Music.mpf изменился")
    if sha256_file(Path(game) / "SOUND/PFDATA/MW_Music.mus") != identity["stock_mus_sha"]:
        raise RuntimeError("Штатный MW_Music.mus изменился")
    log("Кэш проверен: хэши совпадают, штатные файлы не изменены")



def prepare(game, payload=None, settings=None, log=log_default, progress=None, do_build=True, cancel=None):
    game = Path(game)
    if game_is_running():
        raise RuntimeError("Игра запущена. Закройте speed.exe перед установкой/сборкой.")
    identity = validate_game(game, log)
    mod = extract_payload(payload or default_payload(), game, log)
    if settings is None:
        settings = load_settings(game)
    else:
        save_settings(game, settings)
    ensure_converter(game, log, progress, settings.get("ffmpeg"), cancel=cancel)
    music = game / "CustomMusic"
    lib = sync_library(game, music, settings, log)
    enabled = [e for e in lib.get("tracks", []) if e.get("enabled", True)]
    if len(enabled) > MAX_CUSTOM_TRACKS:
        raise ValueError(f"Слишком много включённых своих треков: {len(enabled)} > {MAX_CUSTOM_TRACKS}")
    stock_on = stock_enabled_count(game)
    if stock_on + len(enabled) > MAX_TOTAL_TRACKS:
        raise ValueError(f"Всего видимых треков {stock_on + len(enabled)} > {MAX_TOTAL_TRACKS}. "
                         "Отключите часть оригинальных или своих треков.")
    built = hit = False
    if do_build:
        try:
            built, hit = build_cache(game, mod, lib, settings, log, progress, cancel=cancel, identity=identity)
        except BuildCancelled:
            # Finalize the committed subset so it is immediately playable.
            if (mod / "Cache" / "Build.json").exists():
                try:
                    write_manifest(game, mod, lib, settings, identity, log, hashes=None)
                    verify_cache(game, mod, identity, log)
                except Exception:
                    pass
            raise
        # Re-read current modes and the folder after the snapshot build. New
        # files have no matching measurement and stay unpublished until the
        # next explicit build; mode changes during the build are respected.
        lib = sync_library(game, music, settings, log)
        write_manifest(game, mod, lib, settings, identity, log)
        verify_cache(game, mod, identity, log)
    return {"game": game, "mod": mod, "music": music, "lib": lib, "settings": settings,
            "identity": identity, "built": built, "hit": hit}



def launch_game(game, log=log_default):
    exe = Path(game) / "speed.exe"
    log(f"Запуск игры: {exe}")
    subprocess.Popen([str(exe)], cwd=str(game),
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def uninstall(game, remove_music=False, remove_old=False, log=log_default):
    """Remove the patch. Optionally delete CustomMusic and old MWEA leftovers."""
    game = Path(game)
    if game_is_running():
        raise RuntimeError("Игра запущена. Закройте speed.exe перед удалением.")
    targets = [game / "scripts" / ASI_NAME, game / "scripts" / MOD_NAME]
    loader = game / LOADER_NAME
    if loader.is_file():
        if is_own_loader(loader):
            targets.append(loader)
        else:
            log(f"Сторонний {LOADER_NAME} оставлен без изменений.")
    if remove_music:
        targets.append(game / "CustomMusic")
    if remove_old:
        targets += [game / "scripts" / OLD_ASI,
                    game / "scripts" / (OLD_ASI + ".disabled"),
                    game / "scripts" / "NFSMWEATraxExpansion"]
    for p in targets:
        if not p.exists():
            continue
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            try:
                p.unlink()
            except OSError:
                pass
        log(f"Удалено: {p}")
    log("Патч удалён. Штатные файлы игры не затрагивались.")


def run_cli(args):
    game = find_game(args.game)
    if not game:
        print("Не найдена папка игры. Укажите --game.")
        return 2
    settings = load_settings(game)
    if args.volume is not None:
        settings["volume"] = args.volume
    if args.native_names:
        settings["native_names"] = True
    if getattr(args, "ffmpeg", None):
        settings["ffmpeg"] = args.ffmpeg
    try:
        if args.uninstall:
            uninstall(game, remove_music=bool(getattr(args, 'remove_music', False)) and not args.keep_music, remove_old=args.remove_old)
            print("Удалено.")
            return 0
        result = prepare(game, args.payload, settings, do_build=not args.no_build)
        if args.launch:
            launch_game(game)
        print("Готово.")
        return 0
    except Exception as ex:
        print(f"ОШИБКА: {ex}", file=sys.stderr)
        return 1


def parse_args():
    ap = argparse.ArgumentParser(description=APP)
    ap.add_argument("--game", help="Папка игры или путь к speed.exe")
    ap.add_argument("--payload", help="payload.zip или папка payload (по умолчанию рядом со скриптом)")
    ap.add_argument("--launch", action="store_true", help="Собрать кэш и запустить игру")
    ap.add_argument("--build", action="store_true", help="Только собрать кэш (без GUI)")
    ap.add_argument("--no-build", action="store_true", help="Установить без сборки кэша")
    ap.add_argument("--volume", type=float, help=argparse.SUPPRESS)
    ap.add_argument("--native-names", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--ffmpeg", help="Путь к своему ffmpeg.exe (иначе скачается в папку temp)")
    ap.add_argument("--cli", action="store_true", help="Без GUI")
    ap.add_argument("--uninstall", action="store_true", help="Удалить патч")
    ap.add_argument("--keep-music", action="store_true", help="При удалении оставить CustomMusic (по умолчанию)")
    ap.add_argument("--remove-music", action="store_true", help="При удалении явно удалить и папку CustomMusic")
    ap.add_argument("--remove-old", action="store_true", help="При удалении убрать старый MWEA")
    return ap.parse_args()


# ---------------------------------------------------------------------------
# Tkinter GUI
# ---------------------------------------------------------------------------
def set_preview_session_volume(value):
    """Set only this process's Windows audio sessions, not the game or master volume."""
    if os.name != "nt":
        return False
    import ctypes
    import uuid
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("data1", ctypes.c_uint32), ("data2", ctypes.c_uint16),
                    ("data3", ctypes.c_uint16), ("data4", ctypes.c_ubyte * 8)]
        @classmethod
        def parse(cls, text):
            return cls.from_buffer_copy(uuid.UUID(text).bytes_le)

    ole = ctypes.windll.ole32
    ole.CoInitializeEx.argtypes = (ctypes.c_void_p, wintypes.DWORD)
    ole.CoInitializeEx.restype = ctypes.c_long
    init = ole.CoInitializeEx(None, 2)
    if init < 0 and (init & 0xffffffff) != 0x80010106:
        return False
    uninit = init >= 0
    ptrs = []
    def method(obj, index, restype, *types):
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *types)(vtable[index])
    def check(hr):
        if hr < 0:
            raise OSError(f"Core Audio HRESULT {hr & 0xffffffff:08x}")
    def remember(ptr):
        ptrs.append(ptr)
        return ptr
    try:
        clsid = GUID.parse('BCDE0395-E52F-467C-8E3D-C4579291692E')
        iid = GUID.parse('A95664D2-9614-4F35-A746-DE8DB63617E6')
        mgr_iid = GUID.parse('77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F')
        ctl2_iid = GUID.parse('BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D')
        simple_iid = GUID.parse('87CE5498-68D6-44E5-9215-6DA47EF883D8')
        ole.CoCreateInstance.argtypes = (ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD,
                                         ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
        ole.CoCreateInstance.restype = ctypes.c_long
        enumerator = ctypes.c_void_p()
        check(ole.CoCreateInstance(ctypes.byref(clsid), None, 23, ctypes.byref(iid), ctypes.byref(enumerator)))
        remember(enumerator)
        changed = False
        # Default multimedia and console devices can differ.
        for role in (1, 0):
            dev = ctypes.c_void_p()
            hr = method(enumerator, 4, ctypes.c_long, ctypes.c_int, ctypes.c_int,
                        ctypes.POINTER(ctypes.c_void_p))(enumerator, 0, role, ctypes.byref(dev))
            if hr < 0:
                continue
            remember(dev)
            manager = ctypes.c_void_p()
            check(method(dev, 3, ctypes.c_long, ctypes.POINTER(GUID), wintypes.DWORD,
                         ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
                             dev, ctypes.byref(mgr_iid), 23, None, ctypes.byref(manager)))
            remember(manager)
            sessions = ctypes.c_void_p()
            check(method(manager, 5, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))(
                manager, ctypes.byref(sessions)))
            remember(sessions)
            count = ctypes.c_int()
            check(method(sessions, 3, ctypes.c_long, ctypes.POINTER(ctypes.c_int))(
                sessions, ctypes.byref(count)))
            for i in range(count.value):
                ctl = ctypes.c_void_p()
                check(method(sessions, 4, ctypes.c_long, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(
                    sessions, i, ctypes.byref(ctl)))
                remember(ctl)
                ctl2 = ctypes.c_void_p()
                hr = method(ctl, 0, ctypes.c_long, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(
                    ctl, ctypes.byref(ctl2_iid), ctypes.byref(ctl2))
                if hr < 0:
                    continue
                remember(ctl2)
                pid = wintypes.DWORD()
                check(method(ctl2, 14, ctypes.c_long, ctypes.POINTER(wintypes.DWORD))(ctl2, ctypes.byref(pid)))
                if pid.value != os.getpid():
                    continue
                simple = ctypes.c_void_p()
                hr = method(ctl, 0, ctypes.c_long, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(
                    ctl, ctypes.byref(simple_iid), ctypes.byref(simple))
                if hr < 0:
                    continue
                remember(simple)
                check(method(simple, 3, ctypes.c_long, ctypes.c_float, ctypes.c_void_p)(
                    simple, max(0.0, min(1.0, float(value))), None))
                changed = True
        return changed
    finally:
        for obj in reversed(ptrs):
            method(obj, 2, ctypes.c_ulong)(obj)
        if uninit:
            ole.CoUninitialize()


class NativeFileDrop:
    """Windows Explorer file drops, no third-party Tk extension required."""
    def __init__(self, root, deliver):
        import ctypes
        from ctypes import wintypes
        self.root, self.deliver = root, deliver
        self.ctypes = ctypes
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.shell = ctypes.WinDLL('shell32', use_last_error=True)
        self.controls = ctypes.WinDLL('comctl32', use_last_error=True)
        self.active = set()
        pointer = ctypes.c_size_t
        result = ctypes.c_ssize_t
        proto = ctypes.WINFUNCTYPE(result, wintypes.HWND, wintypes.UINT, pointer, ctypes.c_ssize_t, pointer, pointer)
        self.callback = proto(self._callback)
        self.controls.SetWindowSubclass.argtypes = [wintypes.HWND, proto, pointer, pointer]
        self.controls.SetWindowSubclass.restype = wintypes.BOOL
        self.controls.RemoveWindowSubclass.argtypes = [wintypes.HWND, proto, pointer]
        self.controls.RemoveWindowSubclass.restype = wintypes.BOOL
        self.controls.DefSubclassProc.argtypes = [wintypes.HWND, wintypes.UINT, pointer, ctypes.c_ssize_t]
        self.controls.DefSubclassProc.restype = result
        self.shell.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
        self.shell.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
        self.shell.DragQueryFileW.restype = wintypes.UINT
        self.shell.DragFinish.argtypes = [wintypes.HANDLE]
        self.user.GetParent.argtypes = [wintypes.HWND]
        self.user.GetParent.restype = wintypes.HWND
        enumproc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, ctypes.c_ssize_t)
        self.user.EnumChildWindows.argtypes = [wintypes.HWND, enumproc, ctypes.c_ssize_t]
        self.user.EnumChildWindows.restype = wintypes.BOOL
        self.enum_callback = enumproc(self._attach_child)
        self.closed = False
        self.root.after(250, self._refresh)

    def _attach_child(self, hwnd, _):
        hwnd = int(hwnd)
        if hwnd not in self.active and self.controls.SetWindowSubclass(hwnd, self.callback, 1, 0):
            self.active.add(hwnd)
            self.shell.DragAcceptFiles(hwnd, True)
        return True

    def _refresh(self):
        if self.closed: return
        try:
            # Tk exposes its inner frame; the parent is the native top-level.
            inner = self.root.winfo_id()
            top = self.user.GetParent(inner) or inner
            self._attach_child(top, 0)
            self.user.EnumChildWindows(top, self.enum_callback, 0)
            self.root.after(2000, self._refresh)
        except Exception:
            pass

    def _callback(self, hwnd, msg, wparam, lparam, _ident, _data):
        if msg == 0x0233:  # WM_DROPFILES
            try:
                count = self.shell.DragQueryFileW(wparam, 0xFFFFFFFF, None, 0)
                paths = []
                for i in range(count):
                    size = self.shell.DragQueryFileW(wparam, i, None, 0)
                    buf = self.ctypes.create_unicode_buffer(size + 1)
                    if self.shell.DragQueryFileW(wparam, i, buf, size + 1):
                        paths.append(buf.value)
                if paths: self.deliver(paths)
            except Exception:
                pass
            finally:
                self.shell.DragFinish(wparam)
            return 0
        if msg == 0x0082:  # WM_NCDESTROY
            self.active.discard(int(hwnd))
        return self.controls.DefSubclassProc(hwnd, msg, wparam, lparam)

    def close(self):
        self.closed = True
        for hwnd in tuple(self.active):
            self.shell.DragAcceptFiles(hwnd, False)
            self.controls.RemoveWindowSubclass(hwnd, self.callback, 1)
        self.active.clear()


def run_gui(args):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    from tkinter import font as tkfont
    from tkinter.scrolledtext import ScrolledText

    # Palette transcribed from the HTML mockup (dark grunge + yellow).
    BG, PANEL, CARD, LINE = "#080a09", "#171a19", "#1a1e1b", "#454944"
    PANEL_HI, PANEL_LO, LINE_HI = "#202421", "#151917", "#555b53"
    FG, MUTED, OFFC = "#eeefe9", "#92958f", "#8a8d86"
    HINT, COUNTER = "#a1a39b", "#b0b3a9"
    ACCENT, ACCENT2, ACCENT3, DANGER = "#ffd21c", "#ffe34b", "#eab500", "#d6d6ca"
    FIELD, FIELD_LINE = "#0c0f0e", "#4a4e49"
    HEAD_BG, HEAD_FG, HEAD_LINE = "#303530", "#c3c6bb", "#7a7e6c"
    SEL_BG, ROW_LINE = "#33301a", "#2c302b"
    LOG_BG, LOG_LINE, LOG_FG = "#0b0e0d", "#4b5049", "#aeb1a8"
    STATUS_BG, STATUS_LINE, STATUS_FG = "#101412", "#53594f", "#a8aba2"
    DOT = "#b8d36a"
    STATUS_RED = "#e0685a"
    FAM = "Arial" if os.name == "nt" else "Liberation Sans"
    MONO = "Consolas" if os.name == "nt" else "DejaVu Sans Mono"
    FONT, FONT_SM = (FAM, 9), (FAM, 8)
    FONT_BOLD, FONT_TITLE = (FAM, 9, "bold"), (FAM, 15, "bold")
    FONT_BTN, FONT_BTN_B = (FAM, 8), (FAM, 8, "bold")
    FONT_LAUNCH, FONT_MONO = (FAM, 9, "bold"), (MONO, 8)
    MODE_TEXT = {"ALL": "Везде", "FE": "Меню / гараж", "IG": "Гонка / заезд", "OFF": "Выключен"}
    MODE_ORDER = ("ALL", "FE", "IG", "OFF")
    STATUS_TEXT = {m: MODE_TEXT[m].upper() for m in MODE_ORDER}

    def setup_style(root):
        st = ttk.Style(root)
        try:
            st.theme_use("clam")
        except Exception:
            pass
        root.configure(bg=BG)
        st.configure(".", background=BG, foreground=FG, fieldbackground=FIELD,
                     bordercolor=LINE, lightcolor=CARD, darkcolor=CARD, font=FONT)
        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=CARD)
        st.configure("TLabel", background=BG, foreground=FG, font=FONT)
        st.configure("Muted.TLabel", background=BG, foreground=MUTED, font=FONT_SM)
        st.configure("CardMuted.TLabel", background=CARD, foreground=MUTED, font=FONT_SM)
        st.configure("Accent.TLabel", background=BG, foreground=ACCENT, font=FONT_BOLD)
        st.configure("Title.TLabel", background=BG, foreground=FG, font=FONT_TITLE)
        st.configure("TButton", background="#252a27", foreground=FG, borderwidth=1,
                     focusthickness=0, padding=(10, 7), font=FONT_BTN, relief="solid",
                     bordercolor=LINE_HI)
        st.map("TButton", background=[("active", "#373c37"), ("disabled", PANEL)],
               foreground=[("disabled", OFFC)], bordercolor=[("active", ACCENT)])
        st.configure("Accent.TButton", background=ACCENT3, foreground="#17170e",
                     font=FONT_BTN_B, padding=(13, 9), borderwidth=1, relief="solid",
                     bordercolor=ACCENT2)
        st.map("Accent.TButton", background=[("active", ACCENT2), ("disabled", LINE)],
               foreground=[("disabled", MUTED)])
        st.configure("Danger.TButton", background="#171a18", foreground=DANGER,
                     padding=(11, 8), borderwidth=1, relief="solid", bordercolor="#64675d")
        st.map("Danger.TButton", background=[("active", "#232722"), ("disabled", PANEL)],
               foreground=[("disabled", OFFC)])
        for name in ("Success.TButton", "Neutral.TButton", "Info.TButton", "Small.TButton"):
            st.configure(name, background="#252a27", foreground=FG, padding=(10, 7),
                         borderwidth=1, relief="solid", bordercolor=LINE_HI, font=FONT_BTN)
            st.map(name, background=[("active", "#373c37"), ("disabled", PANEL)],
                   foreground=[("disabled", OFFC)], bordercolor=[("active", ACCENT)])
        st.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 0))
        st.configure("TNotebook.Tab", background="#131615", foreground="#a7aaa2",
                     padding=(17, 10), font=FONT_BTN_B, borderwidth=0)
        st.map("TNotebook.Tab", background=[("selected", "#242822")],
               foreground=[("selected", FG)])
        st.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=FG,
                     rowheight=30, borderwidth=0, font=FONT)
        st.configure("Treeview.Heading", background=HEAD_BG, foreground=HEAD_FG,
                     font=FONT_BTN_B, relief="flat", padding=(10, 8), bordercolor=HEAD_LINE)
        st.map("Treeview", background=[("selected", SEL_BG)], foreground=[("selected", FG)])
        st.map("Treeview.Heading", background=[("active", "#3a403a")])
        st.configure("TEntry", fieldbackground=FIELD, foreground="#d1d2c9",
                     bordercolor=FIELD_LINE, insertcolor=FG, padding=6)
        st.map("TEntry", fieldbackground=[("readonly", FIELD)],
               foreground=[("readonly", "#d1d2c9")])
        st.configure("Vertical.TScrollbar", background="#252a27", troughcolor=BG,
                     bordercolor=BG, arrowcolor=MUTED, relief="flat")
        st.map("Vertical.TScrollbar", background=[("active", ACCENT3)])
        st.configure("TCheckbutton", background=BG, foreground=FG, font=FONT)
        st.map("TCheckbutton", background=[("active", BG)], foreground=[("selected", ACCENT)])
        st.configure("TCombobox", fieldbackground=FIELD, background="#252a27",
                     foreground=FG, arrowcolor=FG, bordercolor=FIELD_LINE, padding=5)
        st.configure("TLabelframe", background=BG, bordercolor=LINE)
        st.configure("TLabelframe.Label", background=BG, foreground=MUTED, font=FONT_SM)
        root.option_add("*TCombobox*Listbox.background", FIELD)
        root.option_add("*TCombobox*Listbox.foreground", FG)
        root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        root.option_add("*TCombobox*Listbox.selectForeground", "#17170e")
        root.option_add("*Menu.background", CARD)
        root.option_add("*Menu.foreground", FG)
        root.option_add("*Menu.activeBackground", ACCENT)
        root.option_add("*Menu.activeForeground", "#17170e")
        return st

    def use_dark_native_title(window):
        """Optional DWM color hint only; keep all native window styles intact."""
        if os.name != "nt" or not window.winfo_exists():
            return
        try:
            import ctypes
            from ctypes import wintypes
            user = ctypes.windll.user32
            user.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
            user.GetAncestor.restype = wintypes.HWND
            hwnd = user.GetAncestor(wintypes.HWND(window.winfo_id()), 2)
            dwm = ctypes.windll.dwmapi.DwmSetWindowAttribute
            dwm.argtypes = (wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD)
            dwm.restype = ctypes.c_long
            dark = ctypes.c_int(1)
            if dwm(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark)) != 0:
                dwm(hwnd, 19, ctypes.byref(dark), ctypes.sizeof(dark))
            # Windows 11 can set caption/text colors explicitly. Windows 10
            # ignores unsupported attributes; the standard frame remains intact.
            for attribute, rgb in ((35, 0x00171410), (36, 0x00dce6e5)):
                value = ctypes.c_uint32(rgb)
                dwm(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value))
        except Exception:
            pass

    # ---------------- branding: logo + photo background ----------------
    # Tk widgets are opaque, so the photo is drawn on a root Canvas and every
    # "glass" container re-shows the same photo, offset to line up with it. The
    # result is a see-through UI over the car picture with no solid fills.
    HEADER_H = 122

    def _brand_images(root):
        here = Path(__file__).resolve().parent

        def load(b64, fallback):
            if b64:
                try:
                    return tk.PhotoImage(master=root, data=b64)
                except Exception:
                    pass
            p = here / "assets" / fallback
            if p.exists():
                try:
                    return tk.PhotoImage(master=root, file=str(p))
                except Exception:
                    pass
            return None

        logo = load(EMBEDDED_LOGO_B64, "logo_white.png")
        bg = load(EMBEDDED_BACKGROUND_B64, "bg_rgb.png")
        if bg is None:
            bg = tk.PhotoImage(master=root, width=1120, height=740)
            bg.put(BG, to=(0, 0, 1120, 740))
        return logo, bg

    class _Background:
        """A fixed, unscaled top-left image, clipped by the window."""
        def __init__(self, root, canvas, photo):
            self.root = root
            self.canvas = canvas
            self.master = photo
            self.scaled = photo
            self.ox = self.oy = 0
            self.item = canvas.create_image(0, 0, anchor="nw", image=photo)
            canvas.tag_lower(self.item)
            self.glass = []

        def register(self, widget):
            self.glass.append(widget)

        def resize(self, w, h):
            # Never resample: preserve source pixels and crop beyond window edges.
            self.queue_sync()

        def queue_sync(self):
            # Coalesce the flood of <Configure> events during a window drag into
            # a single glass relayout per idle cycle (instead of per pixel).
            if getattr(self, "_sync_pending", False):
                return
            self._sync_pending = True
            try:
                self.canvas.after(60, self._sync_all)
            except Exception:
                self._sync_pending = False

        def _sync_all(self):
            self._sync_pending = False
            for g in self.glass:
                try:
                    # The widgets' relative geometry does not change when the
                    # entire window is moved. Avoid reassigning large images.
                    x, y = self.offset_for(g)
                    key = (x, y, g.winfo_width(), g.winfo_height(), str(self.scaled))
                    if getattr(g, "_last_background_geometry", None) == key:
                        continue
                    g._sync_bg()
                    g._last_background_geometry = key
                except Exception:
                    pass

        def offset_for(self, widget):
            fx = widget.winfo_rootx() - self.canvas.winfo_rootx()
            fy = widget.winfo_rooty() - self.canvas.winfo_rooty()
            return self.ox - fx, self.oy - fy

    class GlassFrame(tk.Frame):
        """A transparent frame: shows the shared photo, clipped to its bounds.

        With ``tint=(color, stipple)`` a dithered overlay darkens the photo so the
        car still shows through (e.g. gray50 ~= 50% transparent)."""
        def __init__(self, master, bgman, tint=None, **kw):
            super().__init__(master, bg=BG, highlightthickness=0, bd=0)
            self.bgman = bgman
            self._bgcv = tk.Canvas(self, bg=BG, highlightthickness=0, bd=0)
            self._bgcv.place(x=0, y=0, relwidth=1, relheight=1)
            self._bgitem = self._bgcv.create_image(0, 0, anchor="nw", image=bgman.scaled)
            self._tintitem = None
            if tint:
                self._tintitem = self._bgcv.create_rectangle(
                    0, 0, 0, 0, fill=tint[0], outline="", stipple=tint[1])
            self._bgcv.tk.call("lower", self._bgcv._w)
            bgman.register(self)
            self.bind("<Configure>", lambda e: self.bgman.queue_sync())

        def _sync_bg(self, event=None):
            try:
                x, y = self.bgman.offset_for(self)
                self._bgcv.itemconfigure(self._bgitem, image=self.bgman.scaled)
                self._bgcv.coords(self._bgitem, x, y)
                if self._tintitem is not None:
                    self._bgcv.coords(self._tintitem, 0, 0,
                                      self.winfo_width(), self.winfo_height())
                self._bgcv.tk.call("lower", self._bgcv._w)
            except Exception:
                pass

    class GlassLabel(tk.Canvas):
        """Transparent text drawn straight over the photo (no solid rectangle)."""
        def __init__(self, master, bgman, text="", font=None, fg=FG, padding=(0, 0, 0, 0),
                     wrap=False, tint=None, **kw):
            super().__init__(master, highlightthickness=0, bd=0, bg=BG)
            self.bgman = bgman
            self._font = font or FONT
            self._fg = fg
            self._pad = padding
            self._wrap = wrap
            self._bgitem = self.create_image(0, 0, anchor="nw", image=bgman.scaled)
            self._tintitem = (self.create_rectangle(0, 0, 0, 0, fill=tint[0], outline="", stipple=tint[1])
                              if tint else None)
            self._txt = self.create_text(padding[0], padding[1], anchor="nw",
                                         text=text, font=self._font, fill=fg)
            bgman.register(self)
            self._fit()
            self.bind("<Configure>", lambda e: self.bgman.queue_sync())

        def _fit(self):
            bb = self.bbox(self._txt) or (0, 0, 1, 1)
            h = max(1, bb[3] + self._pad[3])
            if self._wrap:
                tk.Canvas.configure(self, height=h)
            else:
                tk.Canvas.configure(self, width=max(1, bb[2] + self._pad[2]), height=h)

        def set_wrap_width(self, w):
            w = int(w)
            if self._wrap and w > 0 and getattr(self, "_wrap_width", None) != w:
                self._wrap_width = w
                self.itemconfigure(self._txt, width=w)
                self._fit()

        def configure(self, **kw):
            if "text" in kw:
                self.itemconfigure(self._txt, text=kw.pop("text"))
                self._fit()
            if "foreground" in kw or "fg" in kw:
                self._fg = kw.pop("foreground", None) or kw.pop("fg", self._fg)
                self.itemconfigure(self._txt, fill=self._fg)
            if "font" in kw:
                self._font = kw.pop("font")
                self.itemconfigure(self._txt, font=self._font)
                self._fit()
            if kw:
                tk.Canvas.configure(self, **kw)

        config = configure

        def _sync_bg(self, event=None):
            try:
                x, y = self.bgman.offset_for(self)
                self.itemconfigure(self._bgitem, image=self.bgman.scaled)
                self.coords(self._bgitem, x, y)
                if self._tintitem is not None:
                    self.coords(self._tintitem, 0, 0, self.winfo_width(), self.winfo_height())
                self.tag_lower(self._bgitem)
            except Exception:
                pass

    # ---------------- mockup-style custom widgets ----------------
    def _mix(c1, c2, t):
        a = tuple(int(c1[i:i + 2], 16) for i in (1, 3, 5))
        b = tuple(int(c2[i:i + 2], 16) for i in (1, 3, 5))
        return "#%02x%02x%02x" % tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))

    BTN_VARIANTS = {
        "Tool.TButton":    dict(top="#373c37", bot="#252a27", border="#555c54", fg="#e6e8df",
                                hover_border=ACCENT, font=FONT_BTN, pad=(10, 7, 10, 7), inset=True),
        "Success.TButton": dict(top="#373c37", bot="#252a27", border="#555c54", fg="#e6e8df",
                                hover_border=ACCENT, font=FONT_BTN, pad=(10, 7, 10, 7), inset=True),
        "Neutral.TButton": dict(top="#373c37", bot="#252a27", border="#555c54", fg="#e6e8df",
                                hover_border=ACCENT, font=FONT_BTN, pad=(10, 7, 10, 7), inset=True),
        "Info.TButton":    dict(top="#373c37", bot="#252a27", border="#555c54", fg="#e6e8df",
                                hover_border=ACCENT, font=FONT_BTN, pad=(10, 7, 10, 7), inset=True),
        "Small.TButton":   dict(top="#373c37", bot="#252a27", border="#555c54", fg="#e6e8df",
                                hover_border=ACCENT, font=FONT_BTN, pad=(8, 6, 8, 6), inset=True),
        "Pick.TButton":    dict(top="#3a3e39", bot="#202321", border="#656960", fg="#eeeeee",
                                hover_border=ACCENT, font=FONT_BTN, pad=(13, 8, 13, 8)),
        "Journal.TButton": dict(top="#171b19", bot="#171b19", border="#555a51", fg="#d0d2c9",
                                hover_border=ACCENT, font=FONT_BTN, pad=(12, 8, 12, 8)),
        "Build.TButton":   dict(top="#333831", bot="#1d221f", border="#686e62", fg="#e2e4d9",
                                hover_border=ACCENT, font=FONT_BTN_B, pad=(13, 9, 13, 9)),
        "Accent.TButton":  dict(top=ACCENT2, bot=ACCENT3, border="#fff080", fg="#17170e",
                                hover_bright=True, font=FONT_LAUNCH, pad=(15, 10, 15, 10)),
        "Danger.TButton":  dict(top="#171a18", bot="#171a18", border="#64675d", fg="#d6d6ca",
                                hover_border=ACCENT, font=FONT_BTN, pad=(11, 8, 11, 8), clip=True),
        "Stop.TButton":    dict(top="#2a1512", bot="#1c0f0d", border=STATUS_RED, fg="#f0a89e",
                                hover_border="#ff8a7c", font=FONT_BTN_B, pad=(13, 9, 13, 9)),
        "Tab.TButton":     dict(top="#131615", bot="#131615", border="#4a4e49", fg="#a7aaa2",
                                hover_border=ACCENT, font=FONT_BTN_B, pad=(17, 10, 17, 10)),
        "TabActive.TButton": dict(top="#30342e", bot="#1c201d", border="#a2a28b", fg="#ffffff",
                                font=FONT_BTN_B, pad=(17, 10, 17, 10), corner=True),
    }

    class MButton(tk.Canvas):
        """Canvas button with gradient fill, border, hover/pressed/disabled states."""
        def __init__(self, master, text="", command=None, style="Tool.TButton",
                     font=None, padding=None, bgman=None, **kw):
            self._style = style
            self._text = text
            self._command = command
            self._state = "normal"
            self._bgman = bgman
            self._hover = self._pressed = self._focus = False
            v = BTN_VARIANTS.get(style, BTN_VARIANTS["Tool.TButton"])
            self._font = font or v["font"]
            self._pad = padding or v["pad"]
            self._fontobj = tkfont.Font(font=self._font)
            w = self._fontobj.measure(text) + self._pad[0] + self._pad[2]
            h = self._fontobj.metrics("linespace") + self._pad[1] + self._pad[3]
            try:
                parent_bg = master.cget("bg")
            except Exception:
                parent_bg = BG
            super().__init__(master, width=w, height=h, bg=parent_bg,
                             highlightthickness=0, bd=0, takefocus=1, cursor="hand2")
            self._req = (w, h)
            self.bind("<Configure>", lambda e: self._queue_draw())
            self.bind("<Enter>", self._on_enter)
            self.bind("<Leave>", self._on_leave)
            self.bind("<ButtonPress-1>", self._on_press)
            self.bind("<ButtonRelease-1>", self._on_release)
            self.bind("<FocusIn>", self._on_focus)
            self.bind("<FocusOut>", self._on_blur)
            self.bind("<KeyPress-Return>", lambda e: self.invoke())
            self.bind("<KeyPress-space>", lambda e: self.invoke())
            if bgman is not None and BTN_VARIANTS.get(style, {}).get("clip"):
                bgman.register(self)
            self._draw()

        def _queue_draw(self):
            if getattr(self, "_draw_pending", False):
                return
            self._draw_pending = True
            try:
                self.after_idle(self._draw_now)
            except Exception:
                self._draw_pending = False

        def _draw_now(self):
            self._draw_pending = False
            self._draw()

        def _sync_bg(self):
            self._queue_draw()

        def cget(self, key):
            if key == "text":
                return self._text
            if key == "style":
                return self._style
            if key == "state":
                return self._state
            if key == "command":
                return self._command
            return super().cget(key)

        def configure(self, cnf=None, **kw):
            if cnf:
                kw.update(cnf)
            resize = redraw = False
            if "text" in kw:
                self._text = kw.pop("text"); resize = True
            if "command" in kw:
                self._command = kw.pop("command")
            if "style" in kw:
                self._style = kw.pop("style"); resize = redraw = True
            if "state" in kw:
                self._state = kw.pop("state"); redraw = True
                super().configure(cursor="" if self._state == "disabled" else "hand2",
                                  takefocus=0 if self._state == "disabled" else 1)
            if "font" in kw:
                self._font = kw.pop("font"); self._fontobj = tkfont.Font(font=self._font); resize = True
            if kw:
                super().configure(**kw)
            if resize:
                self._resize()
            if resize or redraw:
                self._draw()

        config = configure

        def _resize(self):
            w = self._fontobj.measure(self._text) + self._pad[0] + self._pad[2]
            h = self._fontobj.metrics("linespace") + self._pad[1] + self._pad[3]
            self._req = (w, h)
            super().configure(width=w, height=h)

        def invoke(self):
            if self._state != "disabled" and self._command:
                self._command()

        def _on_enter(self, e):
            self._hover = True; self._draw()

        def _on_leave(self, e):
            self._hover = self._pressed = False; self._draw()

        def _on_press(self, e):
            if self._state != "disabled":
                self._pressed = True; self._draw()

        def _on_release(self, e):
            was = self._pressed
            self._pressed = False; self._draw()
            if was and self._hover:
                self.invoke()

        def _on_focus(self, e):
            self._focus = True; self._draw()

        def _on_blur(self, e):
            self._focus = False; self._draw()

        def _draw(self):
            self.delete("all")
            w, h = self.winfo_width(), self.winfo_height()
            if w <= 1 or h <= 1:
                w, h = self._req
            v = BTN_VARIANTS.get(self._style, BTN_VARIANTS["Tool.TButton"])
            if self._bgman is not None and v.get("clip"):
                try:
                    x, y = self._bgman.offset_for(self)
                    self.create_image(x, y, anchor="nw", image=self._bgman.scaled)
                except Exception:
                    pass
            top, bot, border, fg = v["top"], v.get("bot", v["top"]), v["border"], v["fg"]
            if self._state == "disabled":
                top = bot = PANEL; border = LINE; fg = OFFC
            elif self._pressed:
                top = _mix(top, "#000000", 0.18); bot = _mix(bot, "#000000", 0.18)
            elif self._hover:
                if v.get("hover_bright"):
                    top = _mix(top, "#ffffff", 0.10); bot = _mix(bot, "#ffffff", 0.10)
                border = v.get("hover_border", border)
            if self._focus and self._state != "disabled":
                border = ACCENT
            if v.get("clip"):
                c = 7
                poly = [c, 0, w, 0, w, h - c, w - c, h, 0, h, 0, c]
                self.create_polygon(poly, fill=top, outline="")
                self.create_polygon(poly, fill="", outline=border, width=1)
            else:
                steps = min(12, max(1, h))
                bh = h / steps
                for i in range(steps):
                    col = _mix(top, bot, i / max(1, steps - 1))
                    self.create_rectangle(0, int(i * bh), w, int((i + 1) * bh) + 1,
                                          fill=col, outline="")
                self.create_rectangle(0, 0, w - 1, h - 1, outline=border, width=1)
                if v.get("inset"):
                    self.create_line(1, 1, w - 1, 1, fill=_mix(top, "#ffffff", 0.12))
            if v.get("corner") and self._state != "disabled":
                self.create_line(0, 12, 0, 0, 12, 0, fill=ACCENT, width=2)
            self.create_text(w / 2, h / 2, text=self._text, font=self._font, fill=fg)

    class PlayerButton(MButton):
        """A single transport button with a drawn play/pause icon, not font glyphs."""
        def __init__(self, master, **kw):
            self.playing = False
            super().__init__(master, text="", padding=(15, 7, 15, 7), **kw)

        def set_playing(self, playing):
            self.playing = playing
            self._draw()

        def _draw(self):
            super()._draw()
            w, h = self.winfo_width(), self.winfo_height()
            if w <= 1 or h <= 1:
                w, h = self._req
            x, y = w / 2, h / 2
            if self.playing:
                self.create_rectangle(x - 3.5, y - 4.2, x - 1.4, y + 4.2, fill=FG, outline="")
                self.create_rectangle(x + 1.4, y - 4.2, x + 3.5, y + 4.2, fill=FG, outline="")
            else:
                self.create_polygon(x - 2.8, y - 4.9, x - 2.8, y + 4.9, x + 4.2, y, fill=FG, outline="")

    class IconButton(MButton):
        """Same-size transport and reorder buttons with crisp vector icons."""
        def __init__(self, master, icon, **kw):
            self.icon = icon
            super().__init__(master, text="", padding=(15, 7, 15, 7), **kw)

        def _draw(self):
            super()._draw()
            w, h = self.winfo_width(), self.winfo_height()
            if w <= 1 or h <= 1:
                w, h = self._req
            x, y = w / 2, h / 2
            col = OFFC if self._state == "disabled" else FG
            if self.icon == "stop":
                self.create_rectangle(x - 4.2, y - 4.2, x + 4.2, y + 4.2, fill=col, outline="")
            elif self.icon == "folder":
                self.create_polygon(x-7, y-5, x-2, y-5, x, y-3, x+7, y-3,
                                    x+7, y+5, x-7, y+5, fill="", outline=col, width=1.4)
                self.create_line(x-7, y-1.5, x+7, y-1.5, fill=col, width=1.2)
            elif self.icon == "volume":
                self.create_polygon(x - 7, y - 3, x - 4, y - 3, x, y - 6,
                                    x, y + 6, x - 4, y + 3, x - 7, y + 3,
                                    fill=col, outline="")
                self.create_arc(x - 1, y - 6, x + 8, y + 6, start=290, extent=140,
                                style="arc", outline=col, width=1.5)
            else:
                direction = -1 if self.icon == "up" else 1
                self.create_line(x, y - direction * 4.2, x, y + direction * 4.2,
                                 fill=col, width=1.5)
                self.create_line(x - 3.5, y + direction * 0.7, x, y + direction * 4.2,
                                 x + 3.5, y + direction * 0.7, fill=col, width=1.5)

    class LaunchButton(MButton):
        def _draw(self):
            super()._draw()
            w, h = self.winfo_width(), self.winfo_height()
            if w <= 1 or h <= 1:
                w, h = self._req
            label = self._text.strip()
            text_width = self._fontobj.measure(label)
            x = max(8, (w - (7 + 7 + text_width)) / 2)
            color = OFFC if self._state == "disabled" else "#17170e"
            for item in self.find_all():
                if self.type(item) == "text":
                    self.itemconfigure(item, text=label, anchor="w")
                    self.coords(item, x + 14, h / 2)
            self.create_polygon(x, h / 2 - 4.2, x, h / 2 + 4.2, x + 7, h / 2,
                                fill=color, outline="")

    class PlayerTimeline(tk.Canvas):
        """Timeline painted directly over the same background as the player."""
        def __init__(self, master, bgman):
            super().__init__(master, height=20, bg=BG, highlightthickness=0, bd=0, cursor="hand2")
            self.bgman = bgman
            self.bgitem = self.create_image(0, 0, anchor="nw", image=bgman.scaled, tags="photo")
            bgman.register(self)

        def _sync_bg(self):
            x, y = self.bgman.offset_for(self)
            self.itemconfigure(self.bgitem, image=self.bgman.scaled)
            self.coords(self.bgitem, x, y)
            self.tag_lower(self.bgitem)

    class PanelFrame(tk.Frame):
        """Glass panel (~50% transparent over the photo) + mockup corner marks."""
        def __init__(self, master, bgman=None, **kw):
            super().__init__(master, bg=PANEL_LO, bd=0, highlightthickness=0)
            self.bgman = bgman
            self._bg = tk.Canvas(self, bg=PANEL_LO, highlightthickness=0, bd=0)
            self._bg.place(x=0, y=0, relwidth=1, relheight=1)
            self._bgimg = None
            self._overlay = None
            self._outline = None
            self._corner1 = self._corner2 = None
            if bgman is not None:
                self._bgimg = self._bg.create_image(0, 0, anchor="nw", image=bgman.scaled)
                bgman.register(self)
            self._bg.tk.call("lower", self._bg._w)
            self.bind("<Configure>", lambda e: self._queue_draw())

        def _queue_draw(self):
            if getattr(self, "_draw_pending", False):
                return
            self._draw_pending = True
            try:
                self.after_idle(self._draw_now)
            except Exception:
                self._draw_pending = False

        def _draw_now(self):
            self._draw_pending = False
            self._draw()

        def _sync_bg(self):
            self._queue_draw()

        def _draw(self, event=None):
            c = self._bg
            w, h = self.winfo_width(), self.winfo_height()
            if w <= 1 or h <= 1:
                return
            if self.bgman is not None:
                x, y = self.bgman.offset_for(self)
                c.coords(self._bgimg, x, y)
                if self._overlay is None:
                    self._overlay = c.create_rectangle(0, 0, w, h, fill=PANEL_LO,
                                                       outline="", stipple="gray50")
                else:
                    c.coords(self._overlay, 0, 0, w, h)
            if self._outline is None:
                self._outline = c.create_rectangle(0, 0, w - 1, h - 1, outline=LINE_HI)
                self._corner1 = c.create_line(w - 18, 0, w - 1, 0, w - 1, 17, fill=ACCENT, width=2)
                self._corner2 = c.create_line(0, h - 18, 0, h - 1, 17, h - 1, fill="#a3a79a", width=2)
            else:
                c.coords(self._outline, 0, 0, w - 1, h - 1)
                c.coords(self._corner1, w - 18, 0, w - 1, 0, w - 1, 17)
                c.coords(self._corner2, 0, h - 18, 0, h - 1, 17, h - 1)

    class MockNotebook(tk.Frame):
        """Tab strip + shared panel body, matching the mockup's active-tab corner."""
        def __init__(self, master, bgman=None):
            super().__init__(master, bg=BG, bd=0, highlightthickness=0)
            self.tabbar = tk.Frame(self, bg=BG)
            self.tabbar.pack(fill="x")
            self.body = tk.Frame(self, bg=BG)
            self.body.pack(fill="both", expand=True)
            self.pages = []
            self.tabs = []
            self._sel = 0

        def add(self, page, text=""):
            i = len(self.pages)
            self.pages.append(page)
            b = MButton(self.tabbar, text=text, style="Tab.TButton",
                        command=lambda i=i: self.select(i))
            b.pack(side="left", padx=(0, 6))
            self.tabs.append(b)
            page.pack_forget()
            if i == 0:
                self.select(0)
            return i

        def tab(self, i, text=None, **kw):
            if text is not None:
                self.tabs[i].configure(text=text)

        def select(self, i=None):
            if i is None:
                return self._sel
            i = int(i)
            for p in self.pages:
                p.pack_forget()
            self.pages[i].pack(fill="both", expand=True)
            for j, b in enumerate(self.tabs):
                b.configure(style="TabActive.TButton" if j == i else "Tab.TButton")
            self._sel = i

        def index(self, i):
            return int(i)

    class StripeProgress(tk.Canvas):
        """Full-width racing-stripe bar (like the header stripe under the logo).

        Idle: a static dim strip. Busy: bright yellow stripes scroll across the
        whole panel width — the single loading indicator for the whole app.
        """
        def __init__(self, master, bg=BG):
            super().__init__(master, height=8, bg=bg, highlightthickness=0, bd=0)
            self._offset = 0
            self._busy = False
            self._job = None
            self.bind("<Configure>", lambda e: self._redraw())

        def set_busy(self, busy):
            self._busy = bool(busy)
            if self._busy and self._job is None:
                self._tick()
            elif not self._busy and self._job is not None:
                try:
                    self.after_cancel(self._job)
                except Exception:
                    pass
                self._job = None
            self._redraw()

        def _tick(self):
            self._offset = (self._offset + 2) % 24
            self._redraw()
            self._job = self.after(40, self._tick)

        def _redraw(self):
            self.delete("all")
            w, h = self.winfo_width(), self.winfo_height()
            if w <= 1 or h <= 1:
                return
            col = ACCENT if self._busy else "#565a52"
            step, sw, slant = 24, 11, 6
            x = -step + (self._offset if self._busy else 0)
            while x < w + step:
                self.create_polygon(x, h, x + sw, h, x + sw + slant, 0, x + slant, 0,
                                    fill=col, outline="")
                x += step

    class SubtleTable(tk.Frame):
        """Synchronized native Treeview columns with subtly alternating cell colors.

        Each column is a real Treeview: selection and scrolling are mirrored so
        multi-select, keyboard navigation, inline editing and status menus work
        as one table, while Tk can draw a different background per cell.
        """
        def __init__(self, master, columns):
            super().__init__(master, bg=CARD, bd=0, highlightthickness=0)
            self._columns = columns
            self._parts = []
            self._yscrollcommand = None
            self._sync_view = False
            self._sync_selection = False
            self._handlers = {}
            self._anchor = None
            self._widths = [c[2] for c in columns]
            self._minimum = [42 if c[0] == "num" else 90 if c[0] == "status" else 90 for c in columns]
            self._table_width = None
            self._resize_state = None
            self._styles = []
            style = ttk.Style(self)
            for i, (key, title, width, anchor) in enumerate(columns):
                sty = f"Shade{i % 2}.Treeview"
                if sty not in self._styles:
                    self._styles.append(sty)
                    style.configure(sty, background=CARD, fieldbackground=CARD, foreground=FG,
                                    rowheight=30, borderwidth=0, font=FONT)
                    style.map(sty, background=[("selected", SEL_BG)], foreground=[("selected", FG)])
                    style.layout(sty, [("Treeview.treearea", {"sticky": "nswe"})])
                    head = "#303530" if i % 2 == 0 else "#343934"
                    style.configure(sty + ".Heading", background=head, foreground=HEAD_FG,
                                    font=FONT_BTN_B, relief="flat", padding=(10, 8), bordercolor=HEAD_LINE)
                    style.map(sty + ".Heading", background=[("active", head)])
                t = ttk.Treeview(self, columns=(key,), show="headings", selectmode="extended", style=sty)
                t.heading(key, text=title, anchor="center")
                t.column(key, width=width, minwidth=self._minimum[i], anchor=anchor, stretch=True)
                t.tag_configure("even", background="#1a1e1b" if i % 2 == 0 else "#1d211e")
                t.tag_configure("odd", background="#232724" if i % 2 == 0 else "#202421")
                t.tag_configure("off", foreground=OFFC)
                t.place(x=0, y=0, width=width, relheight=1)
                t.bind("<Motion>", lambda e, idx=i: self._resize_hover(idx, e), add="+")
                t.bind("<B1-Motion>", self._resize_drag, add="+")
                t.bind("<ButtonRelease-1>", self._resize_end, add="+")
                t.configure(yscrollcommand=lambda lo, hi, part=t: self._view_changed(part, lo, hi))
                t.bind("<<TreeviewSelect>>", lambda e, part=t: self._selection_changed(part), add="+")
                self._parts.append(t)
            super().bind("<Configure>", self._resize_table, add="+")

        def _resize_table(self, event):
            target = event.width
            if target <= 1 or target == self._table_width:
                return
            self._table_width = target
            delta = target - sum(self._widths)
            if delta >= 0:
                total = max(1, self._widths[1] + self._widths[2])
                add = round(delta * self._widths[1] / total)
                self._widths[1] += add
                self._widths[2] += delta - add
            else:
                shortage = -delta
                for i in (2, 1, 3, 0):
                    take = min(shortage, self._widths[i] - self._minimum[i])
                    self._widths[i] -= take
                    shortage -= take
                    if not shortage:
                        break
            self._position_columns()

        def _position_columns(self):
            x = 0
            for i, part in enumerate(self._parts):
                width = max(self._minimum[i], self._widths[i])
                part.place_configure(x=x, width=width)
                part.column(self._columns[i][0], width=width)
                x += width

        def _separator_index(self, idx, event):
            part = self._parts[idx]
            if part.identify_region(event.x, event.y) not in ("heading", "separator"):
                return None
            if idx > 0 and event.x <= 5:
                return idx - 1
            if idx < len(self._parts) - 1 and event.x >= part.winfo_width() - 8:
                return idx
            return None

        def _resize_hover(self, idx, event):
            if self._resize_state is None:
                self._parts[idx].configure(cursor="sb_h_double_arrow" if self._separator_index(idx, event) is not None else "")

        def _resize_start(self, idx, event):
            separator = self._separator_index(idx, event)
            if separator is None:
                return False
            self._resize_state = (separator, event.x_root, self._widths[separator], self._widths[separator + 1])
            return True

        def _resize_drag(self, event):
            if self._resize_state is None:
                return None
            idx, start, left, right = self._resize_state
            delta = max(self._minimum[idx] - left,
                        min(right - self._minimum[idx + 1], event.x_root - start))
            self._widths[idx] = left + delta
            self._widths[idx + 1] = right - delta
            self._position_columns()
            return "break"

        def _resize_end(self, event):
            if self._resize_state is not None:
                self._resize_state = None
                return "break"
            return None

        def __getitem__(self, key):
            if key == "columns":
                return tuple(c[0] for c in self._columns)
            return super().__getitem__(key)

        def cget(self, key):
            if key == "selectmode":
                return "extended"
            return super().cget(key)

        def configure(self, cnf=None, **kw):
            if cnf:
                kw.update(cnf)
            if "yscrollcommand" in kw:
                self._yscrollcommand = kw.pop("yscrollcommand")
            if kw:
                return super().configure(**kw)
        config = configure

        def bind(self, sequence=None, func=None, add=None):
            if sequence in ("<Button-1>", "<Double-1>") and func:
                self._handlers[sequence] = func
                for idx, part in enumerate(self._parts):
                    def forward(event, idx=idx, sequence=sequence):
                        from types import SimpleNamespace
                        event2 = SimpleNamespace(x=event.x + self._parts[idx].winfo_x(), y=event.y,
                            x_root=event.x_root, y_root=event.y_root, state=event.state, widget=self)
                        if sequence == "<Button-1>" and self._resize_start(idx, event):
                            return "break"
                        if sequence == "<Button-1>" and event.state & (0x0001 | 0x0004):
                            part = self._parts[idx]
                            if part.identify_region(event.x, event.y) == "cell":
                                iid = part.identify_row(event.y)
                                if iid:
                                    part.focus_set()
                                    if event.state & 0x0001:
                                        rows = list(self.get_children())
                                        anchor = self._anchor if self._anchor in rows else iid
                                        a, b = sorted((rows.index(anchor), rows.index(iid)))
                                        self.selection_set(rows[a:b + 1])
                                        self._anchor = anchor
                                    else:
                                        selected = list(self.selection())
                                        if iid in selected:
                                            selected.remove(iid)
                                        else:
                                            selected.append(iid)
                                        self.selection_set(selected)
                                        self._anchor = iid
                                    return "break"
                        return self._handlers[sequence](event2)
                    part.bind(sequence, forward, add=add)
                return None
            return super().bind(sequence, func, add)

        def column(self, key, option=None):
            i = self._index(key)
            return self._parts[i].column(self._columns[i][0], option)

        def _index(self, col):
            if isinstance(col, str) and col.startswith("#"):
                return int(col[1:]) - 1
            return [c[0] for c in self._columns].index(col)

        def identify_column(self, x):
            for i, part in enumerate(self._parts):
                if part.winfo_x() <= x < part.winfo_x() + part.winfo_width():
                    return f"#{i + 1}"
            return ""

        def identify_region(self, x, y):
            col = self.identify_column(x)
            if not col:
                return "nothing"
            p = self._parts[self._index(col)]
            return p.identify_region(x - p.winfo_x(), y)

        def identify_row(self, y):
            return self._parts[0].identify_row(y)

        def bbox(self, iid, col):
            i = self._index(col)
            part = self._parts[i]
            box = part.bbox(iid, self._columns[i][0])
            if not box:
                return ()
            x, y, w, h = box
            return (x + part.winfo_x(), y + part.winfo_y(), w, h)

        def insert(self, parent, index, iid=None, tags=(), values=()):
            for i, part in enumerate(self._parts):
                part.insert(parent, index, iid=iid, tags=tags,
                            values=(values[i] if i < len(values) else "",))

        def delete(self, *items):
            for part in self._parts:
                part.delete(*items)

        def get_children(self, item=""):
            return self._parts[0].get_children(item)

        def set(self, iid, column):
            i = self._index(column)
            return self._parts[i].set(iid, self._columns[i][0])

        def selection(self):
            return self._parts[0].selection()

        def selection_set(self, items):
            if isinstance(items, str):
                items = (items,)
            else:
                items = tuple(items)
            if items:
                self._anchor = items[-1]
            self._sync_selection = True
            try:
                for part in self._parts:
                    part.selection_set(items)
                    if items:
                        part.focus(items[-1])
            finally:
                self._sync_selection = False

        def _selection_changed(self, source):
            if self._sync_selection:
                return
            selected = source.selection()
            if all(p.selection() == selected for p in self._parts):
                return
            self.selection_set(selected)

        def _view_changed(self, source, lo, hi):
            if self._sync_view:
                return
            self._sync_view = True
            try:
                for part in self._parts:
                    if part is not source:
                        part.yview_moveto(lo)
                if self._yscrollcommand:
                    self._yscrollcommand(lo, hi)
            finally:
                self._sync_view = False

        def yview(self, *args):
            if not args:
                return self._parts[0].yview()
            self._sync_view = True
            try:
                for part in self._parts:
                    part.yview(*args)
                if self._yscrollcommand:
                    self._yscrollcommand(*self._parts[0].yview())
            finally:
                self._sync_view = False

    class App:
        def __init__(self, root):
            self.root = root
            def callback_error(exc_type, exc, tb):
                import traceback
                detail = "".join(traceback.format_exception(exc_type, exc, tb))
                try:
                    self.log(detail)
                    self.show_error(str(exc), "Ошибка интерфейса")
                except Exception:
                    try:
                        messagebox.showerror("Ошибка интерфейса", str(exc), parent=root)
                    except Exception:
                        pass
            root.report_callback_exception = callback_error
            root.title(f"{APP} {VERSION}")
            root.geometry("1120x740")
            root.minsize(940, 600)
            root.bind("<Map>", lambda e: root.after_idle(lambda: use_dark_native_title(root))
                      if e.widget is root else None, add="+")
            self.q = queue.Queue()
            self.worker = None
            self.cancel_event = threading.Event()
            self._build_counter = (0, 0)
            self.payload = Path(args.payload) if getattr(args, "payload", None) else default_payload()
            self.game = find_game(getattr(args, "game", None))
            self.settings = {"volume": 1.0, "native_names": True}
            self.lib = {"tracks": []}
            self.stock_tracks = []
            self.music = None
            self._mtimes = {}
            self._inline = None
            self._inline_get = None
            self._inline_commit = None
            self._edit_job = None
            self._stock_status_job = None
            self._resize_job = None
            self._canvas_layout_size = None
            self._stock_bg_cancel = None
            self._stock_bg_thread = None
            self._stock_bg_enabled = True
            self._custom_scan_pending = None
            self._custom_scan_applied = None
            self._import_lock = threading.Lock()
            self._drop_handler = None
            self._import_refresh_job = None
            self._import_last = None
            self._published_files = set()
            self._published_hash_cache = {}
            self._published_generation = 0
            self._published_job = None
            self._mode_revision = 0
            self._mode_publisher_running = False
            self._player_gen = 0
            self._player_cancel = None
            self._player_file = None
            self._player_open = False
            self._player_paused = False
            self._player_drag = False
            self._player_source = None
            self._player_loading = False
            self._player_pending_paused = False
            self._player_finished = False
            self._preview_volume = 30
            self._volume_popup = None
            self._volume_hide_job = None
            setup_style(root)
            self.canvas = tk.Canvas(root, highlightthickness=0, bd=0, bg=BG)
            self.canvas.pack(fill="both", expand=True)
            self.logo_photo, bg_photo = _brand_images(root)
            self.bgman = _Background(root, self.canvas, bg_photo)
            self.logo_item = None
            if self.logo_photo is not None:
                self.logo_item = self.canvas.create_image(17, 12, anchor="nw", image=self.logo_photo)
            self._build_ui()
            if os.name == "nt":
                try:
                    self._drop_handler = NativeFileDrop(root, lambda paths: self.q.put(("file_drop", paths)))
                except Exception as exc:
                    self.log(f"Перетаскивание файлов недоступно: {exc}")
            if self.game:
                self.load_game(auto=True)
            else:
                self.hint.config(text="Игра не найдена рядом с программой. Укажите папку с speed.exe сверху.")
                self.set_status("Игра не найдена", "warn")
            self.poll()
            self.auto_refresh()

        # ---------------- UI ----------------
        def _build_ui(self):
            # Header: logo is drawn on the root canvas; patch button floats right.
            self.patch_btn = MButton(self.canvas, text="Установить", style="Accent.TButton",
                                     command=self.install_patch, bgman=self.bgman)
            self.patch_item = self.canvas.create_window(0, 0, anchor="ne", window=self.patch_btn)
            self.main = GlassFrame(self.canvas, self.bgman)
            self.main_item = self.canvas.create_window(0, HEADER_H, anchor="nw", window=self.main)
            self.canvas.bind("<Configure>", self._on_canvas_resize)

            gamebar = GlassFrame(self.main, self.bgman)
            gamebar.pack(fill="x", padx=17, pady=(10, 6))
            GlassLabel(gamebar, self.bgman, text="Папка игры", font=FONT_SM,
                       fg="#a7aaa2").pack(side="left", padx=(0, 9))
            self.game_var = tk.StringVar(value=str(self.game) if self.game else "")
            e = ttk.Entry(gamebar, textvariable=self.game_var, state="readonly")
            e.pack(side="left", fill="x", expand=True)
            MButton(gamebar, text="Выбрать…", style="Pick.TButton",
                    command=self.ask_game, bgman=self.bgman).pack(side="left", padx=(9, 0))
            self.hint = GlassLabel(self.main, self.bgman, text="", font=FONT_SM, fg=HINT,
                                   padding=(17, 2, 17, 10), wrap=True)
            self.hint.pack(fill="x")

            self.nb = MockNotebook(self.main, self.bgman)
            stock_tab = PanelFrame(self.nb.body, bgman=self.bgman)
            custom_tab = PanelFrame(self.nb.body, bgman=self.bgman)
            self.nb.add(stock_tab, text="Оригинальные (26)")
            self.nb.add(custom_tab, text="Свои треки (0)")

            # --- stock tab ---
            sbar = GlassFrame(stock_tab, self.bgman, tint=(PANEL_LO, "gray50"))
            sbar.pack(fill="x", padx=11, pady=(11, 0))
            for text, cmd, sty in (("Включить", lambda: self.set_stock_mode("ALL"), "Success.TButton"),
                                   ("Выключить", lambda: self.set_stock_mode("OFF"), "Neutral.TButton")):
                MButton(sbar, text=text, command=cmd, style=sty, bgman=self.bgman).pack(side="left", padx=(0, 5))
            self.stock_info = GlassLabel(sbar, self.bgman, text="", font=FONT_SM, fg=COUNTER,
                                         tint=(PANEL_LO, "gray50"))
            self.stock_info.pack(side="right", padx=(0, 4))
            tk.Frame(stock_tab, bg="#3a3d3b", height=1).pack(fill="x", padx=11, pady=(11, 0))
            self.stock_note = GlassLabel(stock_tab, self.bgman, text="", font=FONT_SM,
                                         fg="#c6c9bf", padding=(3, 10, 3, 10), wrap=True)
            self.stock_note.pack(fill="x", side="bottom", padx=11)
            self.stock_note.bind("<Configure>",
                                 lambda e: self.stock_note.set_wrap_width(e.width - 6)
                                 if e.width > 12 else None, add="+")
            self.stock_tree = self._make_tree(stock_tab, (
                ("num", "№", 44, "center"), ("artist", "Исполнитель", 260, "w"),
                ("title", "Название", 340, "w"), ("status", "Статус ▾", 150, "center")))
            self.stock_tree.bind("<Button-1>", lambda e: self._on_tree_click(self.stock_tree, e, "#4", False))
            self.stock_tree.bind("<Double-1>", lambda e: self._on_tree_double(self.stock_tree, e))

            # --- custom tab ---
            cbar = GlassFrame(custom_tab, self.bgman, tint=(PANEL_LO, "gray50"))
            cbar.pack(fill="x", padx=11, pady=(11, 0))
            MButton(cbar, text="Добавить файлы…", command=self.add_files,
                    style="Tool.TButton", bgman=self.bgman).pack(side="left", padx=(0, 5))
            MButton(cbar, text="Удалить", command=self.remove_tracks,
                    style="Tool.TButton", bgman=self.bgman).pack(side="left", padx=(0, 5))
            MButton(cbar, text="Включить", command=lambda: self.set_custom_mode("ALL"),
                    style="Tool.TButton", bgman=self.bgman).pack(side="left", padx=(0, 5))
            MButton(cbar, text="Выключить", command=lambda: self.set_custom_mode("OFF"),
                    style="Tool.TButton", bgman=self.bgman).pack(side="left", padx=(0, 5))
            IconButton(cbar, icon="down", style="Small.TButton", bgman=self.bgman,
                    command=lambda: self.move(1)).pack(side="right")
            IconButton(cbar, icon="up", style="Small.TButton", bgman=self.bgman,
                    command=lambda: self.move(-1)).pack(side="right", padx=(0, 4))
            tk.Frame(custom_tab, bg="#3a3d3b", height=1).pack(fill="x", padx=11, pady=(11, 0))
            self.tree = self._make_tree(custom_tab, (
                ("num", "№", 44, "center"), ("artist", "Исполнитель", 240, "w"),
                ("title", "Название", 300, "w"),
                ("status", "Статус ▾", 150, "center")))
            self.tree.bind("<Button-1>", lambda e: self._on_tree_click(self.tree, e, "#4", True))
            self.tree.bind("<Double-1>", lambda e: self._on_tree_double(self.tree, e))

            # --- actions ---
            self.act = GlassFrame(self.main, self.bgman)
            self.launch_btn = LaunchButton(self.act, text="    Собрать и запустить игру",
                                      style="Accent.TButton",
                                      command=lambda: self.start_worker(True), bgman=self.bgman)
            self.launch_btn.pack(side="right")
            self.build_btn = MButton(self.act, text="Собрать кэш", style="Build.TButton",
                                     command=lambda: self.start_worker(False), bgman=self.bgman)
            self.build_btn.pack(side="right", padx=(0, 8))
            self.log_btn = MButton(self.act, text="Журнал ▾", style="Journal.TButton",
                                   command=self.toggle_log, bgman=self.bgman)
            self.log_btn.pack(side="left")
            # Emergency stop: visible only while a build/install is running. It
            # commits per track, so stopping keeps every finished track playable.
            self.stop_btn = MButton(self.act, text="■  Остановить", style="Stop.TButton",
                                    command=self.request_stop, bgman=self.bgman)
            # Single full-width animated stripe indicator (replaces the old
            # decorative stripes + the duplicate plain progress bar).
            self.stripe_bar = StripeProgress(self.main, bg=BG)

            self.logf = tk.Frame(self.main, bg=LOG_LINE, bd=0, highlightthickness=0)
            self.log_text = ScrolledText(self.logf, height=6, state="disabled", wrap="word",
                                         bg=LOG_BG, fg=LOG_FG, insertbackground=FG,
                                         relief="flat", borderwidth=0, font=FONT_MONO)
            self.log_text.pack(fill="both", expand=True, padx=1, pady=1)
            self.log_visible = False

            self.status = tk.StringVar(value="")
            self.stats_var = tk.StringVar(value="")
            bar = tk.Frame(self.main, bg=STATUS_BG, highlightbackground=STATUS_LINE,
                           highlightthickness=1)
            left = tk.Frame(bar, bg=STATUS_BG)
            left.pack(side="left", padx=12, pady=9)
            self.dot_canvas = tk.Canvas(left, width=8, height=8, bg=STATUS_BG, highlightthickness=0)
            self.dot_item = self.dot_canvas.create_oval(1, 1, 7, 7, fill=DOT, outline="")
            self.dot_canvas.pack(side="left", padx=(0, 6))
            self.status_label = tk.Label(left, textvariable=self.status, bg=STATUS_BG,
                                         fg="#e5e6dc", font=FONT_BTN_B)
            self.status_label.pack(side="left")
            tk.Label(bar, text=f"V{VERSION}", bg=STATUS_BG, fg=STATUS_FG,
                     font=FONT_SM).pack(side="right", padx=12)
            # Centred stats inside a mockup-style bordered card.
            self.stats_card = tk.Frame(bar, bg="#14181a", highlightbackground=LINE_HI,
                                       highlightthickness=1)
            self.stats_label = tk.Label(self.stats_card, textvariable=self.stats_var,
                                        bg="#14181a", fg="#c7cabf", font=FONT_SM,
                                        padx=16, pady=4)
            self.stats_label.pack()
            self.stats_card.place(relx=0.5, rely=0.5, anchor="center")

            # Pack fixed bars from the bottom up, notebook LAST so it yields space.
            bar.pack(fill="x", side="bottom")
            self.stripe_bar.pack(fill="x", side="bottom", padx=17, pady=(2, 0))
            self.act.pack(fill="x", side="bottom", padx=17, pady=(12, 8))
            self.player = GlassFrame(self.nb.tabbar, self.bgman)
            self.player_button = PlayerButton(self.player, style="Small.TButton", command=self.player_toggle, bgman=self.bgman)
            self.player_button.pack(side="left", padx=(0, 4))
            self.player_stop_button = IconButton(self.player, icon="stop", style="Small.TButton", command=self.player_stop, bgman=self.bgman)
            self.player_stop_button.pack(side="left", padx=(0, 4))
            self.volume_button = IconButton(self.player, icon="volume", style="Small.TButton",
                                            command=self._show_volume, bgman=self.bgman)
            self.volume_button.pack(side="left", padx=(0, 4))
            self.folder_button = IconButton(self.player, icon="folder", style="Small.TButton",
                                            command=self.open_music_folder, bgman=self.bgman)
            self.folder_button.pack(side="left", padx=(0, 8))
            self.volume_button.bind("<Enter>", self._show_volume, add="+")
            self.volume_button.bind("<Leave>", self._volume_leave, add="+")
            self.player_text = tk.StringVar(value="Выберите трек")
            self.player_label = GlassLabel(self.player, self.bgman, text=self.player_text.get(), font=FONT, fg=HINT)
            self.player_label.pack(side="left", padx=(0, 10))
            self.player_text.trace_add("write", lambda *_: self.player_label.config(text=self.player_text.get()))
            self.player_time = tk.StringVar(value="0:00 / 0:00")
            self.player_clock = GlassLabel(self.player, self.bgman, text=self.player_time.get(), font=FONT, fg=HINT)
            self.player_clock.pack(side="right", padx=(8, 0))
            self.player_time.trace_add("write", lambda *_: self.player_clock.config(text=self.player_time.get()))
            self._player_fraction = 0.0
            self._player_offset = 0
            self.player_scale = PlayerTimeline(self.player, self.bgman)
            self.player_scale.configure(width=180)
            self.player_scale.pack(side="right")
            self.player_scale.bind("<Configure>", lambda e: self._player_draw())
            self.player_scale.bind("<Button-1>", self._player_seek)
            self.player_scale.bind("<B1-Motion>", self._player_seek)
            self.root.after(80, self._player_animate)
            self.player.pack(side="right", padx=(100, 4))
            self.nb.pack(fill="both", expand=True, padx=17)
            self.root.after(500, self._player_tick)
            self.root.protocol("WM_DELETE_WINDOW", self.on_close)
            self.update_patch_button()
            self._do_resize()

        def _on_canvas_resize(self, event=None):
            # Debounce: coalesce the flood of <Configure> events while the user
            # drags the window edge into a single relayout ~40ms after it stops.
            if event is not None and (event.width, event.height) == self._canvas_layout_size:
                return  # position-only configure; no layout or image redraw needed
            if getattr(self, "_resize_job", None) is not None:
                try:
                    self.root.after_cancel(self._resize_job)
                except Exception:
                    pass
            self._resize_job = self.root.after(25, self._do_resize)

        def _do_resize(self):
            self._resize_job = None
            w = self.canvas.winfo_width()
            h = self.canvas.winfo_height()
            if w <= 1 or h <= 1:
                return
            if (w, h) == self._canvas_layout_size:
                return
            self._canvas_layout_size = (w, h)
            self.bgman.resize(w, h)
            self.canvas.coords(self.patch_item, w - 17, 44)
            self.canvas.itemconfigure(self.main_item, width=w, height=max(1, h - HEADER_H))
            self.hint.set_wrap_width(w - 34)
            self._draw_header_decor(w, h)

        def _draw_header_decor(self, w, h):
            c = self.canvas
            c.delete("header_decor")
            y = HEADER_H - 1
            c.create_line(17, y, w - 17, y, fill="#3a3d3b", tags="header_decor")
            x = 17
            while x < 17 + 220:
                c.create_polygon(x, y - 4, x + 9, y - 4, x + 5, y, x - 4, y,
                                 fill=ACCENT, outline="", tags="header_decor")
                x += 16
            if hasattr(self, "main_item"):
                c.tag_lower("header_decor", self.main_item)
            if getattr(self, "logo_item", None):
                c.tag_raise(self.logo_item)

        def _make_tree(self, parent, cols):
            wrap = tk.Frame(parent, bg=CARD)
            wrap.pack(fill="both", expand=True, padx=11, pady=(10, 4))
            tree = SubtleTable(wrap, cols)
            vsb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
            tree.configure(yscrollcommand=vsb.set)
            tree.pack(side="left", fill="both", expand=True)
            vsb.pack(side="right", fill="y")
            return tree

        def toggle_log(self):
            self.log_visible = not self.log_visible
            if self.log_visible:
                self.logf.pack(fill="x", side="bottom", before=self.stripe_bar)
                self.log_btn.config(text="Журнал ▴")
            else:
                self.logf.pack_forget()
                self.log_btn.config(text="Журнал ▾")

        # ---------------- game ----------------
        def ask_game(self):
            path = filedialog.askdirectory(title="Выберите папку NFS Most Wanted (со speed.exe)")
            if path:
                self.player_stop()
                self.game = Path(path)
                self.game_var.set(str(self.game))
                self.load_game(auto=False)

        def load_game(self, auto=False):
            if self._stock_bg_cancel:
                self._stock_bg_cancel.set()
            self._custom_scan_pending = self._custom_scan_applied = None
            self._published_files = set()
            self._published_hash_cache = {}
            self._published_generation += 1
            self._stock_bg_enabled = True
            self.music = self.game / "CustomMusic"
            remember_game(self.game)
            self.music.mkdir(parents=True, exist_ok=True)
            self.settings = load_settings(self.game)
            self.settings["native_names"] = True
            self.settings["volume"] = 1.0
            if getattr(args, "ffmpeg", None):
                self.settings["ffmpeg"] = args.ffmpeg
            self.log(f"Игра: {self.game}" + ("  (найдена автоматически)" if auto else ""))
            # No auto-install on first run: the patch is applied only when the
            # user presses «Установить» (or builds). Default state = not installed.
            if (self.game / "speed.exe").is_file():
                if not self.patch_installed():
                    self.log("Патч не установлен. Нажмите «Установить», чтобы добавить мод в игру.")
                self.hint.config(text="Добавьте музыку на вкладке «Свои треки», отключите лишнее на «Оригинальные». Статус — один клик. Оригинал — двойной клик в любой колонке. Свой трек: номер — воспроизведение, текст — редактирование (двойной клик). * — ещё не опубликован.")
            else:
                self.hint.config(text="В этой папке нет speed.exe — выберите другую папку.")
            self.refresh()
            self.root.after(500, self._resume_original_background)

        def open_music_folder(self):
            if not self.music:
                self.show_info("Сначала выберите папку игры.", "Папка музыки")
                return
            try:
                self.music.mkdir(parents=True, exist_ok=True)
                if os.name == "nt":
                    os.startfile(str(self.music))
                else:
                    self.show_info(str(self.music), "Папка музыки")
            except OSError as ex:
                self.show_error(str(ex), "Папка музыки")

        def _resume_original_background(self):
            if not self._stock_bg_enabled or not self.game or not self.music or self._player_loading or os.name != "nt":
                return
            if self._stock_bg_thread and self._stock_bg_thread.is_alive() and self._stock_bg_cancel and not self._stock_bg_cancel.is_set():
                return
            game, music = Path(self.game), Path(self.music)
            tracks = [dict(t) for t in self.stock_tracks]
            cancel = threading.Event()
            self._stock_bg_cancel = cancel
            def work():
                for track in tracks:
                    if cancel.is_set(): return
                    try:
                        if not original_cache_valid(music, track):
                            extract_original_track(game, music, track, cancel, background=True)
                    except BuildCancelled:
                        return
                    except Exception as ex:
                        self.q.put(("log", "Фоновое извлечение оригиналов: " + str(ex)))
                        return
                    if cancel.wait(0.2): return
            self._stock_bg_thread = threading.Thread(target=work, daemon=True)
            self._stock_bg_thread.start()

        def set_status(self, text, kind="ok"):
            self.status.set(text)
            fg = {"ok": "#e5e6dc", "ready": "#e5e6dc", "info": "#e5e6dc",
                  "warn": STATUS_RED, "error": STATUS_RED, "busy": ACCENT}.get(kind, "#e5e6dc")
            dot = {"ready": DOT, "ok": DOT, "info": DOT,
                   "warn": STATUS_RED, "error": STATUS_RED, "busy": ACCENT}.get(kind, DOT)
            self.status_label.config(fg=fg)
            self.dot_canvas.itemconfigure(self.dot_item, fill=dot)

        # ---------------- styled modal dialogs (replace native messagebox) ----------------
        def _styled_dialog(self, message, kind="info", title=None, buttons=None):
            title = title or APP
            buttons = buttons or [("OK", True)]
            top = tk.Toplevel(self.root)
            top.withdraw()
            top.title(title)
            top.configure(bg=PANEL_LO, highlightbackground=LINE_HI, highlightthickness=1)
            top.resizable(False, False)
            top.transient(self.root)
            # Small one-button notifications do not need a title bar; keep
            # confirmation dialogs managed with their native frame.
            if len(buttons) == 1:
                top.overrideredirect(True)
            col = {"info": ACCENT, "warning": ACCENT, "error": STATUS_RED,
                   "question": ACCENT}.get(kind, ACCENT)
            sym = {"info": "i", "warning": "!", "error": "×", "question": "?"}.get(kind, "i")
            head = tk.Frame(top, bg=PANEL_LO)
            head.pack(fill="x", padx=18, pady=(14, 0))
            tk.Label(head, text=title, bg=PANEL_LO, fg=col, font=FONT_BOLD,
                     anchor="w").pack(side="left")
            body = tk.Frame(top, bg=PANEL_LO)
            body.pack(fill="both", expand=True, padx=18, pady=(10, 4))
            icon = tk.Canvas(body, width=34, height=34, bg=PANEL_LO, highlightthickness=0)
            icon.create_oval(2, 2, 32, 32, outline=col, width=2)
            icon.create_text(17, 18, text=sym, fill=col, font=FONT_TITLE)
            icon.pack(side="left", padx=(0, 14), anchor="n")
            tk.Label(body, text=message, bg=PANEL_LO, fg=FG, font=FONT,
                     justify="left", wraplength=360).pack(side="left", fill="both", expand=True)
            result = {"value": buttons[-1][1]}
            bar = tk.Frame(top, bg=PANEL_LO)
            bar.pack(fill="x", padx=18, pady=(8, 16))

            def choose(val):
                result["value"] = val
                try:
                    top.grab_release()
                except Exception:
                    pass
                top.destroy()

            for label, val in reversed(buttons):
                primary = (val is True) or label in ("OK", "Да", "ОК")
                b = MButton(bar, text=label,
                            style="Accent.TButton" if primary else "Tool.TButton",
                            command=lambda v=val: choose(v))
                b.pack(side="right", padx=(8, 0))
            top.protocol("WM_DELETE_WINDOW", lambda: choose(buttons[-1][1]))
            top.bind("<Escape>", lambda e: choose(buttons[-1][1]))
            top.bind("<Return>", lambda e: choose(buttons[0][1]))
            top.update_idletasks()
            rw, rh = self.root.winfo_width(), self.root.winfo_height()
            rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
            w, h = top.winfo_reqwidth(), top.winfo_reqheight()
            top.geometry("+%d+%d" % (rx + max(0, (rw - w) // 2), ry + max(0, (rh - h) // 3)))
            top.deiconify()
            if len(buttons) != 1:
                use_dark_native_title(top)
            top.lift(self.root)
            top.update()
            if not top.winfo_viewable():
                raise RuntimeError("Не удалось показать окно сообщения")
            top.focus_force()
            top.grab_set()
            top.wait_window()
            return result["value"]

        def _dialog_safe(self, msg, kind, title=None, question=False):
            existing = set(self.root.winfo_children())
            try:
                buttons = [("Да", True), ("Нет", False)] if question else [("OK", True)]
                return self._styled_dialog(msg, kind, title, buttons)
            except Exception as ex:
                # Dispose of a partially created dialog before the safe fallback.
                for widget in self.root.winfo_children():
                    if widget not in existing and isinstance(widget, tk.Toplevel):
                        try:
                            widget.grab_release()
                            widget.destroy()
                        except Exception:
                            pass
                # Never hide a critical error if custom chrome fails on a PC.
                self.log(f"Ошибка оформления окна: {ex}")
                method = (messagebox.askyesno if question else
                          {"info": messagebox.showinfo, "warning": messagebox.showwarning,
                           "error": messagebox.showerror}[kind])
                return method(title or APP, msg, parent=self.root)

        def show_info(self, msg, title=None):
            return self._dialog_safe(msg, "info", title)

        def show_warning(self, msg, title=None):
            return self._dialog_safe(msg, "warning", title)

        def show_error(self, msg, title=None):
            return self._dialog_safe(msg, "error", title)

        def ask_yes_no(self, msg, title=None):
            return bool(self._dialog_safe(msg, "question", title, question=True))

        def patch_installed(self):
            return bool(self.game and (self.game / "scripts" / ASI_NAME).is_file())

        def update_patch_button(self):
            if self.patch_installed():
                self.patch_btn.config(text="Удалить патч", style="Danger.TButton", command=self.ask_uninstall)
            else:
                self.patch_btn.config(text="Установить", style="Accent.TButton", command=self.install_patch)

        def install_patch(self):
            if not self.game:
                self.ask_game()
                if not self.game:
                    return
            if self.worker and self.worker.is_alive():
                self.show_warning("Дождитесь окончания текущей операции.")
                return
            self.set_busy(True)
            self.set_status("Установка патча…", "busy")
            game, payload, settings = self.game, self.payload, dict(self.settings)
            cancel = self.cancel_event

            def work():
                try:
                    validate_game(game, self.log)
                    extract_payload(payload, game, self.log)
                    self.log("Патч установлен.")
                    # On-demand converter: fetch FFmpeg into temp/ if missing.
                    ensure_converter(game, self.log, self._on_progress, settings.get("ffmpeg"), cancel=cancel)
                    self.q.put(("done", "Патч установлен"))
                except BuildCancelled:
                    self.q.put(("cancelled", "Установка остановлена"))
                except Exception as ex:
                    self.q.put(("error", str(ex)))

            self.worker = threading.Thread(target=work, daemon=True)
            self.worker.start()

        # ---------------- refresh ----------------
        def log(self, msg):
            self.q.put(("log", str(msg)))

        def _on_progress(self, m):
            # Worker-thread progress: build_cache passes str messages, ensure_converter
            # passes a float fraction. Marshal both to the UI thread via the queue.
            if isinstance(m, (int, float)):
                self.q.put(("status", f"Загрузка конвертера… {int(m * 100)}%"))
            elif isinstance(m, str):
                audio = re.fullmatch(r"AUDIO_PROGRESS (\d+)/(\d+) (\d+)", m)
                if audio:
                    ready, total, remaining = map(int, audio.groups())
                    if remaining:
                        self.q.put(("status", f"Кодирование аудио · {ready}/{total}"))
                    else:
                        self.q.put(("status", f"Аудио готово {ready}/{total} · обновление плейлиста"))
                else:
                    # Keep the committed-index counter for safe-stop reporting,
                    # but don't show it as encoding progress.
                    mm = re.search(r"(?:Encoded|Reused)\s+(\d+)\s*/\s*(\d+)", m)
                    if mm:
                        self._build_counter = (int(mm.group(1)), int(mm.group(2)))

        def request_stop(self):
            self.cancel_event.set()
            self.stop_btn.configure(state="disabled")
            self.set_status("Останавливаю… готовые треки сохраняются", "busy")
            self.log("Остановка по запросу пользователя. Уже собранные треки остаются в игре.")

        def poll(self):
            try:
                while True:
                    kind, data = self.q.get_nowait()
                    if kind == "log":
                        self.log_text.configure(state="normal")
                        self.log_text.insert("end", data + "\n")
                        self.log_text.see("end")
                        self.log_text.configure(state="disabled")
                    elif kind == "status":
                        if self.worker and self.worker.is_alive():
                            self.set_status(data, "busy")
                    elif kind == "stock_player_ready":
                        gen, wav, name, idx = data
                        self._player_ready(gen, wav, name)
                        if gen == self._player_gen:
                            self.root.after(300, self._resume_original_background)
                    elif kind == "player_ready":
                        self._player_ready(*data)
                    elif kind == "player_error":
                        gen, message = data
                        if gen == self._player_gen:
                            self._player_loading = False
                            self._player_source = None
                            self.player_button.set_playing(False)
                            self.player_text.set("Ошибка воспроизведения")
                            self.show_error(message, "Плеер")
                            self.root.after(300, self._resume_original_background)
                    elif kind == "file_drop":
                        self.import_files(data, dropped=True)
                    elif kind == "imported_file":
                        folder, name, dropped = data
                        if self.music and self.music.resolve() == folder:
                            self._import_last = (folder, name, dropped or bool(self._import_last and self._import_last[2]))
                            if self._import_refresh_job is None:
                                self._import_refresh_job = self.root.after(250, self._flush_import_refresh)
                    elif kind == "publication_check":
                        generation, game_path, files, stamps = data
                        if generation == self._published_generation and self.game and self.game.resolve() == game_path:
                            self._published_files = files
                            for i, entry in enumerate(self.lib.get('tracks', [])):
                                try:
                                    st = (self.music / entry['file']).stat()
                                    current = (st.st_size, st.st_mtime_ns)
                                except OSError:
                                    current = None
                                published = entry.get('file') in files and stamps.get(entry.get('file')) == current
                                part = self.tree._parts[0]
                                if part.exists(str(i)):
                                    part.set(str(i), self.tree._columns[0][0], str(i+1) + ('' if published else '*'))
                    elif kind == "mode_publish_done":
                        revision, error, count = data
                        self._mode_publisher_running = False
                        if error:
                            self.log('Не удалось сразу обновить плейлист: ' + error)
                        elif count:
                            self.log('Статусы готовых треков обновлены в кэше; перекодирование не требуется.')
                        if revision != self._mode_revision:
                            self.root.after(150, self._start_mode_publish)
                    elif kind == "import_error":
                        self.show_error(data, "Добавление музыки")
                    elif kind == "import_notice":
                        self.show_info(data, "Добавление музыки")
                    elif kind == "launch_done":
                        self.worker = None
                        self.on_close()
                        return
                    elif kind == "done":
                        self.worker = None
                        self.set_busy(False)
                        self.refresh()
                        self._request_mode_publish()
                        self.set_status(data or "Готово", "ready")
                    elif kind == "cancelled":
                        self.worker = None
                        self.set_busy(False)
                        self.refresh()
                        i, n = self._build_counter
                        msg = f"Остановлено · собрано {i} из {n}" if n else (data or "Остановлено")
                        self.set_status(msg, "warn")
                    elif kind == "error":
                        self.worker = None
                        self.set_busy(False)
                        self.set_status("Ошибка", "error")
                        self.show_error(data)
            except queue.Empty:
                pass
            self.root.after(100, self.poll)

        def _flush_import_refresh(self):
            self._import_refresh_job = None
            data, self._import_last = self._import_last, None
            if not data or not self.music or self.music.resolve() != data[0]:
                return
            folder, name, dropped = data
            self._custom_scan_applied = None
            self.refresh()
            if dropped: self.nb.select(1)
            ids = [str(i) for i, e in enumerate(self.lib.get('tracks', [])) if e.get('file') == name]
            if ids: self.tree._parts[0].see(ids[0])

        def auto_refresh(self):
            """Pick up StockTracks.ini written by the game without a manual button."""
            try:
                if self.game:
                    tracks_ini, _ = stock_paths(self.game)
                    if tracks_ini.exists():
                        st = tracks_ini.stat()
                        key = (st.st_mtime_ns, st.st_size)
                        if self._mtimes.get("stock") != key:
                            self._mtimes["stock"] = key
                            self.refresh_stock()
                    if self.music and self.music.is_dir():
                        # Require two identical scans so an Explorer copy can finish.
                        signature = tuple(sorted((p.name.casefold(), p.stat().st_size, p.stat().st_mtime_ns)
                                          for p in self.music.iterdir()
                                          if p.is_file() and p.suffix.lower() in AUDIO_EXTS and not p.name.startswith('.')))
                        if self._custom_scan_pending != signature:
                            self._custom_scan_pending = signature
                        elif self._custom_scan_applied != signature and not self._inline:
                            old_files = {e.get('file') for e in self.lib.get('tracks', [])}
                            selected = [self.lib['tracks'][i].get('file') for i in self.selected_indices() if i < len(self.lib.get('tracks', []))]
                            self.refresh()
                            new_files = {e.get('file') for e in self.lib.get('tracks', [])} - old_files
                            # A new file clears stale selection; normal metadata
                            # refreshes preserve selections made by the user.
                            if not new_files:
                                keep = [str(i) for i, e in enumerate(self.lib.get('tracks', [])) if e.get('file') in selected]
                                if keep: self.tree.selection_set(keep)
                            self._custom_scan_applied = signature
            except Exception:
                pass
            self.root.after(250, self.auto_refresh)

        def set_busy(self, busy):
            state = "disabled" if busy else "normal"
            self.build_btn.configure(state=state)
            self.launch_btn.configure(state=state)
            self.patch_btn.configure(state=state)
            self.stripe_bar.set_busy(busy)
            if busy:
                self.cancel_event.clear()
                self._build_counter = (0, 0)
                self.stop_btn.configure(state="normal")
                self.stop_btn.pack(side="left", padx=(8, 0))
                self.dot_canvas.itemconfigure(self.dot_item, fill=ACCENT)
            else:
                self.stop_btn.pack_forget()

        def refresh(self):
            if not self.game:
                return
            self._close_inline(False)
            self.update_patch_button()
            try:
                self.lib = sync_library(self.game, self.music, self.settings, log=lambda m: None)
            except Exception as ex:
                self.log(f"Ошибка чтения библиотеки: {ex}")
                self.lib = load_library(self.music / "MusicLibrary.json")
            self.tree.delete(*self.tree.get_children())
            for i, e in enumerate(self.lib.get("tracks", [])):
                mode = e.get("mode", "ALL") if e.get("enabled", True) else "OFF"
                tags = ("odd" if i % 2 else "even",) + (("off",) if mode == "OFF" else ())
                self.tree.insert("", "end", iid=str(i), tags=tags, values=(
                    str(i + 1) + ("" if e.get('file') in self._published_files else "*"), e.get("display_artist") or e.get("artist", ""),
                    e.get("display_title") or e.get("title", ""),
                    STATUS_TEXT.get(mode, mode.upper())))
            self.refresh_stock()
            self.update_status()
            self._schedule_publication_check()

        def _schedule_publication_check(self):
            self._published_generation += 1
            generation = self._published_generation
            if self._published_job is not None:
                self.root.after_cancel(self._published_job)
            self._published_job = self.root.after(200, lambda: self._start_publication_check(generation))

        def _start_publication_check(self, generation):
            self._published_job = None
            if not self.game or not self.music: return
            game, music = self.game.resolve(), self.music.resolve()
            entries = [dict(e) for e in self.lib.get('tracks', [])]
            cache = self._published_hash_cache
            def work():
                stamps = {}
                for e in entries:
                    try:
                        st = (music / e['file']).stat()
                        stamps[e['file']] = (st.st_size, st.st_mtime_ns)
                    except OSError: pass
                files, _ = published_track_files(game, music, entries, cache)
                self.q.put(('publication_check', (generation, game, files, stamps)))
            threading.Thread(target=work, daemon=True).start()

        def _request_mode_publish(self):
            self._mode_revision += 1
            if not self._mode_publisher_running:
                self.root.after(150, self._start_mode_publish)

        def _start_mode_publish(self):
            if self._mode_publisher_running or not self.game or not self.music:
                return
            if self.worker and self.worker.is_alive():
                return  # the completion handler retries with the newest library
            import copy
            game, music, lib, settings = self.game, self.music, copy.deepcopy(self.lib), dict(self.settings)
            revision = self._mode_revision
            self._mode_publisher_running = True
            def work():
                try:
                    count = publish_cached_modes(game, music, lib, settings)
                    self.q.put(('mode_publish_done', (revision, None, count)))
                except Exception as exc:
                    self.q.put(('mode_publish_done', (revision, str(exc), 0)))
            threading.Thread(target=work, daemon=True).start()

        def refresh_stock(self):
            if not self.game:
                return
            self._close_inline(False)
            self.stock_tracks = read_stock_catalog(self.game)
            self.stock_tree.delete(*self.stock_tree.get_children())
            named = 0
            for t in self.stock_tracks:
                named += 1 if t.get("named") else 0
                mode = t.get("mode", "ALL")
                tags = ("odd" if t["index"] % 2 else "even",) + (("off",) if mode == "OFF" else ())
                self.stock_tree.insert("", "end", iid=str(t["index"]), tags=tags, values=(
                    t["index"] + 1,
                    t.get("artist", ""), t.get("title", ""), STATUS_TEXT.get(mode, mode.upper())))
            if named < STOCK_TRACK_COUNT:
                self.stock_note.config(text="Названия EA TRAX справочные и уточнятся после запуска игры. Выключенные треки убираются из плейлиста.")
            else:
                self.stock_note.config(text="Выключенные треки полностью убираются из игрового плейлиста EA TRAX.")
            self.update_status()

        def update_status(self):
            tracks = self.lib.get("tracks", [])
            n = len(tracks)
            en = sum(1 for e in tracks if e.get("enabled", True) and e.get("mode", "ALL") != "OFF")
            son = sum(1 for t in self.stock_tracks if t.get("mode", "ALL") != "OFF")
            total = en + son
            self.nb.tab(0, text=f"Оригинальные ({son}/{STOCK_TRACK_COUNT})")
            self.nb.tab(1, text=f"Свои треки ({n})")
            self.stock_info.config(text=f"Включено {son} из {STOCK_TRACK_COUNT}")
            self.stats_var.set(f"В плейлисте: {total} из {MAX_TOTAL_TRACKS}   ·   оригинальных {son}   ·   своих {en} из {n}")
            self.update_patch_button()
            # Meaningful idle status (skip while a worker is running).
            if not (self.worker and self.worker.is_alive()):
                if not self.patch_installed():
                    self.set_status("Патч не установлен", "warn")
                elif (self.game / "scripts" / MOD_NAME / "Cache" / "EA_TRAX.mpf").is_file():
                    part, done = self._cache_progress()
                    if part:
                        self.set_status(f"Кэш собран частично · {done} треков · нажмите «Собрать кэш»", "warn")
                    else:
                        self.set_status("Кэш собран", "ready")
                else:
                    self.set_status("Патч установлен · кэш не собран", "info")

        def _cache_progress(self):
            """Return (is_partial, built_count) from Build.json, if readable."""
            try:
                rec = json.loads((self.game / "scripts" / MOD_NAME / "Cache" / "Build.json").read_text(encoding="utf-8"))
                return (not rec.get("complete", True), len(rec.get("measurements", [])))
            except Exception:
                return (False, 0)

        # ---------------- inline cell interaction ----------------
        def _close_inline(self, commit=True):
            w = getattr(self, "_inline", None)
            if w is None:
                return
            self._inline = None
            get = getattr(self, "_inline_get", None)
            fn = getattr(self, "_inline_commit", None)
            self._inline_get = None
            self._inline_commit = None
            val = get() if (commit and get) else None
            try:
                w.destroy()
            except Exception:
                pass
            if commit and fn:
                try:
                    fn(val)
                except Exception:
                    pass

        def _on_tree_click(self, tree, event, status_col, editable):
            # One click: select row, or open status. Double click: edit or play.
            # Ctrl / Shift retain multi-selection.
            self._cancel_stock_status_menu()
            if tree.identify_region(event.x, event.y) != "cell":
                self._close_inline(True)
                return None
            col = tree.identify_column(event.x)
            iid = tree.identify_row(event.y)
            if not iid:
                return None
            if event.state & (0x0001 | 0x0004):  # Shift / Ctrl
                self._close_inline(True)
                return None
            if col == status_col:
                self._close_inline(True)
                if iid not in tree.selection():
                    tree.selection_set(iid)
                if tree is self.stock_tree:
                    # Defer the menu just long enough to distinguish a double
                    # click on this same cell (which plays the original song).
                    delay = 500
                    if os.name == 'nt':
                        try:
                            import ctypes
                            delay = max(1, min(5000, int(ctypes.windll.user32.GetDoubleClickTime())))
                        except Exception:
                            pass
                    def show_if_current():
                        self._stock_status_job = None
                        if tree.winfo_exists() and tree.winfo_ismapped() and tree._parts[0].exists(iid):
                            self._open_status_menu(tree, iid, status_col, event)
                    self._stock_status_job = self.root.after(delay + 20, show_if_current)
                else:
                    self._open_status_menu(tree, iid, status_col, event)
                return "break"
            self._close_inline(True)
            return None

        def _cancel_stock_status_menu(self):
            if self._stock_status_job is not None:
                try: self.root.after_cancel(self._stock_status_job)
                except Exception: pass
                self._stock_status_job = None

        def _schedule_cell_edit(self, tree, iid, col):
            if getattr(self, "_edit_job", None) is not None:
                try:
                    self.root.after_cancel(self._edit_job)
                except Exception:
                    pass
            self._edit_job = self.root.after(
                240, lambda: self._open_cell_editor(tree, iid, col))

        def _open_cell_editor(self, tree, iid, col):
            self._edit_job = None
            self._close_inline(True)
            if tree is not self.tree:
                return  # stock names are not editable
            bbox = tree.bbox(iid, col)
            if not bbox:
                return
            x, y, w, h = bbox
            idx = int(iid)
            if idx >= len(self.lib.get("tracks", [])):
                return
            e = self.lib["tracks"][idx]
            key = "display_artist" if col == "#2" else "display_title"
            cur = e.get(key) or e.get(key.replace("display_", ""), "")
            ent = ttk.Entry(tree, font=FONT)
            ent.place(x=x, y=y, width=w, height=h)
            ent.insert(0, cur)
            ent.select_range(0, "end")
            ent.icursor("end")
            ent.lift()
            ent.focus_force()
            self._inline = ent
            self._inline_get = ent.get
            self._inline_commit = lambda v: self._save_cell(tree, iid, col, v)
            ent.bind("<Return>", lambda ev: self._close_inline(True))
            ent.bind("<Escape>", lambda ev: self._close_inline(False))
            ent.bind("<FocusOut>", lambda ev: self._close_inline(True))

        def _save_cell(self, tree, iid, col, value):
            idx = int(iid)
            if idx >= len(self.lib.get("tracks", [])):
                return
            e = self.lib["tracks"][idx]
            key = "display_artist" if col == "#2" else "display_title"
            new = clean_text(value or "")
            old = e.get(key) or e.get(key.replace("display_", ""), "")
            if new == old:
                return
            e[key] = new
            write_sidecar(self.music / e["file"], e)
            self.save_lib()
            self.refresh()
            self.log(f"Изменено: {new}")

        def _open_status_menu(self, tree, iid, col, event):
            idx = int(iid)
            if tree is self.tree:
                if idx >= len(self.lib.get("tracks", [])):
                    return
                e = self.lib["tracks"][idx]
                cur = e.get("mode", "ALL") if e.get("enabled", True) else "OFF"
            else:
                if idx >= len(self.stock_tracks):
                    return
                cur = self.stock_tracks[idx].get("mode", "ALL")
            menu = tk.Menu(self.root, tearoff=0, bg=CARD, fg=FG,
                           activebackground=ACCENT3, activeforeground="#17170e",
                           activeborderwidth=0, font=FONT, bd=1, relief="solid")
            for m in MODE_ORDER:
                label = ("●  " if m == cur else "      ") + MODE_TEXT[m]
                menu.add_command(label=label, command=lambda m=m: self._apply_mode(tree, m))
            # Anchor the dropdown just below the status cell so it reads as an
            # in-place combo; fall back to the pointer position if off-screen.
            bbox = tree.bbox(iid, col)
            if bbox:
                x, y, w, h = bbox
                menu.update_idletasks()
                px = tree.winfo_rootx() + x + (w - menu.winfo_reqwidth()) // 2
                py = tree.winfo_rooty() + y + h
            else:
                px, py = event.x_root, event.y_root
            try:
                menu.tk_popup(px, py)
            finally:
                menu.grab_release()

        def _on_tree_double(self, tree, event):
            self._cancel_stock_status_menu()
            if getattr(self, "_edit_job", None) is not None:
                try:
                    self.root.after_cancel(self._edit_job)
                except Exception:
                    pass
                self._edit_job = None
            if tree.identify_region(event.x, event.y) != "cell":
                return "break"
            iid = tree.identify_row(event.y)
            col = tree.identify_column(event.x)
            if not iid:
                return "break"
            if tree is self.stock_tree:
                self._close_inline(True)
                idx = int(iid)
                if idx < len(self.stock_tracks):
                    track = self.stock_tracks[idx]
                    path = original_cache_path(self.music, track) if self.music else None
                    if path == self._player_source and (self._player_open or self._player_loading):
                        self.player_toggle()
                    else:
                        self._play_stock(track)
                return "break"
            if tree is not self.tree or col == "#4":
                return "break"
            if col in ("#2", "#3"):
                if iid not in tree.selection(): tree.selection_set(iid)
                self._open_cell_editor(tree, iid, col)
                return "break"
            if col != "#1":
                return "break"
            self._close_inline(True)
            idx = int(iid)
            if idx < len(self.lib.get("tracks", [])):
                e = self.lib["tracks"][idx]
                path = (self.music / e["file"]).resolve()
                if path == self._player_source and (self._player_open or self._player_loading):
                    self.player_toggle()
                else:
                    self._player_start(path, e)
            return "break"

        def _mci(self, command, result=False):
            import ctypes
            buf = ctypes.create_unicode_buffer(512)
            code = ctypes.windll.winmm.mciSendStringW(command, buf, len(buf), 0)
            if code:
                err = ctypes.create_unicode_buffer(512)
                ctypes.windll.winmm.mciGetErrorStringW(code, err, len(err))
                raise RuntimeError(err.value or f"MCI error {code}")
            return buf.value if result else None

        def _show_volume(self, event=None):
            if self._volume_hide_job:
                self.root.after_cancel(self._volume_hide_job)
                self._volume_hide_job = None
            if self._volume_popup and self._volume_popup.winfo_exists():
                return
            popup = tk.Toplevel(self.root)
            self._volume_popup = popup
            popup.withdraw()
            popup.overrideredirect(True)
            popup.configure(bg=LINE_HI, padx=1, pady=1)
            popup.transient(self.root)
            inside = tk.Frame(popup, bg=PANEL_LO)
            inside.pack()
            label = tk.Label(inside, text=f"{self._preview_volume}%", bg=PANEL_LO, fg=FG, font=FONT_SM)
            label.pack(padx=5, pady=(5, 0))
            scale = tk.Canvas(inside, width=26, height=112, bg=PANEL_LO,
                              highlightthickness=0, bd=0, cursor="hand2")
            scale.pack(padx=6, pady=(2, 6))
            def draw_scale(event=None):
                scale.delete("all")
                w, h = scale.winfo_width(), scale.winfo_height()
                if w <= 1 or h <= 1:
                    w, h = 26, 112
                top, bottom = 4, h - 4
                left, right = w // 2 - 5, w // 2 + 5
                scale.create_rectangle(left, top, right, bottom, fill="#454a43", outline="#8a8d86")
                y = bottom - (bottom - top) * self._preview_volume / 100
                if y < bottom:
                    scale.create_rectangle(left + 1, y, right - 1, bottom - 1, fill=ACCENT, outline="")
                scale.create_line(left - 2, y, right + 2, y, fill="#e8e9df", width=2)
            def change_scale(event):
                h = scale.winfo_height()
                value = round(100 * (h - 4 - event.y) / max(1, h - 8))
                self._set_preview_volume(value, label)
                draw_scale()
            scale.bind("<Configure>", draw_scale)
            scale.bind("<Button-1>", change_scale)
            scale.bind("<B1-Motion>", change_scale)
            self._volume_canvas = scale
            draw_scale()
            for widget in (popup, inside, scale, label):
                widget.bind("<Enter>", self._volume_enter, add="+")
                widget.bind("<Leave>", self._volume_leave, add="+")
            popup.update_idletasks()
            x = self.volume_button.winfo_rootx() + (self.volume_button.winfo_width() - popup.winfo_reqwidth()) // 2
            y = self.volume_button.winfo_rooty() + self.volume_button.winfo_height() + 3
            popup.geometry(f"+{x}+{y}")
            popup.deiconify()
            popup.lift()

        def _volume_enter(self, event=None):
            if self._volume_hide_job:
                self.root.after_cancel(self._volume_hide_job)
                self._volume_hide_job = None

        def _volume_leave(self, event=None):
            if self._volume_hide_job:
                self.root.after_cancel(self._volume_hide_job)
            self._volume_hide_job = self.root.after(400, self._hide_volume_if_outside)

        def _hide_volume_if_outside(self):
            self._volume_hide_job = None
            popup = self._volume_popup
            if not popup or not popup.winfo_exists():
                return
            x, y = self.root.winfo_pointerxy()
            widget = self.root.winfo_containing(x, y)
            node = widget
            while node:
                if node is popup or node is self.volume_button:
                    return
                node = getattr(node, "master", None)
            popup.destroy()
            self._volume_popup = None

        def _set_preview_volume(self, value, label=None):
            self._preview_volume = max(0, min(100, int(float(value))))
            if label and label.winfo_exists():
                label.configure(text=f"{self._preview_volume}%")
            if self._player_open:
                try:
                    if not set_preview_session_volume(self._preview_volume / 100):
                        self.log("Регулятор громкости: аудиосессия пока не найдена.")
                except Exception as ex:
                    self.log(f"Регулятор громкости: {ex}")

        def _apply_initial_volume(self, gen, attempts=0):
            if gen != self._player_gen or not self._player_open:
                return
            try:
                if set_preview_session_volume(self._preview_volume / 100):
                    return
            except Exception as ex:
                if attempts == 9:
                    self.log(f"Не удалось настроить громкость плеера: {ex}")
            if attempts < 9:
                self.root.after(150, lambda: self._apply_initial_volume(gen, attempts + 1))
            else:
                self.log("Не удалось найти аудиосессию для громкости предпрослушивания.")

        def player_stop(self):
            self._player_gen += 1
            if self._player_cancel:
                self._player_cancel.set()
            if self._player_open:
                try:
                    self._mci("close nfs_music_preview")
                except Exception:
                    pass
            self._player_open = False
            self._player_paused = False
            self._player_loading = False
            self._player_pending_paused = False
            self._player_finished = False
            self._player_source = None
            self.player_button.set_playing(False)
            if self._player_file:
                try:
                    self._player_file.unlink(missing_ok=True)
                except OSError:
                    pass
                self._player_file = None
            self._player_fraction = 0.0
            self._player_draw()
            self.player_time.set("0:00 / 0:00")
            self.player_text.set("Выберите трек")
            if self._stock_bg_cancel and self._stock_bg_cancel.is_set():
                self.root.after(1000, self._resume_original_background)

        def player_toggle(self):
            if self.nb.select() == 0 and self.music:
                ids = self.selected_stock()
                if ids and ids[0] < len(self.stock_tracks):
                    track = self.stock_tracks[ids[0]]
                    if original_cache_path(self.music, track) != self._player_source:
                        self._play_stock(track)
                        return
            if self._player_loading:
                self._player_pending_paused = not self._player_pending_paused
                self.player_button.set_playing(not self._player_pending_paused)
                return
            if self._player_open:
                try:
                    if self._player_finished:
                        self._mci("seek nfs_music_preview to 0")
                        self._mci("play nfs_music_preview")
                        self._player_finished = False
                        self._player_paused = False
                    elif self._player_paused:
                        self._mci("resume nfs_music_preview")
                        self._player_paused = False
                    else:
                        self._mci("pause nfs_music_preview")
                        self._player_paused = True
                    self.player_button.set_playing(not self._player_paused)
                except Exception as ex:
                    self.show_error(str(ex), "Плеер")
                return
            ids = self.selected_indices()
            if not ids or not self.music:
                self.show_info("Выберите свой трек в списке и нажмите ▶.", "Плеер")
                return
            tracks = self.lib.get("tracks", [])
            if ids[0] < len(tracks):
                e = tracks[ids[0]]
                self._player_start((self.music / e["file"]).resolve(), e)

        def _play_stock(self, track):
            if os.name != "nt":
                self.show_info("Встроенный плеер доступен в Windows.", "Плеер")
                return
            if not self.game or not self.music:
                self.show_info("Сначала выберите папку игры.", "Плеер")
                return
            self.player_stop()
            game, music, track = Path(self.game), Path(self.music), dict(track)
            path = original_cache_path(music, track)
            if not original_cache_valid(music, track) and self._stock_bg_cancel:
                self._stock_bg_cancel.set()
            self._player_source = path
            self._player_loading = True
            self._player_pending_paused = False
            self.player_button.set_playing(True)
            gen = self._player_gen
            cancel = threading.Event()
            self._player_cancel = cancel
            name = (str(track.get("artist", "")) + " — " + str(track.get("title", ""))).strip(" —")
            self.player_text.set(name[:20])
            def work():
                wav = None
                try:
                    ready_path = extract_original_track(game, music, track, cancel)
                    if cancel.is_set():
                        raise BuildCancelled("Остановлено")
                    fd, tmp = tempfile.mkstemp(prefix="nfsmw_preview_", suffix=".wav")
                    os.close(fd)
                    wav = Path(tmp)
                    with open(ready_path, "rb") as src, open(wav, "wb") as dst:
                        while True:
                            if cancel.is_set(): raise BuildCancelled("Остановлено")
                            chunk = src.read(1024 * 1024)
                            if not chunk: break
                            dst.write(chunk)
                    if cancel.is_set(): raise BuildCancelled("Остановлено")
                    self.q.put(("stock_player_ready", (gen, wav, name, track["index"])))
                    wav = None
                except BuildCancelled:
                    pass
                except Exception as ex:
                    self.q.put(("player_error", (gen, str(ex))))
                finally:
                    if wav:
                        wav.unlink(missing_ok=True)
            threading.Thread(target=work, daemon=True).start()

        def _player_start(self, path, entry):
            if os.name != "nt":
                self.show_info("Встроенный плеер доступен в Windows.", "Плеер")
                return
            if not path.is_file():
                self.show_error(f"Файл не найден: {path}", "Плеер")
                return
            self.player_stop()
            self._player_source = path
            self._player_loading = True
            self._player_pending_paused = False
            self.player_button.set_playing(True)
            gen = self._player_gen
            cancel = threading.Event()
            self._player_cancel = cancel
            name = (str(entry.get("artist", "")) + " — " + str(entry.get("title", path.stem))).strip(" —")
            self.player_text.set(name[:20])
            def work():
                import tempfile
                wav = None
                try:
                    ff = ensure_converter(self.game, log=lambda msg: self.q.put(("log", msg)), cancel=cancel)
                    fd, tmp = tempfile.mkstemp(prefix="nfsmw_preview_", suffix=".wav")
                    os.close(fd)
                    wav = Path(tmp)
                    run_tool([ff, "-v", "error", "-y", "-threads", "1", "-i", path,
                              "-vn", "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", wav], cancel=cancel)
                    if cancel.is_set():
                        raise BuildCancelled("Остановлено")
                    self.q.put(("player_ready", (gen, wav, name)))
                    wav = None
                except BuildCancelled:
                    pass
                except Exception as ex:
                    self.q.put(("player_error", (gen, str(ex))))
                finally:
                    if wav:
                        wav.unlink(missing_ok=True)
            threading.Thread(target=work, daemon=True).start()

        def _player_ready(self, gen, wav, name):
            if gen != self._player_gen:
                wav.unlink(missing_ok=True)
                return
            try:
                self._mci(f'open "{wav}" type waveaudio alias nfs_music_preview')
                self._player_open = True
                self._player_file = wav
                self._mci("set nfs_music_preview time format milliseconds")
                self._mci("play nfs_music_preview")
                self._apply_initial_volume(gen)
                self._player_loading = False
                self._player_paused = self._player_pending_paused
                if self._player_paused:
                    self._mci("pause nfs_music_preview")
                self.player_button.set_playing(not self._player_paused)
                self.player_text.set(name[:20])
            except Exception as ex:
                self.player_stop()
                try:
                    wav.unlink(missing_ok=True)
                except OSError:
                    pass
                self.show_error(str(ex), "Плеер")

        def _player_draw(self):
            c = self.player_scale
            w, h = c.winfo_width(), c.winfo_height()
            c.delete("timeline")
            if w < 2:
                return
            y0, y1 = 5, max(6, h - 5)
            c.create_line(0, h // 2, w, h // 2, fill="#77796d", tags="timeline")
            filled = int(max(0, min(1, self._player_fraction)) * (w - 2))
            if filled > 0:
                c.create_rectangle(1, y0 + 1, filled, y1 - 1, fill=ACCENT, outline="", tags="timeline")
                step, stripe, slant = 24, 10, 6
                x = -step + self._player_offset
                def clip(poly, boundary, keep_greater):
                    out = []
                    for i, point in enumerate(poly):
                        prev = poly[i - 1]
                        inside = point[0] >= boundary if keep_greater else point[0] <= boundary
                        prev_inside = prev[0] >= boundary if keep_greater else prev[0] <= boundary
                        if inside != prev_inside:
                            t = (boundary - prev[0]) / (point[0] - prev[0])
                            out.append((boundary, prev[1] + t * (point[1] - prev[1])))
                        if inside:
                            out.append(point)
                    return out
                while x < filled:
                    poly = [(x, y1 - 1), (x + stripe, y1 - 1),
                            (x + stripe + slant, y0 + 1), (x + slant, y0 + 1)]
                    poly = clip(clip(poly, 1, True), filled, False)
                    if len(poly) >= 3:
                        c.create_polygon(*[v for point in poly for v in point], fill="#292a1d", outline="", tags="timeline")
                    x += step

        def _player_animate(self):
            if self._player_open and not self._player_paused:
                self._player_offset = (self._player_offset + 2) % 24
                self._player_draw()
            self.root.after(80, self._player_animate)

        def _player_seek(self, event):
            if not self._player_open:
                return
            try:
                length = int(self._mci("status nfs_music_preview length", True))
                fraction = max(0, min(1, event.x / max(1, self.player_scale.winfo_width())))
                self._player_fraction = fraction
                self._player_finished = False
                self._player_draw()
                self._mci(f"seek nfs_music_preview to {int(length * fraction)}")
                self._mci("play nfs_music_preview")
                if self._player_paused:
                    self._mci("pause nfs_music_preview")
            except Exception as ex:
                self.log(f"Плеер: {ex}")

        def _player_tick(self):
            if self._player_open:
                try:
                    pos = int(self._mci("status nfs_music_preview position", True))
                    length = int(self._mci("status nfs_music_preview length", True))
                    fmt = lambda ms: f"{ms // 60000}:{ms // 1000 % 60:02d}"
                    self.player_time.set(f"{fmt(pos)} / {fmt(length)}")
                    self._player_fraction = min(1, pos / max(1, length))
                    if length > 0 and pos >= length and not self._player_paused:
                        self._player_finished = True
                        self._player_paused = True
                        self.player_button.set_playing(False)
                    self._player_draw()
                except Exception:
                    pass
            self.root.after(500, self._player_tick)

        def _open_file(self, path):
            try:
                if not path.is_file():
                    self.log(f"Файл не найден: {path}")
                    return
                if os.name == "nt":
                    os.startfile(str(path))  # type: ignore[attr-defined]
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", str(path)])
                else:
                    subprocess.Popen(["xdg-open", str(path)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.log(f"Открыт в плеере: {path.name}")
            except Exception as ex:
                self.log(f"Не удалось открыть файл: {ex}")

        def _apply_mode(self, tree, mode):
            if tree is self.tree:
                self.set_custom_mode(mode)
            else:
                self.set_stock_mode(mode)

        def selected_indices(self):
            return sorted(int(i) for i in self.tree.selection())

        def selected_stock(self):
            return sorted(int(i) for i in self.stock_tree.selection())

        def save_lib(self):
            save_library(self.music / "MusicLibrary.json", self.lib)

        # ---------------- stock actions ----------------
        def set_stock_mode(self, mode):
            idxs = self.selected_stock()
            if not idxs:
                self.show_info("Сначала выделите треки в списке (Shift / Ctrl для нескольких).")
                return
            for i in idxs:
                self.stock_tracks[i]["mode"] = mode
            write_stock_modes(self.game, self.stock_tracks)
            self.refresh_stock()
            self.log(f"Оригинальные треки: {len(idxs)} шт. → {MODE_TEXT[mode]}. Применится при следующем запуске игры.")

        # ---------------- custom actions ----------------
        def unique_dest(self, name):
            dest = self.music / name
            if not dest.exists():
                return dest
            stem, suffix = Path(name).stem, Path(name).suffix
            i = 2
            while True:
                cand = self.music / f"{stem} ({i}){suffix}"
                if not cand.exists():
                    return cand
                i += 1

        def add_files(self):
            if not self.game:
                self.show_info("Сначала укажите папку игры.")
                return
            paths = filedialog.askopenfilenames(title="Выберите аудиофайлы",
                filetypes=[("Аудио", "*.mp3 *.wav *.m4a"), ("Все файлы", "*.*")])
            if paths: self.import_files(paths, dropped=False)

        def import_files(self, paths, dropped=False):
            if not self.game or not self.music:
                self.show_info("Сначала укажите папку игры.")
                return
            folder = self.music.resolve()
            original = folder / 'Original'
            candidates, rejected = [], 0
            for name in paths:
                try:
                    p = Path(name)
                    if p.is_file() and p.suffix.lower() in AUDIO_EXTS:
                        if p.resolve().is_relative_to(original.resolve()): rejected += 1
                        else: candidates.append(p)
                except OSError:
                    pass
            if rejected:
                self.show_info("Треки из Original предназначены только для прослушивания и не добавляются в свои треки.")
            if not candidates:
                if not rejected: self.show_info("Перетащите аудиофайлы MP3, WAV или M4A.")
                return
            def work():
                with self._import_lock:
                    errors = []
                    for source in candidates:
                        tmp = None
                        try:
                            if source.resolve().parent == folder:
                                name = source.name
                            else:
                                fd, tmp = tempfile.mkstemp(prefix='.import_', suffix='.tmp', dir=folder)
                                os.close(fd)
                                shutil.copy2(source, tmp)
                                stem, suffix = source.stem, source.suffix
                                for n in range(1,10000):
                                    name = source.name if n == 1 else f"{stem} ({n}){suffix}"
                                    dest = folder / name
                                    try:
                                        # Hard-link publishes a complete copy atomically and never overwrites.
                                        os.link(tmp, dest)
                                    except FileExistsError:
                                        continue
                                    except OSError:
                                        # Windows rename also refuses to overwrite an existing file.
                                        if os.name != 'nt': raise
                                        try: os.rename(tmp, dest); tmp = None
                                        except FileExistsError: continue
                                    break
                                else: raise OSError('Не удалось подобрать имя файла.')
                            self.q.put(('imported_file', (folder, name, dropped)))
                        except Exception as exc:
                            errors.append(f"{source.name}: {exc}")
                        finally:
                            if tmp:
                                try: os.unlink(tmp)
                                except OSError: pass
                    if errors: self.q.put(('import_error', "Не удалось добавить:\n" + "\n".join(errors[:10])))
            threading.Thread(target=work, daemon=True).start()

        def remove_tracks(self):
            idxs = self.selected_indices()
            if not idxs:
                return
            if not self.ask_yes_no(f"Удалить треков: {len(idxs)}?\nФайлы будут перемещены в CustomMusic\\_Removed."):
                return
            stamp = time.strftime("%Y%m%d-%H%M%S")
            trash = self.music / "_Removed" / stamp
            for i in reversed(idxs):
                e = self.lib["tracks"][i]
                src = self.music / e["file"]
                dst = trash / e["file"]
                dst.parent.mkdir(parents=True, exist_ok=True)
                if src.exists():
                    shutil.move(str(src), str(dst))
                side = sidecar_path(src)
                if side.exists():
                    sdst = trash / e["sidecar"]
                    sdst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(side), str(sdst))
                del self.lib["tracks"][i]
            self.save_lib()
            self.log(f"Удалено треков: {len(idxs)}")
            self.refresh()

        def set_custom_mode(self, mode):
            idxs = self.selected_indices()
            if not idxs:
                self.show_info("Сначала выделите треки в списке (Shift / Ctrl для нескольких).")
                return
            changes = {}
            for i in idxs:
                e = self.lib["tracks"][i]
                e["mode"] = mode
                e["enabled"] = mode != "OFF"
                changes[e.get('key') or stable_key(e['file'])] = mode
                write_sidecar(self.music / e["file"], e)
            write_custom_state_modes(self.game, changes)
            self.save_lib()
            self.refresh()
            self._request_mode_publish()
            self.log(f"Свои треки: {len(idxs)} шт. → {MODE_TEXT[mode]}; статус сохранён без перекодирования.")
            self.log("Если игра уже запущена, изменения появятся после её перезапуска.")

        def move(self, delta):
            idxs = self.selected_indices()
            if not idxs:
                return
            tracks = self.lib["tracks"]
            if delta < 0:
                for i in idxs:
                    if i > 0:
                        tracks[i - 1], tracks[i] = tracks[i], tracks[i - 1]
            else:
                for i in reversed(idxs):
                    if i < len(tracks) - 1:
                        tracks[i + 1], tracks[i] = tracks[i], tracks[i + 1]
            self.save_lib()
            self.refresh()
            self.tree.selection_set([str(i + delta) for i in idxs if 0 <= i + delta < len(tracks)])

        # ---------------- uninstall ----------------
        def ask_uninstall(self):
            if not self.game:
                return
            if self.worker and self.worker.is_alive():
                self.show_warning("Дождитесь окончания сборки кэша.")
                return
            dlg = tk.Toplevel(self.root)
            dlg.withdraw()
            dlg.title("Удалить патч")
            dlg.configure(bg=BG)
            dlg.transient(self.root)
            dlg.resizable(False, False)
            body = ttk.Frame(dlg, padding=18)
            body.pack(fill="both", expand=True)
            ttk.Label(body, text="Удалить NFSMW Custom Music из игры?", font=FONT_BOLD).pack(anchor="w")
            ttk.Label(body, text="Штатные файлы игры не затрагиваются. Игра должна быть закрыта.\nСвой dinput8.dll удаляется; сторонние ASI-загрузчики не трогаются.",
                      style="Muted.TLabel").pack(anchor="w", pady=(4, 12))
            music_var = tk.BooleanVar(value=False)
            old_var = tk.BooleanVar(value=True)
            ttk.Checkbutton(body, text="Также удалить мою музыку (CustomMusic)", variable=music_var).pack(anchor="w", pady=2)
            ttk.Checkbutton(body, text="Также удалить старый MW-EA-TRAX-Expansion", variable=old_var).pack(anchor="w", pady=2)

            def do_remove():
                dlg.destroy()
                self._stock_bg_enabled = False
                if self._stock_bg_cancel:
                    self._stock_bg_cancel.set()
                self.player_stop()
                try:
                    uninstall(self.game, remove_music=bool(music_var.get()),
                              remove_old=bool(old_var.get()), log=self.log)
                    if music_var.get():
                        self.lib = {"tracks": []}
                        self.tree.delete(*self.tree.get_children())
                        self.refresh_stock()
                        self.update_status()
                    else:
                        # Keep the displayed library and Original cache intact.
                        self._stock_bg_enabled = True
                        self.refresh()
                        self.root.after(500, self._resume_original_background)
                    self.update_patch_button()
                    self.show_info("Патч удалён." if music_var.get() else "Патч удалён. Папка CustomMusic сохранена.")
                except Exception as ex:
                    self.show_error(str(ex))

            row = ttk.Frame(body)
            row.pack(fill="x", pady=(16, 0))
            MButton(row, text="Отмена", command=dlg.destroy).pack(side="right")
            MButton(row, text="Удалить", style="Danger.TButton", command=do_remove).pack(side="right", padx=(0, 8))
            dlg.update_idletasks()
            x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - dlg.winfo_reqwidth()) // 2)
            y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - dlg.winfo_reqheight()) // 3)
            dlg.geometry(f"+{x}+{y}")
            dlg.deiconify()
            use_dark_native_title(dlg)
            dlg.lift(self.root)
            dlg.update()
            if not dlg.winfo_viewable():
                dlg.destroy()
                self.show_error("Не удалось показать окно удаления патча.")
                return
            dlg.focus_force()
            dlg.grab_set()

        # ---------------- worker ----------------
        def start_worker(self, launch):
            if self.worker:
                return
            if self._mode_publisher_running:
                self.root.after(150, lambda: self.start_worker(launch))
                return
            if not self.game:
                self.ask_game()
                if not self.game:
                    return
            if not self.patch_installed():
                self.show_info("Сначала установите патч — кнопка «Установить» сверху.")
                return
            save_settings(self.game, self.settings)
            tracks = self.lib.get("tracks", [])
            en = sum(1 for e in tracks if e.get("enabled", True) and e.get("mode", "ALL") != "OFF")
            son = sum(1 for t in self.stock_tracks if t.get("mode", "ALL") != "OFF")
            if en + son == 0:
                self.show_warning("Плейлист пуст: включите хотя бы один трек.")
                return
            if not self.log_visible:
                self.toggle_log()
            self.set_busy(True)
            self._build_counter = (0, en)
            self.set_status("Проверка файлов и готового кэша…", "busy")
            settings = dict(self.settings)
            game, payload = self.game, self.payload
            cancel = self.cancel_event

            def work():
                try:
                    result = prepare(game, payload, settings, log=self.log,
                                     progress=self._on_progress, do_build=True, cancel=cancel)
                    if launch:
                        launch_game(game, log=self.log)
                        self.q.put(("launch_done", None))
                    else:
                        self.q.put(("done", "Кэш собран"))
                except BuildCancelled:
                    self.q.put(("cancelled", "Сборка остановлена"))
                except Exception as ex:
                    self.q.put(("error", str(ex)))

            self.worker = threading.Thread(target=work, daemon=True)
            self.worker.start()

        def on_close(self):
            if self.worker and self.worker.is_alive():
                if not self.ask_yes_no("Идёт сборка кэша. Всё равно выйти?\n"
                                       "Уже собранные треки сохранятся и будут играть.", "Выход"):
                    return
                # Signal a clean stop so the worker commits and rolls back safely.
                self.cancel_event.set()
                self.worker.join(timeout=5)
            if self._stock_bg_cancel:
                self._stock_bg_cancel.set()
            self.player_stop()
            if self._drop_handler:
                self._drop_handler.close()
            self.root.destroy()

    root = tk.Tk()
    app = App(root)
    root._nfsmw_app = app
    root.mainloop()
    return 0


def main():
    args = parse_args()
    if args.launch or args.build or args.cli or args.no_build or args.uninstall:
        return run_cli(args)
    try:
        return run_gui(args)
    except ImportError as ex:
        print(f"Tkinter недоступен ({ex}); запускаю CLI.")
        return run_cli(args)


if __name__ == "__main__":
    sys.exit(main())
