#!/usr/bin/env python3
"""Build the LIUQIN firmware area and a sparse image that writes it at 16 GiB.

  tools/mkfwarea.py FW_DIR OUT.bin [--sparse OUT.img] [--offset 16G] [--size 8M]

Area format (matches the kernel loader):
  "LIUQINFW" | u32 version=1 | u32 count | count x { char name[48];
  u32 offset (bytes from area start) | u32 size | u8 sha256[32] }
Files follow the header, 4 KiB aligned.  The area is 8 MiB.
"""
import argparse, hashlib, os, struct, sys

MAGIC = b"LIUQINFW"
ENTRY = struct.Struct("<48sII32s")

def parse_size(s):
    s = s.strip().upper()
    mult = {"K": 1024, "M": 1024**2, "G": 1024**3}
    if s and s[-1] in mult:
        return int(s[:-1]) * mult[s[-1]]
    return int(s)

def build(fwdir, area_size):
    names = sorted(os.listdir(fwdir))
    names = [n for n in names if os.path.isfile(os.path.join(fwdir, n))]
    if len(names) > 16:
        sys.exit("too many firmware files")
    area = bytearray(area_size)
    hdr = bytearray()
    hdr += MAGIC
    hdr += struct.pack("<II", 1, len(names))
    data_off = 4096
    blobs = []
    for n in names:
        p = os.path.join(fwdir, n)
        blob = open(p, "rb").read()
        off = data_off
        data_off += (len(blob) + 4095) & ~4095
        if data_off > area_size:
            sys.exit("firmware area too small")
        hdr += ENTRY.pack(n.encode()[:47], off, len(blob), hashlib.sha256(blob).digest())
        blobs.append((off, blob))
        print(f"{n:24s} {len(blob):8d} bytes @ 0x{off:x}")
    area[:len(hdr)] = hdr
    for off, blob in blobs:
        area[off:off + len(blob)] = blob
    return bytes(area)

def sparse(area, offset):
    """Android sparse image: skip `offset`, then one RAW chunk with `area`."""
    blk = 4096
    if offset % blk:
        sys.exit("offset must be 4 KiB aligned")
    chunks = b""
    if offset:
        chunks += struct.pack("<HHII", 0xcac3, 0, offset // blk, 12)   # DONT_CARE
    chunks += struct.pack("<HHII", 0xcac1, 0, len(area) // blk, 12 + len(area))  # RAW
    nchunks = 2 if offset else 1
    hdr = struct.pack("<IHHHHI", 0xed26ff3a, 1, 0, 28, 12, nchunks)
    return hdr + chunks + area

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("fwdir")
    ap.add_argument("out")
    ap.add_argument("--sparse")
    ap.add_argument("--offset", default="16G")
    ap.add_argument("--size", default="8M")
    a = ap.parse_args()
    area = build(a.fwdir, parse_size(a.size))
    open(a.out, "wb").write(area)
    print(f"{a.out}: {len(area)} bytes, sha256 {hashlib.sha256(area).hexdigest()[:16]}")
    if a.sparse:
        img = sparse(area, parse_size(a.offset))
        open(a.sparse, "wb").write(img)
        print(f"{a.sparse}: sparse image, {len(img)} bytes (flash as userdata)")

if __name__ == "__main__":
    main()
