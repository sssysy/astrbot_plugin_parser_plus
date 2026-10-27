from __future__ import annotations

import hashlib
from urllib.parse import quote, urlparse


ZK = (
    1170614578,
    1024848638,
    1413669199,
    3951632832,
    3528873006,
    2921909214,
    4151847688,
    3997739139,
    1933479194,
    3323781115,
    3888513386,
    460404854,
    3747539722,
    2403641034,
    2615871395,
    2119585428,
    2265697227,
    2035090028,
    2773447226,
    4289380121,
    4217216195,
    2200601443,
    3051914490,
    1579901135,
    1321810770,
    456816404,
    2903323407,
    4065664991,
    330002838,
    3506006750,
    363569021,
    2347096187,
)

ZB = (
    20,
    223,
    245,
    7,
    248,
    2,
    194,
    209,
    87,
    6,
    227,
    253,
    240,
    128,
    222,
    91,
    237,
    9,
    125,
    157,
    230,
    93,
    252,
    205,
    90,
    79,
    144,
    199,
    159,
    197,
    186,
    167,
    39,
    37,
    156,
    198,
    38,
    42,
    43,
    168,
    217,
    153,
    15,
    103,
    80,
    189,
    71,
    191,
    97,
    84,
    247,
    95,
    36,
    69,
    14,
    35,
    12,
    171,
    28,
    114,
    178,
    148,
    86,
    182,
    32,
    83,
    158,
    109,
    22,
    255,
    94,
    238,
    151,
    85,
    77,
    124,
    254,
    18,
    4,
    26,
    123,
    176,
    232,
    193,
    131,
    172,
    143,
    142,
    150,
    30,
    10,
    146,
    162,
    62,
    224,
    218,
    196,
    229,
    1,
    192,
    213,
    27,
    110,
    56,
    231,
    180,
    138,
    107,
    242,
    187,
    54,
    120,
    19,
    44,
    117,
    228,
    215,
    203,
    53,
    239,
    251,
    127,
    81,
    11,
    133,
    96,
    204,
    132,
    41,
    115,
    73,
    55,
    249,
    147,
    102,
    48,
    122,
    145,
    106,
    118,
    74,
    190,
    29,
    16,
    174,
    5,
    177,
    129,
    63,
    113,
    99,
    31,
    161,
    76,
    246,
    34,
    211,
    13,
    60,
    68,
    207,
    160,
    65,
    111,
    82,
    165,
    67,
    169,
    225,
    57,
    112,
    244,
    155,
    51,
    236,
    200,
    233,
    58,
    61,
    47,
    100,
    137,
    185,
    64,
    17,
    70,
    234,
    163,
    219,
    108,
    170,
    166,
    59,
    149,
    52,
    105,
    24,
    212,
    78,
    173,
    45,
    0,
    116,
    226,
    119,
    136,
    206,
    135,
    175,
    195,
    25,
    92,
    121,
    208,
    126,
    139,
    3,
    75,
    141,
    21,
    130,
    98,
    241,
    40,
    154,
    66,
    184,
    49,
    181,
    46,
    243,
    88,
    101,
    183,
    8,
    23,
    72,
    188,
    104,
    179,
    210,
    134,
    250,
    201,
    164,
    89,
    216,
    202,
    220,
    50,
    221,
    152,
    140,
    33,
    235,
    214,
)

ALPHABET = "6fpLRqJO8M/c3jnYxFkUVC4ZIG12SiH=5v0mXDazWBTsuw7QetbKdoPyAl+hN9rgE"
KEY16 = b"059053f7d15e01d7"


def _read_u32_be(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "big")


def _write_u32_be(value: int, output: bytearray, offset: int) -> None:
    output[offset : offset + 4] = (value & 0xFFFFFFFF).to_bytes(4, "big")


def _rotate_left(value: int, bits: int) -> int:
    value &= 0xFFFFFFFF
    return ((value << bits) | (value >> (32 - bits))) & 0xFFFFFFFF


