"""Test helpers that build synthetic study folders and zips, and change one byte in place."""
import os
import zipfile
from pathlib import Path

SCAN_FILES = ("method", "acqp", "pdata/1/2dseq", "pdata/1/visu_pars", "pdata/1/reco")

def make_study(root, name, scans=(1,)):
    root_path = Path(root)
    study_dir = root_path / name
    study_dir.mkdir(parents=True, exist_ok=True)
    
    # Write subject file
    subject_path = study_dir / "subject"
    with open(subject_path, "wb") as f:
        f.write(b"subject\n")
        
    for s in scans:
        for f_rel in SCAN_FILES:
            rel_path = f"{s}/{f_rel}"
            target_path = study_dir / rel_path
            target_path.parent.mkdir(parents=True, exist_ok=True)
            
            if rel_path.endswith("/2dseq"):
                content = bytes(range(256)) * 4
            else:
                content = (rel_path + "\n").encode("utf-8")
                
            with open(target_path, "wb") as f:
                f.write(content)
                
    return study_dir

def zip_study(study_dir, dest):
    study_dir = Path(study_dir)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    
    files_to_zip = []
    for dirpath, _, filenames in os.walk(study_dir):
        for fn in filenames:
            rel = (Path(dirpath) / fn).relative_to(study_dir).as_posix()
            files_to_zip.append(rel)
            
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel in sorted(files_to_zip):
            zf.write(study_dir / rel, study_dir.name + "/" + rel)
            
    return dest

def flip_byte_same_size(path, offset=0):
    path = Path(path)
    st = os.stat(path)
    with open(path, "rb") as f:
        data = bytearray(f.read())
    
    data[offset] ^= 0xFF
    
    with open(path, "wb") as f:
        f.write(data)
        
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    return None

def corrupt_zip_member_data(zip_path, member):
    zip_path = Path(zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        info = zf.getinfo(member)
        
    if info.compress_size == 0:
        raise ValueError("member has no data: " + member)
        
    with open(zip_path, "r+b") as f:
        f.seek(info.header_offset)
        header = f.read(30)
        n = int.from_bytes(header[26:28], "little")
        m = int.from_bytes(header[28:30], "little")
        pos = info.header_offset + 30 + n + m
        
        f.seek(pos)
        b = f.read(1)
        f.seek(pos)
        f.write(bytes([b[0] ^ 0xFF]))
        
    return None

def truncate_file(path, keep_bytes):
    path = Path(path)
    with open(path, "rb") as f:
        data = f.read()
        
    if keep_bytes < 0 or keep_bytes > len(data):
        raise ValueError("keep_bytes out of range")
        
    with open(path, "wb") as f:
        f.write(data[:keep_bytes])
        
    return None
