"""Test helpers that damage a synthetic zip on purpose (central directory, stored CRC, duplicate)."""
import warnings
import zipfile
from pathlib import Path

def corrupt_central_directory(zip_path):
    path = Path(zip_path)
    with open(path, "rb") as f:
        data = bytearray(f.read())
    
    idx = data.rfind(b"PK\x05\x06")
    if idx < 0:
        raise ValueError("no end of central directory")
    
    cd_offset = int.from_bytes(data[idx + 16:idx + 20], "little")
    data[cd_offset:cd_offset + 4] = b"\x00\x00\x00\x00"
    
    with open(path, "wb") as f:
        f.write(data)

def set_stored_crc(zip_path, member, new_crc):
    path = Path(zip_path)
    with zipfile.ZipFile(path) as zf:
        info = zf.getinfo(member)
    
    with open(path, "rb") as f:
        data = bytearray(f.read())
    
    crc_bytes = (new_crc & 0xFFFFFFFF).to_bytes(4, "little")
    
    # Local header
    data[info.header_offset + 14:info.header_offset + 18] = crc_bytes
    
    # Central directory
    idx = data.rfind(b"PK\x05\x06")
    cd_size = int.from_bytes(data[idx + 12:idx + 16], "little")
    cd_offset = int.from_bytes(data[idx + 16:idx + 20], "little")
    
    pos = cd_offset
    found = False
    while pos < cd_offset + cd_size:
        # Entry starts with PK\x01\x02 (4 bytes)
        # name_len at offset 28 (2 bytes)
        # extra_len at offset 30 (2 bytes)
        # comment_len at offset 32 (2 bytes)
        name_len = int.from_bytes(data[pos + 28:pos + 30], "little")
        extra_len = int.from_bytes(data[pos + 30:pos + 32], "little")
        comment_len = int.from_bytes(data[pos + 32:pos + 34], "little")
        
        name = bytes(data[pos + 46:pos + 46 + name_len])
        if name == member.encode("utf-8"):
            data[pos + 16:pos + 20] = crc_bytes
            found = True
            break
        pos += 46 + name_len + extra_len + comment_len
    
    if not found:
        raise KeyError(member)
    
    with open(path, "wb") as f:
        f.write(data)

def add_duplicate_member(zip_path, member, data):
    path = Path(zip_path)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with zipfile.ZipFile(path, "a", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(member, data)