def _g_transform(value: int) -> int:
    b0 = (value >> 24) & 0xFF
    b1 = (value >> 16) & 0xFF
    b2 = (value >> 8) & 0xFF
    b3 = value & 0xFF
    mixed = (
        (ZB[b0] << 24) | (ZB[b1] << 16) | (ZB[b2] << 8) | ZB[b3]
    )
    return (
        mixed
        ^ _rotate_left(mixed, 2)
        ^ _rotate_left(mixed, 10)
        ^ _rotate_left(mixed, 18)
        ^ _rotate_left(mixed, 24)
    ) & 0xFFFFFFFF


def _r_block(block: bytes) -> bytes:
    words = [0] * 36
    words[0] = _read_u32_be(block, 0)
    words[1] = _read_u32_be(block, 4)
    words[2] = _read_u32_be(block, 8)
    words[3] = _read_u32_be(block, 12)
    for index in range(32):
        transform = _g_transform(
            words[index + 1]
            ^ words[index + 2]
            ^ words[index + 3]
            ^ ZK[index]
        )
        words[index + 4] = (words[index] ^ transform) & 0xFFFFFFFF
    output = bytearray(16)
    _write_u32_be(words[35], output, 0)
    _write_u32_be(words[34], output, 4)
    _write_u32_be(words[33], output, 8)
    _write_u32_be(words[32], output, 12)
    return bytes(output)


def _x_blocks(data: bytes, initial: bytes) -> bytes:
    vector = bytearray(initial)
    output = bytearray(len(data))
    for offset in range(0, len(data), 16):
        mixed = bytearray(16)
        for index in range(16):
            mixed[index] = data[offset + index] ^ vector[index]
        vector = bytearray(_r_block(bytes(mixed)))
        output[offset : offset + 16] = vector
    return bytes(output)


def _custom_encode(data: bytes) -> str:
    remainder = len(data) % 3
    if remainder:
        data += b"\x00" * (3 - remainder)
    output: list[str] = []
    index = 0
    cursor = len(data) - 1
    while cursor >= 0:
        value = 0
        for shift in range(3):
            mask = (58 >> (8 * (index % 4))) & 0xFF
            index += 1
            value |= (data[cursor - shift] ^ mask) << (8 * shift)
        output.extend(
            (
                ALPHABET[value & 63],
                ALPHABET[(value >> 6) & 63],
                ALPHABET[(value >> 12) & 63],
                ALPHABET[(value >> 18) & 63],
            )
        )
        cursor -= 3
    return "".join(output)


def _encrypt_zse_v4(value: str) -> str:
    plain = bytearray((210, 0))
    plain.extend(quote(value, safe="~()*!").encode("ascii"))
    pad = 16 - (len(plain) % 16)
    plain.extend([pad] * pad)
    first = bytearray(16)
    for index in range(16):
        first[index] = plain[index] ^ KEY16[index] ^ 42
    initial = _r_block(bytes(first))
    cipher = bytearray(len(plain))
    cipher[:16] = initial
    if len(plain) > 16:
        cipher[16:] = _x_blocks(bytes(plain[16:]), initial)
    return _custom_encode(bytes(cipher))


def sign_zhihu_fetch_request(
    url: str,
    dc0: str = "",
    body: str | None = None,
    zse93: str = "101_3_3.0",
) -> dict[str, str]:
    parsed = urlparse(url)
    path = parsed.path or "/"
    if parsed.query:
        path += f"?{parsed.query}"
    source = [zse93, path, dc0]
    if body is not None:
        source.append(body)
    digest = hashlib.md5("+".join(source).encode()).hexdigest()
    return {
        "x-zse-93": zse93,
        "x-zse-96": f"2.0_{_encrypt_zse_v4(digest)}",
        "x-requested-with": "fetch",
    }
