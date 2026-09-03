"""
Extract PyInstaller's PKG archive from the exe, then walk the embedded PYZ
(zlib-compressed) to verify the source-code fixes are bundled.
PyInstaller stores a cookie at the end of the exe: last 24 bytes are
MAGIC (8B 'MEI\014\013\012\013\016') + pkg_len (4B) + toc_offset (4B) +
toc_len (4B) + pyver (4B).
"""
import struct, zlib, marshal, io, sys
from pathlib import Path

exe = Path(sys.argv[1] if len(sys.argv) > 1 else r'release_v10f/VideoDLDesktop/VideoDLDesktop.exe')
data = exe.read_bytes()
print(f'exe: {exe}  size={len(data)}')

MAGIC = b'MEI\x0c\x0b\x0a\x0b\x0e'
if not data.endswith(MAGIC):
    print('ERROR: not a PyInstaller exe (no MAGIC at end)')
    sys.exit(1)

# Cookie is 24 bytes; fields: pkg_len(4), toc_offset(4), toc_len(4), pyver(4)
pkg_len, toc_off, toc_len, pyver = struct.unpack('!4sIIII', data[-24:])[1:]
print(f'pkg_len={pkg_len} toc_offset={toc_off} toc_len={toc_len} pyver=0x{pyver:08x}')

pkg_start = len(data) - 24 - toc_off  # approximate start; toc is at end
# More reliable: pkg start = len(data) - 24 - pkg_len (but pkg_len includes the trailer)
# Actually: the CArchive (PKG) starts at (len - 24 - toc_offset) and has length toc_offset
pkg = data[len(data) - 24 - toc_off : len(data) - 24]
print(f'pkg slice: {len(pkg)} bytes (first 4: {pkg[:4].hex()})')

# The PKG contains entries: each entry has a header. The PYZ entry contains
# the compressed PYZ archive. Let's find it by scanning for the 'PYZ' marker.
pyz_magic = b'PYZ\x00'
idx = pkg.find(pyz_magic)
if idx < 0:
    print('No PYZ found in PKG (might be in a different archive)')
else:
    print(f'PYZ marker at offset {idx} inside pkg')

# Easier path: PyInstaller's CArchive has a TOC at pkg_start + toc_offset.
# Let's parse the TOC entries to find the PYZ entry.
# But the TOC format is complex. Let's try a different approach:
# the CArchive entries are stored sequentially; the PYZ entry header contains
# the compressed size. We can scan for the Python version cookie.

# Alternative: use the fact that marshal.loads on a code object preserves
# co_consts (string constants). The PYZ is a zlib-compressed marshal of
# {'PYZ-00.pyz': {'python_version': '3.11', 'names': [...], 'contents': ...}}.
# Let's try to find a zlib-compressed block in the PKG that, when decompressed,
# contains our target string.

needles = [b'_platform_parser_table', b'_common_parser_table', b'parse_batch', b'parsebatch']
found = {n: 0 for n in needles}

# Scan the PKG for zlib streams (0x78 0x01 / 0x9C / 0xDA) and try to decompress
zlib_magics = [b'\x78\x9c', b'\x78\x01', b'\x78\xda']
for i in range(0, len(pkg) - 2):
    if pkg[i:i+2] in zlib_magics:
        try:
            dec = zlib.decompress(pkg[i:i+min(2_000_000, len(pkg)-i)])
            for n in needles:
                if n in dec:
                    found[n] += 1
        except Exception:
            pass

print('Needle hits across zlib streams:')
for n, c in found.items():
    print(f'  {n.decode():30s} -> {c}')

# Also check the bundled web/app.js directly (not compressed)
web_js = Path('release_v10f/VideoDLDesktop/_internal/web/app.js')
if web_js.exists():
    js = web_js.read_text(encoding='utf-8', errors='ignore')
    print(f'\nbundled app.js: {len(js)} chars')
    for n in ['Always retry', 'parsebatch', 'cfgAllowed.length === 0']:
        print(f"  {n!r:35s} -> {js.count(n)}")
else:
    print(f'\n(no bundled app.js at {web_js})')
