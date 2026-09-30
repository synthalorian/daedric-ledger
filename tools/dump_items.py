#!/usr/bin/env python3
"""Dump tradeable item names from Skyrim plugins (ESM/ESP/ESL) to items.json.

Walks the plugin binary format directly:
  - records of type ARMO/WEAP/MISC/INGR/ALCH/SLGM/SCRL/AMMO/BOOK/KEYM/LIGH
  - FULL subrecord = display name (literal string, or uint32 string-table ID
    when the plugin's TES4 header has the localized flag 0x80)
  - EDID subrecord = editor ID
  - value extracted from the type's DATA subrecord (best-effort sanity floor)

String tables live in Data/Strings/<PluginBasename>_<LANG>.STRINGS.

Usage:
    python tools/dump_items.py --data-dir "<Skyrim SE>/Data" --lang english \
        --out data/items.json
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import zlib
from pathlib import Path

RECORD_TYPES = {b"ARMO", b"WEAP", b"MISC", b"INGR", b"ALCH", b"SLGM", b"SCRL",
                b"AMMO", b"BOOK", b"KEYM", b"LIGH"}

# DATA subrecord value offsets (best-effort; gold value as sanity floor)
# format: record_type -> (offset, fmt) within the DATA subrecord payload
VALUE_LAYOUT = {
    b"ARMO": (0, "<I"),    # value u32 @0, weight f32 @4
    b"WEAP": (0, "<I"),    # value u32 @0, weight f32 @4, damage u16 @8
    b"MISC": (0, "<I"),    # value u32 @0, weight f32 @4
    b"INGR": (0, "<I"),    # value u32 @0, weight f32 @4
    b"ALCH": (4, "<I"),    # weight f32 @0, value u32 @4
    b"SLGM": (0, "<I"),    # value u32 @0, weight f32 @4
    b"SCRL": (0, "<I"),    # value u32 @0
    b"AMMO": (8, "<I"),    # flags u32 @0, damage f32 @4, value u32 @8
    b"KEYM": (0, "<I"),    # value u32 @0, weight f32 @4
    b"LIGH": (40, "<I"),   # value u32 @40 (after time/radius/color/flags/falloff/...)
    b"BOOK": (2, "<H"),    # flags u8, type u8, value u16 @2
}

FLAG_LOCALIZED = 0x80
FLAG_COMPRESSED = 0x00040000


class BSA:
    """Minimal TES BSA reader (v103/v104) — enough to pull .strings files.

    Header: 'BSA\\0', u32 version, u32 folderOffset, u32 archiveFlags,
    u32 folderCount, u32 fileCount, u32 folderNameLen, u32 fileNameLen,
    u32 fileFlags. Folder records 16B, file records 16B each, then folder
    names, then the file-name blob, then file data blocks.
    """

    AF_COMPRESS = 0x4
    AF_EMBED_NAMES = 0x100
    SIZE_TOGGLE = 0x40000000

    def __init__(self, path: Path):
        self.path = path
        # Lazy: read only the header + index region; data blocks are seeked
        # to on demand (texture BSAs are multi-GB).
        with open(path, "rb") as fh:
            head = fh.read(36)
            if head[:4] != b"BSA\x00":
                raise ValueError(f"not a BSA: {path}")
            (self.version, folder_off, self.flags, folder_count, file_count,
             folder_name_len, file_name_len, _file_flags) = struct.unpack_from("<8I", head, 4)
            if self.version not in (103, 104, 105):
                raise ValueError(f"unsupported BSA version {self.version}: {path}")
            index_len = folder_count * (24 if self.version >= 105 else 16) \
                + file_count * 16 + folder_name_len + file_name_len
            fh.seek(folder_off)
            # +64KB slack: v104 prefixes each file-record block with a
            # length-prefixed folder name whose size isn't in the header
            data = fh.read(index_len + 65536)

        # folder records: v105 = 24 bytes (u64 hash, u32 count, 12B pad/offset)
        #                 v103/v104 = 16 bytes (u64 hash, u32 count, u32 pad)
        rec_size = 24 if self.version >= 105 else 16
        folders = []
        pos = 0
        for _ in range(folder_count):
            _hash, count = struct.unpack_from("<QI", data, pos)
            folders.append(count)
            pos += rec_size

        # file record blocks. v104+: each folder's block is prefixed with a
        # length-prefixed folder name whose length INCLUDES the null
        # terminator. v103: names come after all blocks, null-terminated.
        recs = []  # (folder_idx, size, offset)
        folder_names: list[str] = []
        if self.version >= 104:
            for fi, count in enumerate(folders):
                nlen = data[pos]
                raw = data[pos + 1:pos + 1 + nlen].rstrip(b"\x00")
                folder_names.append(raw.decode("ascii", errors="replace"))
                pos += 1 + nlen
                for _ in range(count):
                    _hash, size, off = struct.unpack_from("<QII", data, pos)
                    recs.append((fi, size, off))
                    pos += 16
        else:
            for fi, count in enumerate(folders):
                for _ in range(count):
                    _hash, size, off = struct.unpack_from("<QII", data, pos)
                    recs.append((fi, size, off))
                    pos += 16
            for _ in folders:
                end = data.index(b"\x00", pos)
                folder_names.append(data[pos:end].decode("ascii", errors="replace"))
                pos = end + 1

        # file names blob (sequential, in record order)
        names_end = pos + file_name_len
        names = data[pos:names_end].split(b"\x00")
        self.index: dict[str, tuple[int, int]] = {}
        for (fi, size, off), raw_name in zip(recs, names):
            full = f"{folder_names[fi]}/{raw_name.decode('ascii', errors='replace')}"
            self.index[full.lower().replace("\\", "/")] = (size, off)

    def extract(self, name: str) -> bytes | None:
        """Extract by path-insensitive basename match."""
        key = name.lower()
        entry = None
        for path, rec in self.index.items():
            if path.rsplit("/", 1)[-1] == key:
                entry = rec
                break
        if entry is None:
            return None
        size, off = entry
        with open(self.path, "rb") as fh:
            fh.seek(off)
            blob = fh.read((size & ~self.SIZE_TOGGLE) + 4096)  # slack for embedded name + size header
        pos = 0
        if self.flags & self.AF_EMBED_NAMES:
            nlen = blob[0]
            pos += 1 + nlen
        compressed = bool(self.flags & self.AF_COMPRESS)
        if size & self.SIZE_TOGGLE:
            compressed = not compressed
            size &= ~self.SIZE_TOGGLE
        blob = blob[pos:pos + size]
        if compressed:
            full = struct.unpack_from("<I", blob)[0]
            return zlib.decompress(blob[4:4 + size], bufsize=full)
        return blob


_BSA_CACHE: dict[Path, BSA] = {}


def strings_from_bsas(data_dir: Path, filename: str) -> bytes | None:
    for bsa_path in sorted(data_dir.glob("*.bsa"), key=lambda p: p.name.lower()):
        try:
            bsa = _BSA_CACHE.get(bsa_path)
            if bsa is None:
                bsa = _BSA_CACHE[bsa_path] = BSA(bsa_path)
        except Exception:
            continue
        blob = bsa.extract(filename)
        if blob is not None:
            return blob
    return None


def load_strings(strings_dir: Path, plugin_stem: str, lang: str) -> dict[int, str]:
    """Parse <stem>_<lang>.STRINGS: uint32 count, uint32 dataSize, then
    count x (uint32 id, uint32 offset), then null-terminated string blob.
    Falls back to BSAs in the Data dir when no loose file exists (vanilla
    masters ship their string tables inside 'Skyrim - Misc.bsa')."""
    filename = f"{plugin_stem}_{lang}.strings"
    data: bytes | None = None
    path = strings_dir / filename
    if path.exists():
        data = path.read_bytes()
    else:
        # case-insensitive loose-file fallback (Wine/NTFS habits)
        for cand in strings_dir.glob(f"{plugin_stem}_*.strings"):
            if cand.name.lower() == filename.lower():
                data = cand.read_bytes()
                break
        else:
            data = strings_from_bsas(strings_dir.parent, filename)
    if not data or len(data) < 8:
        return {}
    count, _data_size = struct.unpack_from("<II", data, 0)
    table = {}
    entries = []
    for i in range(count):
        off = 8 + i * 8
        if off + 8 > len(data):
            break
        sid, soff = struct.unpack_from("<II", data, off)
        entries.append((sid, soff))
    blob_start = 8 + count * 8
    for sid, soff in entries:
        start = blob_start + soff
        end = data.find(b"\x00", start)
        if end == -1:
            continue
        try:
            table[sid] = data[start:end].decode("utf-8", errors="replace")
        except Exception:
            continue
    return table


def iter_subrecords(buf: bytes):
    """Yield (sig, payload) from a record's data region."""
    pos = 0
    while pos + 6 <= len(buf):
        sig = buf[pos:pos + 4]
        size = struct.unpack_from("<H", buf, pos + 4)[0]
        pos += 6
        if sig == b"XXXX":
            # oversized subrecord; real size in payload, next subrecord carries data
            if size >= 4:
                _real = struct.unpack_from("<I", buf, pos)[0]
            pos += size
            continue
        payload = buf[pos:pos + size]
        yield sig, payload
        pos += size


