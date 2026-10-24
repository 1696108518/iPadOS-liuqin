#!/usr/bin/env python3
"""Minimal flattened-device-tree reader (no dtc/libfdt needed).

Used by mkadt.py to take liuqin's real memory map, GIC bases, timer INTID and
UFS/framebuffer addresses from the stock DTB that ships inside the phone's
boot.img, instead of hardcoding values that differ between 8 GB and 12 GB
units or between firmware revisions.

    from fdtread import parse, find, prop_u32, prop_u64_list
    root = parse(open('dtb/boot_unpacked/dtb','rb').read())
"""
import struct

FDT_MAGIC = 0xD00DFEED
FDT_BEGIN_NODE, FDT_END_NODE, FDT_PROP, FDT_NOP, FDT_END = 1, 2, 3, 4, 9


class Node:
    __slots__ = ("name", "props", "children", "parent")

    def __init__(self, name, parent=None):
        self.name = name
        self.props = {}
        self.children = []
        self.parent = parent

    def get(self, name, default=None):
        return self.props.get(name, default)

    def string(self, name):
        v = self.props.get(name)
        if v is None:
            return None
        return v.split(b"\0")[0].decode("utf-8", "replace")

    def strings(self, name):
        v = self.props.get(name)
        if v is None:
            return []
        return [s.decode("utf-8", "replace") for s in v.split(b"\0") if s]

    def cell_count(self, name, default):
        v = self.props.get(name)
        if v is None or len(v) != 4:
            return default
        return struct.unpack(">I", v)[0]

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()

    def path(self):
        if self.parent is None:
            return "/"
        parts = []
        n = self
        while n.parent is not None:
            parts.append(n.name)
            n = n.parent
        return "/" + "/".join(reversed(parts))

    def __repr__(self):
        return f"<Node {self.path()} props={sorted(k for k in self.props)}>"


def parse(blob):
    magic, totalsize, off_struct, off_strings, off_rsv, version, last_comp, \
        boot_cpuid, size_strings, size_struct = struct.unpack_from(">10I", blob, 0)
    if magic != FDT_MAGIC:
        raise ValueError("not a flattened device tree")
    if off_struct + size_struct > len(blob) or off_strings + size_strings > len(blob):
        raise ValueError("truncated device tree")

    strings = blob[off_strings:off_strings + size_strings]
    root = None
    cur = None
    off = off_struct
    while True:
        (tok,) = struct.unpack_from(">I", blob, off)
        off += 4
        if tok == FDT_BEGIN_NODE:
            end = blob.index(b"\0", off)
            name = blob[off:end].decode("utf-8", "replace")
            off = (end + 4) & ~3
            if root is None:
                # The tree's own root node has an empty name; it is the root,
                # not a child of a synthetic one (paths would be off by one).
                root = Node(name, None)
                cur = root
            else:
                child = Node(name, cur)
                cur.children.append(child)
                cur = child
        elif tok == FDT_END_NODE:
            cur = cur.parent
        elif tok == FDT_PROP:
            length, nameoff = struct.unpack_from(">II", blob, off)
            off += 8
            end = strings.index(b"\0", nameoff)
            name = strings[nameoff:end].decode("utf-8", "replace")
            cur.props[name] = blob[off:off + length]
            off = (off + length + 3) & ~3
        elif tok == FDT_NOP:
            continue
        elif tok == FDT_END:
            break
        else:
            raise ValueError(f"bad FDT token {tok} at {off - 4:#x}")
    if root is None:
        raise ValueError("empty device tree")
    return root


def find(root, path):
    """Find by absolute path ('/soc@0/ufshc@1d84000') or None."""
    if path == "/":
        return root
    node = root
    for part in path.strip("/").split("/"):
        for c in node.children:
            if c.name == part:
                node = c
                break
        else:
            return None
    return node


def find_compatible(root, compat):
    for n in root.walk():
        if compat in n.strings("compatible"):
            return n
    return None


def cells(node, value):
    """Decode a <addr size> reg property using the parent's cell counts."""
    if node is None or value is None:
        return []
    parent = node.parent or node
    ac = parent.cell_count("#address-cells", 2)
    sc = parent.cell_count("#size-cells", 1)
    width = (ac + sc) * 4
    if len(value) % width:
        return []
    out = []
    for off in range(0, len(value), width):
        addr = int.from_bytes(value[off:off + ac * 4], "big")
        size = int.from_bytes(value[off + ac * 4:off + width], "big")
        out.append((addr, size))
    return out


def prop_u32(node, name, default=None):
    v = node.props.get(name) if node else None
    if v is None or len(v) < 4:
        return default
    return struct.unpack_from(">I", v, 0)[0]


def memory_banks(root):
    """[(base, size)] of the /memory nodes, in tree order."""
    banks = []
    for n in root.walk():
        if n.get("device_type") == b"memory\0" or n.name.startswith("memory@"):
            banks.extend(cells(n, n.get("reg")))
    return banks


def gic(root):
    n = find_compatible(root, "arm,gic-v3")
    if n is None:
        return None
    regs = cells(n, n.get("reg"))
    if len(regs) < 2:
        return None
    return {"gicd": regs[0], "gicr": regs[1], "stride": prop_u32(n, "redistributor-stride", None)}


def timer(root):
    n = find_compatible(root, "arm,armv8-timer")
    if n is None:
        return None
    freq = prop_u32(n, "clock-frequency", None)
    # interrupts = <type num flags>, <type num flags> ...; virtual = PPI 11.
    intid = None
    raw = n.get("interrupts")
    if raw:
        for off in range(0, len(raw) - 8, 12):
            typ, num, _flags = struct.unpack_from(">III", raw, off)
            if typ == 1:          # GIC_PPI
                intid = 16 + num
                if num == 11:
                    break
    return {"freq": freq, "vtimer_intid": intid}


def qcom_ufshc(root):
    for compat in ("qcom,sm8475-ufshc", "qcom,sm8450-ufshc", "qcom,ufshc"):
        n = find_compatible(root, compat)
        if n is not None:
            regs = cells(n, n.get("reg"))
            if regs:
                return regs[0]
    return None


def simple_framebuffer(root):
    n = find_compatible(root, "simple-framebuffer")
    if n is None:
        return None
    regs = cells(n, n.get("reg"))
    if not regs:
        return None
    return {
        "base": regs[0][0],
        "size": regs[0][1],
        "width": prop_u32(n, "width", 0),
        "height": prop_u32(n, "height", 0),
        "stride": prop_u32(n, "stride", 0),
    }


def dump(root, out, depth=0):
    out.write("  " * depth + (root.name or "/") + "\n")
    for k in sorted(root.props):
        v = root.props[k]
        if len(v) <= 16 and all(32 <= b < 127 or b == 0 for b in v):
            val = v.split(b"\0")[0].decode("ascii", "replace")
        else:
            val = v.hex() if len(v) <= 32 else f"<{len(v)} bytes>"
        out.write("  " * (depth + 1) + f"{k} = {val}\n")
    for c in root.children:
        dump(c, out, depth + 1)


if __name__ == "__main__":
    import sys
    root = parse(open(sys.argv[1], "rb").read())
    print("memory:", [f"{b:#x}+{s:#x}" for b, s in memory_banks(root)])
    print("gic:", gic(root))
    print("timer:", timer(root))
    print("ufs:", qcom_ufshc(root))
    print("fb:", simple_framebuffer(root))
    if "--dump" in sys.argv:
        dump(root, sys.stdout)
