"""
Properly locate and decompress PyInstaller's PYZ from the exe, then verify
the source-code fixes are inside.
Layout: exe = bootloader + CArchive. The CArchive has a cookie at its end
(MAGIC 'MEI\x0c\x0b\x0a\x0b\x0e' + 16 bytes of fields). The MAGIC may not
be at the very end of the file (padding / overlay), so we rfind it.
"""
import struct, zlib, sys
from pathlib import Path

exe = Path(sys.argv[1] if len(sys.argv) > 1 else r'release_v10f/VideoDLDesktop/VideoDLDesktop.exe')
data = exe.read_bytes()
MAGIC = b'MEI\x0c\x0b\x0a\x0b\x0e'
mpos = data.rfind(MAGIC)
if mpos < 0:
    sys.exit('no MAGIC')

# Cookie layout: MAGIC(8) + pkg_len(4) + toc_offset(4) + toc_len(4) + pyver(4)
# MAGIC is the FIRST 8 bytes of the cookie, so cookie starts at mpos.
cookie = data[mpos : mpos + 24]
assert len(cookie) == 24
pkg_len, toc_off, toc_len, pyver = struct.unpack('!IIII', cookie[8:])
print(f'cookie: pkg_len={pkg_len} toc_offset={toc_off} toc_len={toc_len} pyver=0x{pyver:08x}')

# CArchive ends at mpos + 24; its start is at end - pkg_len.
carc_end = mpos + 24
carc_start = carc_end - pkg_len
print(f'CArchive: [{carc_start}, {carc_end}) len={pkg_len}')

# CArchive ends at mpos + 24; its start is at end - pkg_len.
carc_end = mpos + 24
carc_start = carc_end - pkg_len
print(f'CArchive: [{carc_start}, {carc_end}) len={pkg_len}')
carc = data[carc_start:carc_end]

# The TOC is at CArchive_start + toc_off, length toc_len.
toc = carc[toc_off : toc_off + toc_len]

# Walk TOC entries. Each entry: entry_len(4) + compressed_len(4) + ...
# Actually the CArchive TOC entries are complex. Simpler: scan the whole
# CArchive for the 'PYZ\x00' magic that marks the PYZ archive, then find the
# zlib stream and decompress.
pyz_idx = carc.find(b'PYZ\x00')
print(f'PYZ marker at CArchive+{pyz_idx}')

# After 'PYZ\x00' there's a Python version string then the zlib data.
# The PYZ archive is a marshalled dict. Let's try to find the zlib data:
# it starts right after the pyver field. Simpler: scan for zlib magic near
# the PYZ marker.
best = None
# Scan the whole CArchive for a zlib stream whose decompressed content
# contains the PYZ magic strings (module names like 'vd_desktop').
for off in range(0, len(carc) - 2):
    if carc[off:off+2] not in (b'\x78\x9c', b'\x78\x01', b'\x78\xda'):
        continue
    # try to decompress up to a reasonable size
    try:
        dec = zlib.decompress(carc[off:off + 4_000_000])
    except Exception:
        continue
    # PYZ marshal payload: contains module names. Check for our needles.
    if b'vd_desktop' in dec and b'backend' in dec and b'api' in dec:
        best = (off, dec)
        break

if best is None:
    print('Could not decompress PYZ (tried whole CArchive)')
    sys.exit(1)

off, dec = best
print(f'decompressed PYZ: {len(dec)} bytes starting at CArchive+{off}')

needles = [
    b'_platform_parser_table',
    b'_common_parser_table',
    b'parse_batch',
    b'parsebatch',
    b'VD_LAZY_PARSERS',
    b'engineready',
]
print('Needle hits in decompressed PYZ:')
for n in needles:
    print(f'  {n.decode():30s} -> {dec.count(n)}')
