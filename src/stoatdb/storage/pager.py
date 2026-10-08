"""Paged file storage.

A stoatdb file is a sequence of fixed 4096-byte pages. Page 0 is the header
(magic, format version, page count, free-list head, catalog head). The table
catalog is stored on a chain of pages starting at ``catalog_head``; table data
lives in B-trees allocated from the same page space.

Writes are buffered in memory and flushed (with ``fsync``) when a statement
completes, giving best-effort durability at statement granularity. Clean pages
stay in a cache so tree traversals do not re-read the file.
"""

from __future__ import annotations

import json
import os
import struct
from pathlib import Path

MAGIC = b"STOATDB\x00"  # exactly 8 bytes; the struct field is 8s
VERSION = 1
PAGE_SIZE = 4096
MAX_CACHED_PAGES = 8192  # ~32 MB of cached pages before clean-page eviction

_HEADER = struct.Struct(">8sIIII")  # magic, version, page_count, freelist, catalog_head


class Pager:
    """Owns the database file: page allocation, catalog chain, durability."""

    def __init__(self, path: str | os.PathLike[str], create: bool = False) -> None:
        self.path = Path(path)
        self._cache: dict[int, bytes] = {}   # page_no -> latest contents
        self._dirty: set[int] = set()        # pages modified since last flush
        existed = self.path.exists() and self.path.stat().st_size > 0
        if create or not existed:
            # "w+b" truncates the file — used for both fresh and re-create paths.
            self._file = open(self.path, "w+b")
            self.page_count = 1  # header page
            self.freelist_head = 0
            self.catalog_head = 0
            self._write_header()
            self.flush()
        else:
            self._file = open(self.path, "r+b")
            magic, version, self.page_count, self.freelist_head, self.catalog_head = (
                self._read_header()
            )
            if magic != MAGIC:
                raise ValueError(f"{self.path} is not a stoatdb file")
            if version != VERSION:
                raise ValueError(f"unsupported format version {version}")

    # ------------------------------------------------------------------ pages

    def read_page(self, page_no: int) -> bytearray:
        cached = self._cache.get(page_no)
        if cached is not None:
            return bytearray(cached)
        self._file.seek(page_no * PAGE_SIZE)
        data = self._file.read(PAGE_SIZE)
        if len(data) < PAGE_SIZE:
            data += bytes(PAGE_SIZE - len(data))
        self._cache[page_no] = data
        self._evict_if_needed()
        return bytearray(data)

    def write_page(self, page_no: int, data: bytes | bytearray) -> None:
        if len(data) != PAGE_SIZE:
            raise ValueError("page payload must be exactly PAGE_SIZE bytes")
        block = bytes(data)
        self._cache[page_no] = block
        self._dirty.add(page_no)
        self._evict_if_needed()

    def _evict_if_needed(self) -> None:
        """Drop oldest clean pages when the cache exceeds its budget."""
        if len(self._cache) <= MAX_CACHED_PAGES:
            return
        for page_no in list(self._cache):
            if page_no in self._dirty:
                continue
            del self._cache[page_no]
            if len(self._cache) <= MAX_CACHED_PAGES:
                return

    def alloc_page(self) -> int:
        if self.freelist_head:
            page_no = self.freelist_head
            nxt = struct.unpack_from(">I", self.read_page(page_no), 0)[0]
            self.freelist_head = nxt
            return page_no
        page_no = self.page_count
        self.page_count += 1
        return page_no

    def free_page(self, page_no: int) -> None:
        block = struct.pack(">I", self.freelist_head) + bytes(PAGE_SIZE - 4)
        self.write_page(page_no, block)
        self.freelist_head = page_no

    # --------------------------------------------------------------- catalog

    def load_catalog(self) -> dict:
        if not self.catalog_head:
            return {}
        chunks: list[bytes] = []
        page_no = self.catalog_head
        while page_no:
            page = self.read_page(page_no)
            (nxt,) = struct.unpack_from(">I", page, 0)
            chunks.append(bytes(page[4:PAGE_SIZE]))
            page_no = nxt
        raw = b"".join(chunks)
        # JSON never contains raw NUL bytes, so padding can be stripped safely.
        return json.loads(raw.decode("utf-8").rstrip("\x00"))

    def save_catalog(self, catalog: dict) -> None:
        raw = json.dumps(catalog, sort_keys=True).encode("utf-8")
        max_payload = PAGE_SIZE - 4
        pages_needed = max(1, -(-len(raw) // max_payload))
        # release the old chain
        page_no = self.catalog_head
        while page_no:
            nxt = struct.unpack_from(">I", self.read_page(page_no), 0)[0]
            self.free_page(page_no)
            page_no = nxt
        # write the new chain
        new_head = 0
        prev = 0
        for i in range(pages_needed):
            page_no = self.alloc_page()
            if prev:
                prev_page = bytearray(self.read_page(prev))
                struct.pack_into(">I", prev_page, 0, page_no)
                self.write_page(prev, prev_page)
            else:
                new_head = page_no
            block = bytearray(PAGE_SIZE)
            struct.pack_into(">I", block, 0, 0)  # next pointer patched above
            chunk = raw[i * max_payload:(i + 1) * max_payload]
            block[4:4 + len(chunk)] = chunk
            self.write_page(page_no, block)
            prev = page_no
        self.catalog_head = new_head

    # ------------------------------------------------------------ durability

    def flush(self) -> None:
        for page_no in sorted(self._dirty):
            self._file.seek(page_no * PAGE_SIZE)
            self._file.write(self._cache[page_no])
        self._dirty.clear()
        self._write_header()
        self._file.flush()
        os.fsync(self._file.fileno())

    def close(self) -> None:
        self.flush()
        self._file.close()

    # --------------------------------------------------------------- internal

    def _read_header(self) -> tuple[bytes, int, int, int, int]:
        self._file.seek(0)
        raw = self._file.read(PAGE_SIZE)
        return _HEADER.unpack_from(raw, 0)

    def _write_header(self) -> None:
        block = bytearray(PAGE_SIZE)
        _HEADER.pack_into(block, 0, MAGIC, VERSION, self.page_count,
                          self.freelist_head, self.catalog_head)
        self._file.seek(0)
        self._file.write(bytes(block))
