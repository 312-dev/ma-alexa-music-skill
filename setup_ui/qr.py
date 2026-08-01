"""A QR encoder, byte mode, versions 1 to 10.

The endpoint check needs a QR so the user can open the verify URL on a phone
with WiFi off, which is the only test that proves the path Amazon actually
takes. One image is not worth a dependency, and a QR that scanners silently
refuse would be worse than no QR at all, so this is a real encoder: Reed-Solomon
over GF(256), interleaved blocks, mask chosen by penalty score.

Capped at version 10 because the only thing it ever encodes is a URL. Past that
encode() returns None and the caller falls back to showing the URL as text.
"""

from __future__ import annotations

# Total codewords (data + error correction) per version.
_TOTAL = {1: 26, 2: 44, 3: 70, 4: 100, 5: 134,
          6: 172, 7: 196, 8: 242, 9: 292, 10: 346}

# (version, ecl) -> (ec codewords per block, [(block count, data codewords), ...])
_ECC = {
    (1, "L"): (7, [(1, 19)]),   (1, "M"): (10, [(1, 16)]),
    (2, "L"): (10, [(1, 34)]),  (2, "M"): (16, [(1, 28)]),
    (3, "L"): (15, [(1, 55)]),  (3, "M"): (26, [(1, 44)]),
    (4, "L"): (20, [(1, 80)]),  (4, "M"): (18, [(2, 32)]),
    (5, "L"): (26, [(1, 108)]), (5, "M"): (24, [(2, 43)]),
    (6, "L"): (18, [(2, 68)]),  (6, "M"): (16, [(4, 27)]),
    (7, "L"): (20, [(2, 78)]),  (7, "M"): (18, [(4, 31)]),
    (8, "L"): (24, [(2, 97)]),  (8, "M"): (22, [(2, 38), (2, 39)]),
    (9, "L"): (30, [(2, 116)]), (9, "M"): (22, [(3, 36), (2, 37)]),
    (10, "L"): (18, [(2, 68), (2, 69)]),
    (10, "M"): (26, [(4, 43), (1, 44)]),
}

_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30],
          6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46],
          10: [6, 28, 50]}

# Bits left over after the data area is filled; they are placed as zeros.
_REMAINDER = {1: 0, 2: 7, 3: 7, 4: 7, 5: 7, 6: 7,
              7: 0, 8: 0, 9: 0, 10: 0}

_ECL_BITS = {"L": 0b01, "M": 0b00, "Q": 0b11, "H": 0b10}


# --- GF(256), primitive polynomial 0x11d -----------------------------------

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


def _mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _generator(degree: int) -> list[int]:
    poly = [1]
    for i in range(degree):
        nxt = [0] * (len(poly) + 1)
        for j, coef in enumerate(poly):
            nxt[j] ^= coef
            nxt[j + 1] ^= _mul(coef, _EXP[i])
        poly = nxt
    return poly


def _remainder(data: list[int], degree: int) -> list[int]:
    gen = _generator(degree)
    work = list(data) + [0] * degree
    for i in range(len(data)):
        coef = work[i]
        if coef:
            for j, g in enumerate(gen):
                work[i + j] ^= _mul(g, coef)
    return work[len(data):]


# --- bit stream -------------------------------------------------------------


def _bitstream(data: bytes, version: int, ecl: str) -> list[int]:
    _, groups = _ECC[(version, ecl)]
    capacity = sum(count * size for count, size in groups) * 8
    bits: list[int] = []

    def put(value: int, length: int) -> None:
        for shift in range(length - 1, -1, -1):
            bits.append((value >> shift) & 1)

    put(0b0100, 4)                                  # byte mode
    put(len(data), 8 if version <= 9 else 16)
    for byte in data:
        put(byte, 8)

    put(0, min(4, capacity - len(bits)))            # terminator
    while len(bits) % 8:
        bits.append(0)
    pad = (0xEC, 0x11)
    index = 0
    while len(bits) < capacity:
        put(pad[index % 2], 8)
        index += 1
    return bits


