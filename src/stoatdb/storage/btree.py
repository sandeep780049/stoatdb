"""On-disk B+tree over the pager's page space.

Layout (all integers big-endian)::

    leaf page      : b"LF" | next_leaf u32 | count u32 | entries...
    entry          : key u64 | payload_len u32 | payload bytes
    internal page  : b"IT" | count u32 | first_child u32 | separators...
    separator      : sep_key u64 | child u32

Leaves hold ``(rowid, row payload)`` pairs sorted by key; internal nodes route
searches. Pages split on overflow; the tree grows at the root, so only the
catalog's stored root page number can change after an insert.

Deletions remove the entry from its leaf and leave the page allocated
(no coalescing yet — see the README roadmap).
"""

from __future__ import annotations

import struct
from collections.abc import Iterator

from ..errors import RowTooLarge
from .pager import PAGE_SIZE, Pager

_MAGIC_LEAF = b"LF"
_MAGIC_INTERNAL = b"IT"

# leaf: magic[0:2] next_leaf[2:6] count[6:10] entries[10:]
_LEAF_NEXT_OFF = 2
_LEAF_COUNT_OFF = 6
_LEAF_ENTRIES_OFF = 10
# internal: magic[0:2] count[2:6] first_child[6:10] separators[10:]
_INT_COUNT_OFF = 2
_INT_CHILD_OFF = 6
_INT_SEPS_OFF = 10

_ENTRY = struct.Struct(">QI")
_SEP = struct.Struct(">QI")
_U32 = struct.Struct(">I")

MAX_PAYLOAD = 1200  # per-row cap: guarantees an entry fits in a fresh leaf


