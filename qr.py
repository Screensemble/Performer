"""Minimal QR Code generator — pure stdlib (Kodi addons cannot pip-install).

Byte mode, error-correction level L, versions 1-6. Version 6 already fits
~134 byte-mode characters, far more than a setup URL needs, and keeping to
v1-6 avoids the version-information block entirely.

Algorithm adapted from Nayuki's QR Code generator (MIT License),
https://www.nayuki.io/page/qr-code-generator-library
"""

import struct
import zlib

# version -> (num blocks, ecc codewords per block, data codewords per block)
# for error correction level L (versions 1-6 only).
_ECC_L = {
    1: (1, 7, 19),
    2: (1, 10, 34),
    3: (1, 15, 55),
    4: (1, 20, 80),
    5: (1, 26, 108),
    6: (2, 18, 68),
}
# Alignment pattern centers per version.
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34]}

# ---- GF(256) tables for Reed-Solomon --------------------------------------

_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _gf_mul(a, b):
    return _EXP[_LOG[a] + _LOG[b]] if a and b else 0


def _rs_divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 2)
    return result


def _rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, c in enumerate(divisor):
            result[i] ^= _gf_mul(c, factor)
    return result


# ---- bit stream -----------------------------------------------------------


def _encode_data(data, version):
    nblocks, ecc, dcw = _ECC_L[version]
    capacity = nblocks * dcw * 8

    bits = []
    append = bits.extend

    def put(val, n):
        append([(val >> i) & 1 for i in range(n - 1, -1, -1)])

    put(0b0100, 4)            # byte mode
    put(len(data), 8)         # count field (versions 1-9: 8 bits)
    for b in data:
        put(b, 8)
    put(0, min(4, capacity - len(bits)))      # terminator
    while len(bits) % 8:
        bits.append(0)
    pad = 0xEC
    while len(bits) < capacity:
        put(pad, 8)
        pad ^= 0xEC ^ 0x11                     # alternate EC / 11

    codewords = [
        sum(bits[i + j] << (7 - j) for j in range(8))
        for i in range(0, len(bits), 8)
    ]

    # Split into blocks, append each block's ECC, interleave.
    blocks = [codewords[i * dcw:(i + 1) * dcw] for i in range(nblocks)]
    divisor = _rs_divisor(ecc)
    ecc_blocks = [_rs_remainder(b, divisor) for b in blocks]
    out = []
    for i in range(dcw):
        for blk in blocks:
            out.append(blk[i])
    for i in range(ecc):
        for blk in ecc_blocks:
            out.append(blk[i])
    return out


# ---- matrix construction ---------------------------------------------------


def _format_bits(mask):
    fmt = (0b01 << 3) | mask        # ECC L = 01
    rem = fmt
    for _ in range(10):
        rem = (rem << 1) ^ (0b10100110111 if (rem >> 9) & 1 else 0)
    return ((fmt << 10) | rem) ^ 0b101010000010010


