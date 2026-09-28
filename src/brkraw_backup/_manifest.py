"""File CRC-32 and raw/archive manifest comparison (used by verify.py)."""
import zlib
from pathlib import Path

def file_crc32(path, chunk_size=1048576):
    if not isinstance(chunk_size, int) or chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    crc = 0
    with open(Path(path), "rb") as f:
        while True:
            block = f.read(chunk_size)
            if not block:
                break
            crc = zlib.crc32(block, crc)
    return crc & 0xFFFFFFFF

def compare_manifests(raw, archive):
    missing = []
    extra = []
    size_mismatch = []
    crc_mismatch = []

    raw_names = set(raw.keys())
    archive_names = set(archive.keys())

    missing = sorted(list(raw_names - archive_names))
    extra = sorted(list(archive_names - raw_names))

    common = raw_names & archive_names
    for name in sorted(list(common)):
        raw_size, raw_crc = raw[name]
        arc_size, arc_crc = archive[name]
        if raw_size != arc_size:
            size_mismatch.append(name)
        elif raw_crc is not None and arc_crc is not None and raw_crc != arc_crc:
            crc_mismatch.append(name)

    ok = not (missing or extra or size_mismatch or crc_mismatch)

    return {
        "missing": missing,
        "extra": extra,
        "size_mismatch": size_mismatch,
        "crc_mismatch": crc_mismatch,
        "ok": ok
    }