class BTree:
    """A mutable B+tree rooted at a page number inside a :class:`Pager`."""

    def __init__(self, pager: Pager, root: int | None) -> None:
        self.pager = pager
        self.root = root  # None means "empty tree"

    # ---------------------------------------------------------------- lookups

    def get(self, key: int) -> bytes | None:
        if self.root is None:
            return None
        page_no = self.root
        while True:
            page = self.pager.read_page(page_no)
            if page[:2] == _MAGIC_INTERNAL:
                page_no = self._route(page, key)
                continue
            entries = self._leaf_entries(page)
            lo, hi = 0, len(entries)
            while lo < hi:
                mid = (lo + hi) // 2
                if entries[mid][0] < key:
                    lo = mid + 1
                else:
                    hi = mid
            if lo < len(entries) and entries[lo][0] == key:
                return entries[lo][1]
            return None

    def scan(self) -> Iterator[tuple[int, bytes]]:
        """Yield every ``(key, payload)`` in ascending key order."""
        if self.root is None:
            return
        yield from self._scan_page(self.root)

    def __len__(self) -> int:
        return sum(1 for _ in self.scan())

    # ---------------------------------------------------------------- mutation

    def set(self, key: int, payload: bytes) -> None:
        if len(payload) > MAX_PAYLOAD:
            raise RowTooLarge(
                f"row payload is {len(payload)} bytes; the per-row limit is {MAX_PAYLOAD}"
            )
        if self.root is None:
            leaf = self._new_leaf([], 0)
            self.root = leaf
        split = self._insert(self.root, key, payload)
        if split is not None:
            sep, right = split
            new_root = self._new_internal(self.root, [(sep, right)])
            self.root = new_root

    def delete(self, key: int) -> bool:
        """Remove *key* if present. Returns whether anything was removed."""
        if self.root is None:
            return False
        page_no = self.root
        while True:
            page = self.pager.read_page(page_no)
            if page[:2] == _MAGIC_INTERNAL:
                page_no = self._route(page, key)
                continue
            entries = self._leaf_entries(page)
            for idx, (k, _payload) in enumerate(entries):
                if k == key:
                    del entries[idx]
                    self.pager.write_page(page_no, self._pack_leaf(entries, self._leaf_next(page)))
                    return True
                if k > key:
                    return False
            return False

    def free_all(self) -> None:
        """Return every page of the tree to the pager's freelist."""
        if self.root is None:
            return
        stack = [self.root]
        while stack:
            page_no = stack.pop()
            page = self.pager.read_page(page_no)
            if page[:2] == _MAGIC_INTERNAL:
                (count,) = _U32.unpack_from(page, _INT_COUNT_OFF)
                (first,) = _U32.unpack_from(page, _INT_CHILD_OFF)
                stack.append(first)
                for i in range(count):
                    (_sep, child) = _SEP.unpack_from(page, _INT_SEPS_OFF + i * _SEP.size)
                    stack.append(child)
            self.pager.free_page(page_no)
        self.root = None

    # ---------------------------------------------------------------- internal

    def _route(self, page: bytearray, key: int) -> int:
        (count,) = _U32.unpack_from(page, _INT_COUNT_OFF)
        (child,) = _U32.unpack_from(page, _INT_CHILD_OFF)
        for i in range(count):
            sep, nxt = _SEP.unpack_from(page, _INT_SEPS_OFF + i * _SEP.size)
            if key < sep:
                return child
            child = nxt
        return child

    def _scan_page(self, page_no: int) -> Iterator[tuple[int, bytes]]:
        page = self.pager.read_page(page_no)
        if page[:2] == _MAGIC_INTERNAL:
            (count,) = _U32.unpack_from(page, _INT_COUNT_OFF)
            (child,) = _U32.unpack_from(page, _INT_CHILD_OFF)
            yield from self._scan_page(child)
            for i in range(count):
                sep, nxt = _SEP.unpack_from(page, _INT_SEPS_OFF + i * _SEP.size)
                yield from self._scan_page(nxt)
        else:
            yield from self._leaf_entries(page)

    def _insert(self, page_no: int, key: int, payload: bytes) -> tuple[int, int] | None:
        """Insert into the subtree rooted at *page_no*.

        Returns ``(separator, new_right_page)`` when the page split, else None.
        """
        page = self.pager.read_page(page_no)
        if page[:2] == _MAGIC_INTERNAL:
            (count,) = _U32.unpack_from(page, _INT_COUNT_OFF)
            (child,) = _U32.unpack_from(page, _INT_CHILD_OFF)
            sep_off = _INT_SEPS_OFF
            separators = [_SEP.unpack_from(page, sep_off + i * _SEP.size)
                          for i in range(count)]
            child_idx = count
            for i, (sep, _nxt) in enumerate(separators):
                if key < sep:
                    child_idx = i
                    break
            target = child if child_idx == 0 else separators[child_idx - 1][1]
            split = self._insert(target, key, payload)
            if split is None:
                return None
            sep, right = split
            separators.insert(child_idx, (sep, right))
            packed = self._pack_internal(child, separators)
            if len(packed) <= PAGE_SIZE:
                self.pager.write_page(page_no, packed)
                return None
            return self._split_internal(page_no, child, separators)
        entries = self._leaf_entries(page)
        lo, hi = 0, len(entries)
        while lo < hi:
            mid = (lo + hi) // 2
            if entries[mid][0] < key:
                lo = mid + 1
            else:
                hi = mid
        if lo < len(entries) and entries[lo][0] == key:
            entries[lo] = (key, payload)
        else:
            entries.insert(lo, (key, payload))
        packed = self._pack_leaf(entries, self._leaf_next(page))
        if len(packed) <= PAGE_SIZE:
            self.pager.write_page(page_no, packed)
            return None
        return self._split_leaf(page_no, entries, self._leaf_next(page))

    def _split_leaf(self, page_no: int, entries: list, next_leaf: int) -> tuple[int, int]:
        mid = len(entries) // 2
        left, right_entries = entries[:mid], entries[mid:]
        right_no = self._new_leaf(right_entries, next_leaf)
        self.pager.write_page(page_no, self._pack_leaf(left, right_no))
        return right_entries[0][0], right_no

    def _split_internal(self, page_no: int, first_child: int,
                        separators: list[tuple[int, int]]) -> tuple[int, int]:
        mid = len(separators) // 2
        up_sep, up_child = separators[mid]
        right_seps = separators[mid + 1:]
        left_seps = separators[:mid]
        right_no = self._new_internal(up_child, right_seps)
        self.pager.write_page(page_no, self._pack_internal(first_child, left_seps))
        return up_sep, right_no

    # ------------------------------------------------------------ page packing

    @staticmethod
    def _leaf_entries(page: bytearray) -> list[tuple[int, bytes]]:
        (count,) = _U32.unpack_from(page, _LEAF_COUNT_OFF)
        entries = []
        off = _LEAF_ENTRIES_OFF
        for _ in range(count):
            key, length = _ENTRY.unpack_from(page, off)
            off += _ENTRY.size
            entries.append((key, bytes(page[off:off + length])))
            off += length
        return entries

    @staticmethod
    def _leaf_next(page: bytearray) -> int:
        return _U32.unpack_from(page, _LEAF_NEXT_OFF)[0]

    def _new_leaf(self, entries: list[tuple[int, bytes]], next_leaf: int) -> int:
        page_no = self.pager.alloc_page()
        self.pager.write_page(page_no, self._pack_leaf(entries, next_leaf))
        return page_no

    def _new_internal(self, first_child: int, separators: list[tuple[int, int]]) -> int:
        page_no = self.pager.alloc_page()
        self.pager.write_page(page_no, self._pack_internal(first_child, separators))
        return page_no

    @staticmethod
    def _pack_leaf(entries: list[tuple[int, bytes]], next_leaf: int) -> bytes:
        parts = [_MAGIC_LEAF, _U32.pack(next_leaf), _U32.pack(len(entries))]
        for key, payload in entries:
            parts.append(_ENTRY.pack(key, len(payload)))
            parts.append(payload)
        return b"".join(parts).ljust(PAGE_SIZE, b"\x00")

    @staticmethod
    def _pack_internal(first_child: int, separators: list[tuple[int, int]]) -> bytes:
        parts = [_MAGIC_INTERNAL, _U32.pack(len(separators)), _U32.pack(first_child)]
        for sep, child in separators:
            parts.append(_SEP.pack(sep, child))
        return b"".join(parts).ljust(PAGE_SIZE, b"\x00")
