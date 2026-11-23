"""itch -- NASDAQ TotalView-ITCH 5.0 decoder, order book reconstructor, and analytics.

The exchange broadcasts *changes*, not state. Rebuilding the book from that delta feed
is the whole exercise; see :mod:`itch.book`.
"""

from __future__ import annotations

__version__ = "0.1.0"

from .book import MultiBook, OrderBook, OrderNode, PriceLevel
from .decoder import Message, decode, iter_decode, read_file
from .fast_decoder import DecodedBatch, book_events, decode_columnar
from .generator import SyntheticStream, encode
from .messages import PRICE_SCALE, price_to_decimal
from .replay import OracleReport, replay

__all__ = [
    "PRICE_SCALE",
    "DecodedBatch",
    "Message",
    "MultiBook",
    "OracleReport",
    "OrderBook",
    "OrderNode",
    "PriceLevel",
    "SyntheticStream",
    "book_events",
    "decode",
    "decode_columnar",
    "encode",
    "iter_decode",
    "price_to_decimal",
    "read_file",
    "replay",
]
