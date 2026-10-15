"""Raw-byte protocol handling and CRC verification for biosignal-mock."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple
import zlib


def extract_data_slice(raw_line: bytes) -> Optional[bytes]:
    """Extract the raw byte slice of the 'data' field array from '[' to matching ']'.

    CRITICAL SPEC REQUIREMENT:
    Compute the CRC over the raw bytes of the received line (slice out the
    data array text). Do NOT re-serialize parsed JSON, because whitespace
    would change the CRC.
    """
    key = b'"data":'
    idx = raw_line.find(key)
    if idx == -1:
        return None

    # Find the opening bracket of the array
    start_bracket = raw_line.find(b"[", idx + len(key))
    if start_bracket == -1:
        return None

    # Track balanced brackets to find matching closing bracket
    depth = 0
    end_bracket = -1
    in_string = False
    escape = False

    for i in range(start_bracket, len(raw_line)):
        b = raw_line[i : i + 1]
        if escape:
            escape = False
            continue
        if b == b"\\":
            escape = True
            continue
        if b == b'"':
            in_string = not in_string
            continue
        if not in_string:
            if b == b"[":
                depth += 1
            elif b == b"]":
                depth -= 1
                if depth == 0:
                    end_bracket = i
                    break

    if end_bracket == -1:
        return None

    return raw_line[start_bracket : end_bracket + 1]


def verify_data_crc(raw_line: bytes, expected_crc_hex: str) -> bool:
    """Verify that zlib.crc32 of the raw 'data' slice matches expected_crc_hex."""
    data_slice = extract_data_slice(raw_line)
    if data_slice is None:
        return False

    crc_computed = zlib.crc32(data_slice) & 0xFFFFFFFF
    crc_hex = f"{crc_computed:08x}".lower()
    return crc_hex == expected_crc_hex.strip().lower()


def parse_reply_line(raw_line: bytes) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Parse a received raw line from biosignal-mock.

    Returns:
        (parsed_dict, is_crc_valid)
        If JSON parsing fails, returns (None, False).
    """
    line_stripped = raw_line.strip()
    if not line_stripped:
        return None, False

    try:
        reply_dict = json.loads(line_stripped.decode("utf-8"))
    except Exception:
        return None, False

    expected_crc = str(reply_dict.get("crc32", ""))
    crc_valid = verify_data_crc(raw_line, expected_crc)

    return reply_dict, crc_valid
