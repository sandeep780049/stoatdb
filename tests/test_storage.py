"""Tests for the pager and the B-tree storage layer."""

from __future__ import annotations

import pytest

from stoatdb.errors import RowTooLarge
from stoatdb.storage.btree import BTree, MAX_PAYLOAD
from stoatdb.storage.pager import Pager


@pytest.fixture
def pager(tmp_path):
    p = Pager(str(tmp_path / "store.db"), create=True)
    try:
        yield p
    finally:
        p.close()


def test_page_roundtrip(pager):
    page = pager.read_page(0)
    page[16:20] = b"test"
    pager.write_page(0, page)
    assert pager.read_page(0)[16:20] == b"test"


def test_alloc_and_freelist(tmp_path):
    p = Pager(str(tmp_path / "f.db"), create=True)
    a = p.alloc_page()
    b = p.alloc_page()
    p.free_page(a)
    p.flush()
    p.close()
    # reopen: freelist head must have persisted
    p2 = Pager(str(tmp_path / "f.db"))
    assert p2.freelist_head == a
    c = p2.alloc_page()
    assert c == a  # recycled
    p2.close()
    _ = b


def test_freelist_reuse(pager):
    first = pager.alloc_page()
    pager.free_page(first)
    assert pager.alloc_page() == first


def test_catalog_roundtrip(tmp_path):
    p = Pager(str(tmp_path / "c.db"), create=True)
    p.save_catalog({"tables": {"t": {"root": 7, "next_rowid": 3}}})
    p.flush()
    p.close()
    p2 = Pager(str(tmp_path / "c.db"))
    assert p2.load_catalog() == {"tables": {"t": {"root": 7, "next_rowid": 3}}}
    p2.close()


def test_btree_empty_scan(bt=None, pager=pager):
    tree = BTree(pager, None)
    assert list(tree.scan()) == []
    assert len(tree) == 0
    assert tree.get(1) is None


def test_btree_set_get_delete(pager):
    tree = BTree(pager, None)
    tree.set(5, b"five")
    tree.set(3, b"three")
    tree.set(8, b"eight")
    assert tree.get(3) == b"three"
    assert tree.get(8) == b"eight"
    assert tree.get(4) is None
    assert tree.delete(3) is True
    assert tree.delete(3) is False
    assert tree.get(3) is None
    assert tree.get(5) == b"five"


def test_btree_upsert_overwrites(pager):
    tree = BTree(pager, None)
    tree.set(1, b"a")
    tree.set(1, b"b")
    assert tree.get(1) == b"b"


def test_btree_scan_sorted_after_splits(pager):
    tree = BTree(pager, None)
    payload = b"x" * 200  # forces frequent splits
    for key in range(1, 60):
        tree.set(key, payload + bytes([key % 256]))
    keys = [k for k, _p in tree.scan()]
    assert keys == list(range(1, 60))
    assert len(tree) == 59
    for key in (1, 29, 30, 59):
        assert tree.get(key) == payload + bytes([key % 256])


def test_btree_descending_inserts(pager):
    tree = BTree(pager, None)
    for key in range(50, 0, -1):
        tree.set(key, b"row")
    assert [k for k, _ in tree.scan()] == list(range(1, 51))


def test_btree_delete_from_split_tree(pager):
    tree = BTree(pager, None)
    for key in range(40):
        tree.set(key, b"r" * 150)
    for key in range(0, 40, 2):
        assert tree.delete(key) is True
    assert [k for k, _ in tree.scan()] == list(range(1, 40, 2))


def test_row_too_large(pager):
    tree = BTree(pager, None)
    with pytest.raises(RowTooLarge):
        tree.set(1, b"z" * (MAX_PAYLOAD + 1))


def test_reopen_persistence(tmp_path):
    p = Pager(str(tmp_path / "r.db"), create=True)
    tree = BTree(p, None)
    for key in range(20):
        tree.set(key, f"row-{key}".encode())
    root = tree.root
    p.save_catalog({"root": root})
    p.close()

    p2 = Pager(str(tmp_path / "r.db"))
    cat = p2.load_catalog()
    tree2 = BTree(p2, cat["root"])
    assert len(tree2) == 20
    assert tree2.get(7) == b"row-7"
    p2.close()