def _interleave(codewords: list[int], version: int, ecl: str) -> list[int]:
    ec_len, groups = _ECC[(version, ecl)]
    blocks: list[list[int]] = []
    at = 0
    for count, size in groups:
        for _ in range(count):
            blocks.append(codewords[at:at + size])
            at += size
    ec_blocks = [_remainder(block, ec_len) for block in blocks]

    out: list[int] = []
    for i in range(max(len(b) for b in blocks)):
        out.extend(block[i] for block in blocks if i < len(block))
    for i in range(ec_len):
        out.extend(block[i] for block in ec_blocks)
    return out


# --- module placement -------------------------------------------------------


def _skeleton(version: int) -> tuple[list[list[int]], list[list[bool]]]:
    size = version * 4 + 17
    grid = [[0] * size for _ in range(size)]
    fixed = [[False] * size for _ in range(size)]

    def finder(row: int, col: int) -> None:
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = row + dr, col + dc
                if not (0 <= r < size and 0 <= c < size):
                    continue
                if 0 <= dr <= 6 and 0 <= dc <= 6:
                    ring = dr in (0, 6) or dc in (0, 6)
                    core = 2 <= dr <= 4 and 2 <= dc <= 4
                    grid[r][c] = 1 if ring or core else 0
                else:
                    grid[r][c] = 0                  # separator
                fixed[r][c] = True

    finder(0, 0)
    finder(0, size - 7)
    finder(size - 7, 0)

    for i in range(size):
        for r, c in ((6, i), (i, 6)):
            if not fixed[r][c]:
                grid[r][c] = 1 if i % 2 == 0 else 0
                fixed[r][c] = True

    centres = _ALIGN[version]
    corners = {(6, 6), (6, size - 7), (size - 7, 6)}
    for row in centres:
        for col in centres:
            # Alignment patterns do overlap the timing lines, so the only
            # exclusion is the three finder corners.
            if (row, col) in corners:
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    grid[row + dr][col + dc] = 1 if max(abs(dr), abs(dc)) != 1 else 0
                    fixed[row + dr][col + dc] = True

    grid[size - 8][8] = 1                           # the always-dark module
    fixed[size - 8][8] = True

    for i in range(9):                              # reserved for format info
        for r, c in ((8, i), (i, 8)):
            fixed[r][c] = True
    for i in range(8):
        fixed[8][size - 1 - i] = True
        fixed[size - 1 - i][8] = True

    if version >= 7:
        for i in range(18):
            fixed[size - 11 + i % 3][i // 3] = True
            fixed[i // 3][size - 11 + i % 3] = True

    return grid, fixed


def _lay_data(grid, fixed, bits: list[int]) -> None:
    size = len(grid)
    at = 0
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:                                # the vertical timing line
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for c in (col, col - 1):
                if not fixed[row][c]:
                    grid[row][c] = bits[at] if at < len(bits) else 0
                    at += 1
        upward = not upward
        col -= 2


_MASKS = (
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
)

_FINDER_RUN = [1, 0, 1, 1, 1, 0, 1]


def _penalty(grid) -> int:
    size = len(grid)
    score = 0

    lines = [list(row) for row in grid]
    lines += [[grid[r][c] for r in range(size)] for c in range(size)]
    for line in lines:
        run = 1
        for i in range(1, size):
            if line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + run - 5
                run = 1
        if run >= 5:
            score += 3 + run - 5

        for i in range(size - 6):
            if line[i:i + 7] == _FINDER_RUN:
                before = line[max(0, i - 4):i]
                after = line[i + 7:i + 11]
                if len(before) == 4 and sum(before) == 0:
                    score += 40
                if len(after) == 4 and sum(after) == 0:
                    score += 40

    for r in range(size - 1):
        for c in range(size - 1):
            block = grid[r][c] + grid[r][c + 1] + grid[r + 1][c] + grid[r + 1][c + 1]
            if block in (0, 4):
                score += 3

    dark = sum(sum(row) for row in grid)
    percent = dark * 100 // (size * size)
    score += 10 * (abs(percent - 50) // 5)
    return score


def _format_value(ecl: str, mask: int) -> int:
    data = (_ECL_BITS[ecl] << 3) | mask
    value = data << 10
    rem = value
    while rem.bit_length() >= 11:
        rem ^= 0x537 << (rem.bit_length() - 11)
    return (value | rem) ^ 0x5412


def _version_value(version: int) -> int:
    value = version << 12
    rem = value
    while rem.bit_length() >= 13:
        rem ^= 0x1F25 << (rem.bit_length() - 13)
    return value | rem


def _write_format(grid, ecl: str, mask: int) -> None:
    size = len(grid)
    value = _format_value(ecl, mask)
    bit = [(value >> i) & 1 for i in range(15)]

    for i in range(6):
        grid[i][8] = bit[i]
    grid[7][8] = bit[6]
    grid[8][8] = bit[7]
    grid[8][7] = bit[8]
    for i in range(9, 15):
        grid[8][14 - i] = bit[i]

    for i in range(8):
        grid[8][size - 1 - i] = bit[i]
    for i in range(8, 15):
        grid[size - 15 + i][8] = bit[i]
    grid[size - 8][8] = 1


def _write_version(grid, version: int) -> None:
    if version < 7:
        return
    size = len(grid)
    value = _version_value(version)
    for i in range(18):
        bit = (value >> i) & 1
        grid[size - 11 + i % 3][i // 3] = bit
        grid[i // 3][size - 11 + i % 3] = bit


def encode(text: str, ecl: str = "M") -> list[list[int]] | None:
    """Return the module matrix, or None if the text will not fit."""
    data = text.encode("utf-8")
    chosen = None
    for version in range(1, 11):
        _, groups = _ECC[(version, ecl)]
        room = sum(count * size for count, size in groups) * 8
        if 4 + 8 + len(data) * 8 <= room:
            chosen = version
            break
    if chosen is None:
        return None

    bits = _bitstream(data, chosen, ecl)
    codewords = [int("".join(str(b) for b in bits[i:i + 8]), 2)
                 for i in range(0, len(bits), 8)]
    final = _interleave(codewords, chosen, ecl)
    stream = [(word >> shift) & 1 for word in final for shift in range(7, -1, -1)]
    stream += [0] * _REMAINDER[chosen]

    base, fixed = _skeleton(chosen)
    _lay_data(base, fixed, stream)
    _write_version(base, chosen)

    best = None
    for mask in range(8):
        grid = [row[:] for row in base]
        rule = _MASKS[mask]
        for r in range(len(grid)):
            for c in range(len(grid)):
                if not fixed[r][c] and rule(r, c):
                    grid[r][c] ^= 1
        _write_format(grid, ecl, mask)
        score = _penalty(grid)
        if best is None or score < best[0]:
            best = (score, grid)
    return best[1]


def svg(matrix: list[list[int]], quiet: int = 4) -> str:
    """Inline SVG for a matrix.

    Colors are pinned rather than inherited: a QR inverted by a dark-mode rule
    will not scan on most phones.
    """
    size = len(matrix) + quiet * 2
    runs = []
    for r, row in enumerate(matrix):
        c = 0
        while c < len(row):
            if row[c]:
                start = c
                while c < len(row) and row[c]:
                    c += 1
                runs.append(f"M{start + quiet} {r + quiet}h{c - start}v1h-{c - start}z")
            else:
                c += 1
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
        f'width="100%" role="img" aria-label="Verification link QR code" '
        f'style="max-width:16rem;height:auto;image-rendering:pixelated">'
        f'<rect width="{size}" height="{size}" fill="#fff"/>'
        f'<path fill="#000" d="{"".join(runs)}"/></svg>'
    )
