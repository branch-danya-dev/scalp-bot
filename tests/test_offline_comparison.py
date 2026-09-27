import sqlite3
from scalp_bot.offline_comparison import markout


def quotes():
    db=sqlite3.connect(':memory:')
    db.execute('CREATE TABLE quotes(symbol TEXT,mono REAL,seq INTEGER,bid REAL,ask REAL)')
    db.executemany('INSERT INTO quotes VALUES(?,?,?,?,?)',[('AAA',i,i,100+i*.01,100.01+i*.01) for i in range(64)])
    return db


def test_markout_starts_from_available_executable_quote_and_counts_costs_once():
    db=quotes();r=markout(db,'AAA','long',.5,60)
    assert r['entry_sequence']==0 and r['entry']==100.01
    assert r['diagnostic_net_bps']==r['gross_markout_bps']-13
    short=markout(db,'AAA','short',.5,60)
    assert short['entry']==100 and short['gross_markout_bps']<0


def test_unobserved_interval_or_end_cannot_be_a_payoff():
    db=quotes();db.execute('DELETE FROM quotes WHERE mono BETWEEN 20 AND 30')
    assert markout(db,'AAA','long',.5,60)=={'status':'quote_gap'}
    assert markout(db,'AAA','long',61,60)=={'status':'right_censored'}
    assert markout(db,'AAA','long',-.5,60)=={'status':'initial_quote_unavailable'}