def _matrix(version, data, mask):
    size = 17 + 4 * version
    mod = [[False] * size for _ in range(size)]
    func = [[False] * size for _ in range(size)]

    def setf(x, y, dark):
        if 0 <= x < size and 0 <= y < size:
            mod[y][x] = dark
            func[y][x] = True

    # Timing first so the finder patterns drawn over it win.
    for i in range(size):
        setf(6, i, i % 2 == 0)
        setf(i, 6, i % 2 == 0)

    def finder(x, y):
        for dy in range(-1, 8):
            for dx in range(-1, 8):
                dark = (
                    0 <= dx <= 6
                    and 0 <= dy <= 6
                    and (dx in (0, 6) or dy in (0, 6) or (2 <= dx <= 4 and 2 <= dy <= 4))
                )
                setf(x + dx, y + dy, dark)

    finder(0, 0)
    finder(size - 7, 0)
    finder(0, size - 7)

    pos = _ALIGN[version]
    for i, cx in enumerate(pos):
        for j, cy in enumerate(pos):
            # Skip the three corners where finders already are.
            if (i, j) in ((0, 0), (0, len(pos) - 1), (len(pos) - 1, 0)):
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    setf(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)

    def draw_format(m):
        bits = _format_bits(m)
        getbit = lambda i: (bits >> i) & 1 == 1
        for i in range(6):
            setf(8, i, getbit(i))
        setf(8, 7, getbit(6))
        setf(8, 8, getbit(7))
        setf(7, 8, getbit(8))
        for i in range(9, 15):
            setf(14 - i, 8, getbit(i))
        for i in range(8):
            setf(size - 1 - i, 8, getbit(i))
        for i in range(8, 15):
            setf(8, size - 15 + i, getbit(i))
        setf(8, size - 8, True)  # always-dark module

    draw_format(mask)  # reserves the format cells before data placement

    # Data placement: zigzag pairs from bottom-right, skipping column 6.
    codewords = _encode_data(data, version)
    stream = [(cw >> i) & 1 for cw in codewords for i in range(7, -1, -1)]
    i = 0
    right = size - 1
    while right >= 1:
        if right == 6:
            right -= 1
        for vert in range(size):
            upward = ((right + 1) & 2) == 0
            y = size - 1 - vert if upward else vert
            for j in range(2):
                x = right - j
                if not func[y][x]:
                    mod[y][x] = bool(stream[i]) if i < len(stream) else False
                    i += 1
        right -= 2

    # Apply mask to non-function modules.
    def mask_bit(m, x, y):
        if m == 0:
            return (x + y) % 2 == 0
        if m == 1:
            return y % 2 == 0
        if m == 2:
            return x % 3 == 0
        if m == 3:
            return (x + y) % 3 == 0
        if m == 4:
            return (x // 3 + y // 2) % 2 == 0
        if m == 5:
            return (x * y) % 2 + (x * y) % 3 == 0
        if m == 6:
            return ((x * y) % 2 + (x * y) % 3) % 2 == 0
        return ((x + y) % 2 + (x * y) % 3) % 2 == 0

    for y in range(size):
        for x in range(size):
            if not func[y][x] and mask_bit(mask, x, y):
                mod[y][x] = not mod[y][x]

    draw_format(mask)  # final format bits (same cells, same values)
    return mod


def _penalty(mod):
    size = len(mod)
    total = 0

    # Adjacent runs of 5+ in rows and columns.
    for row in mod + [[mod[y][x] for y in range(size)] for x in range(size)]:
        run_color, run_len = row[0], 1
        for cell in row[1:]:
            if cell == run_color:
                run_len += 1
            else:
                if run_len >= 5:
                    total += 3 + (run_len - 5)
                run_color, run_len = cell, 1
        if run_len >= 5:
            total += 3 + (run_len - 5)

    # 2x2 solid blocks.
    for y in range(size - 1):
        for x in range(size - 1):
            c = mod[y][x]
            if c == mod[y][x + 1] == mod[y + 1][x] == mod[y + 1][x + 1]:
                total += 3

    # Finder-like 1:1:3:1:1 patterns with a 4-light run on either side.
    pattern = [True, False, True, True, True, False, True]
    for row in mod + [[mod[y][x] for y in range(size)] for x in range(size)]:
        padded = [False] * 4 + row + [False] * 4
        for i in range(len(padded) - 6):
            if padded[i:i + 7] == pattern:
                if (i >= 4 and not any(padded[i - 4:i])) or (
                    i + 11 <= len(padded) and not any(padded[i + 7:i + 11])
                ):
                    total += 40

    # Dark-ratio deviation from 50%.
    dark = sum(row.count(True) for row in mod)
    k = abs(dark * 20 - size * size * 10) // (size * size)
    total += k * 10
    return total


def encode_matrix(text):
    """Return the QR module matrix (list of rows; True = dark) for text."""
    data = text.encode("utf-8")
    version = next(
        (v for v in range(1, 7) if len(data) <= _ECC_L[v][0] * _ECC_L[v][2] - 2),
        None,
    )
    if version is None:
        raise ValueError("QR payload too long for version 6")
    best = min(
        ((_penalty(m), m) for m in
         (_matrix(version, data, mask) for mask in range(8))),
        key=lambda t: t[0],
    )
    return best[1]


def write_png(text, path, scale=10, border=4):
    """Encode text as a QR code and write a black-on-white PNG."""
    matrix = encode_matrix(text)
    size = (len(matrix) + border * 2) * scale

    def chunk(tag, body):
        return (
            struct.pack(">I", len(body))
            + tag + body
            + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)
        )

    rows = []
    for y in range(size):
        my = y // scale - border
        line = bytearray()
        for x in range(size):
            mx = x // scale - border
            dark = (
                0 <= my < len(matrix)
                and 0 <= mx < len(matrix)
                and matrix[my][mx]
            )
            line += b"\x00\x00\x00" if dark else b"\xff\xff\xff"
        rows.append(b"\x00" + bytes(line))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"".join(rows)))
        + chunk(b"IEND", b"")
    )
    with open(path, "wb") as f:
        f.write(png)
    return size


def write_rounded_qr_png(text, path, size, radius, bg, tile=(255, 255, 255),
                         fg=(0, 0, 0), pad=4):
    """QR modules on a white rounded tile over a solid bg color — opaque
    RGB PNG, so the corners survive Kodi's texture pipeline intact. bg
    should match the color behind the image control. `pad` insets the
    tile so the curve never touches the texture boundary."""
    import math

    matrix = encode_matrix(text)
    n = len(matrix)
    quiet = 4
    scale = max(size // (n + quiet * 2), 1)
    span = scale * (n + quiet * 2)
    off = (size - span) // 2
    hw = size / 2.0
    inner = hw - pad - radius

    def chunk(tag, body):
        return (
            struct.pack(">I", len(body))
            + tag + body
            + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)
        )

    rows = []
    for y in range(size):
        row = bytearray(b"\x00")
        dy = abs(y + 0.5 - hw) - inner
        my = (y - off) // scale - quiet
        for x in range(size):
            dx = abs(x + 0.5 - hw) - inner
            ox, oy = max(dx, 0.0), max(dy, 0.0)
            d = math.sqrt(ox * ox + oy * oy) + min(max(dx, dy), 0.0) - radius
            t = 0.5 - d / 2.0
            t = 0.0 if t < 0 else (1.0 if t > 1 else t)
            px = [bg[i] + (tile[i] - bg[i]) * t for i in range(3)]
            mx = (x - off) // scale - quiet
            if 0 <= my < n and 0 <= mx < n and matrix[my][mx]:
                px = fg
            row += bytes(int(c) for c in px)
        rows.append(bytes(row))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"".join(rows)))
        + chunk(b"IEND", b"")
    )
    with open(path, "wb") as f:
        f.write(png)
    return size
