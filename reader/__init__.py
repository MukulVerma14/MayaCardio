"""Reader client package for reading biosignal mock data."""

from reader.client import ReaderClient
from reader.protocol import parse_reply_line, verify_data_crc

__all__ = ["ReaderClient", "parse_reply_line", "verify_data_crc"]