def parse_plugin(path: Path, strings: dict[int, str]) -> list[dict]:
    """Extract item records from one plugin. Best-effort, never raises."""
    out: list[dict] = []
    data = path.read_bytes()
    if len(data) < 24 or data[:4] != b"TES4":
        return out

    header_flags = struct.unpack_from("<I", data, 8)[0]
    localized = bool(header_flags & FLAG_LOCALIZED)

    pos = 24 + struct.unpack_from("<I", data, 4)[0]  # skip TES4 payload
    current_group_type: bytes | None = None

    while pos + 24 <= len(data):
        sig = data[pos:pos + 4]
        size = struct.unpack_from("<I", data, pos + 4)[0]
        if sig == b"GRUP":
            # group label = record type for type-0 groups
            current_group_type = data[pos + 8:pos + 12]
            pos += 24
            continue
        if sig not in RECORD_TYPES:
            # non-item record at top level — skip it, keep scanning
            if size < 0 or pos + 24 + size > len(data) + 65536:
                break  # desync safety: bail rather than slide forever
            pos += 24 + size
            continue
        flags = struct.unpack_from("<I", data, pos + 8)[0]
        payload = data[pos + 24:pos + 24 + size]
        if flags & FLAG_COMPRESSED:
            try:
                payload = zlib.decompress(payload[4:])  # first 4 bytes = decompressed size
            except Exception:
                pos += 24 + size
                continue
        name = ""
        edid = ""
        value = 0
        for ssig, spayload in iter_subrecords(payload):
            if ssig == b"FULL":
                if localized:
                    if len(spayload) >= 4:
                        sid = struct.unpack_from("<I", spayload)[0]
                        name = strings.get(sid, "")
                else:
                    name = spayload.rstrip(b"\x00").decode("utf-8", errors="replace")
            elif ssig == b"EDID":
                edid = spayload.rstrip(b"\x00").decode("utf-8", errors="replace")
            elif ssig in (b"DATA", b"DATA2") and not value:
                layout = VALUE_LAYOUT.get(sig)
                if layout:
                    off, fmt = layout
                    if len(spayload) >= off + struct.calcsize(fmt):
                        v = struct.unpack_from(fmt, spayload, off)[0]
                        if 0 < v < 10_000_000:
                            value = int(v)
        if name:
            out.append({
                "name": name,
                "type": sig.decode(),
                "value": value,
                "editor_id": edid,
                "plugin": path.name,
            })
        pos += 24 + size
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True, type=Path)
    ap.add_argument("--lang", default="english")
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()

    data_dir: Path = args.data_dir
    if not data_dir.is_dir():
        sys.exit(f"Data dir not found: {data_dir}")
    strings_dir = data_dir / "Strings"

    plugins = sorted(
        [p for p in data_dir.iterdir() if p.suffix.lower() in (".esm", ".esp", ".esl")],
        key=lambda p: p.name.lower(),
    )
    print(f"found {len(plugins)} plugins")

    seen: set[tuple[str, str]] = set()
    items: list[dict] = []
    for plug in plugins:
        try:
            strings = load_strings(strings_dir, plug.stem, args.lang)
            recs = parse_plugin(plug, strings)
        except Exception as exc:  # one bad plugin must not kill the dump
            print(f"  !! {plug.name}: {exc}", file=sys.stderr)
            continue
        kept = 0
        for r in recs:
            key = (r["name"].lower(), r["type"])
            if key in seen:
                continue
            seen.add(key)
            items.append(r)
            kept += 1
        if kept:
            print(f"  {plug.name}: +{kept} items")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"items": items}, indent=1, ensure_ascii=False))
    print(f"\nwrote {len(items)} items -> {args.out}")


if __name__ == "__main__":
    main()
