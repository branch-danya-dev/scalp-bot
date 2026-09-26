"""Audit arithmetic/data availability, not strategy profitability tests."""
import importlib.util
from pathlib import Path

import pytest

spec=importlib.util.spec_from_file_location('entry_audit',Path(__file__).resolve().parents[1]/'scripts/audit-entry-edge.py')
audit=importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def test_vwap_uses_all_required_depth_and_refuses_missing_quantity():
    assert audit.sweep([(100,2),(101,3)],4)==100.5
    assert audit.sweep([(100,2),(101,3)],6) is None


@pytest.mark.parametrize('sign,exit_price,gross,exit_fill',[(1,101,2,100.9899),(-1,99,2,99.0099)])
def test_markout_does_not_charge_embedded_entry_friction_twice(sign,exit_price,gross,exit_fill):
    result=audit.markout(sign,2,100,.11,exit_price,.00055,.0001)
    assert result['quote_gross_usd']==gross
    assert result['exit_fill']==pytest.approx(exit_fill)
    assert result['net_usd']==pytest.approx(sign*(exit_fill-100)*2-.11-exit_fill*2*.00055)


def test_asof_never_uses_future_book_or_stale_depth():
    first=dict(mono=10,deepReceipt=10,fastReceipt=10,healthy=True,bid=99,ask=100)
    future=dict(first,mono=12,deepReceipt=12,fastReceipt=12,bid=90)
    assert audit.quote_at([first,future],[10,12],9) is None
    assert audit.quote_at([first,future],[10,12],11)==first
    assert audit.quote_at([first,future],[10,12],11.6) is None
    assert audit.quote_at([first,future],[10,12],12)==future
    broken=dict(future,mono=12.1,healthy=False,deepReceipt=None,fastReceipt=None)
    assert audit.quote_at([first,future,broken],[10,12,12.1],12.2) is None


def test_book_gap_requires_new_snapshot():
    book=audit.Book(50)
    def message(u,kind='delta'):
        return dict(type=kind,receipt_mono_ns=10000000000,ts=123,
                    data=dict(u=u,seq=u,b=[['99','3']],a=[['100','4']]))
    assert book.update(message(1,'snapshot'),1)=='applied'
    assert book.update(message(3),2)=='gap'
    assert not book.synced
    assert book.update(message(4),3)=='unsynced'
    assert book.update(message(5,'snapshot'),4)=='applied'
    assert book.synced


def test_diagnostic_sweeps_use_same_coherent_fast_head_as_broker():
    fast, deep = audit.Book(50), audit.Book(1000)
    fast.b,fast.a = {100:2,99:3},{101:2,102:3}
    deep.b,deep.a = {100.5:900,100:999,98:7},{100.6:900,101:999,103:7}
    consistent = audit.consistent_depth(fast,deep)
    assert audit.sweep(consistent.bids,6)==pytest.approx((200+297+98)/6)
    assert audit.sweep(consistent.asks,6)==pytest.approx((202+306+103)/6)
    assert audit.sweep(consistent.asks,13) is None
