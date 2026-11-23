"""Shared loaders for the tape studies (local snapshot)."""
import sqlite3
import numpy as np, pandas as pd

import os
DB = os.environ.get("SNAP_DB", "snap.db")


def db():
    return sqlite3.connect(DB)


def load_tape(exclude_ours=True):
    c = db()
    t = pd.read_sql("SELECT exchange_id ex, created_at, side, price, size AS qty FROM trade_tape", c)
    t["ts"] = pd.to_datetime(t.created_at, utc=True, format="ISO8601")
    t["usd"] = t.price * t.qty
    t["agg_yes"] = t.side.str.upper() == "YES"
    t["ours"] = False
    if exclude_ours:
        f = pd.read_sql("SELECT exchange_id ex, filled_at, price, quantity FROM strategy_fills", c)
        f["ts"] = pd.to_datetime(f.filled_at, utc=True, format="ISO8601")
        f["yp"] = f.price.round(4)
        t["yp"] = t.price.round(4)
        # a fill may be recorded in the bought outcome's price space: match either p or 1-p
        f2 = f.assign(yp=(1 - f.price).round(4))
        ff = pd.concat([f, f2])[["ex", "ts", "yp", "quantity"]].sort_values("ts")
        tt = t.reset_index().sort_values("ts")
        m = pd.merge_asof(tt, ff.rename(columns={"ts": "fts", "quantity": "fq"}), left_on="ts", right_on="fts",
                          by=["ex", "yp"], tolerance=pd.Timedelta("5s"), direction="nearest")
        hit = m.fq.notna() & ((m.fq - m["qty"]).abs() <= 1)
        t.loc[m.loc[hit, "index"].values, "ours"] = True
    return t


def minute_series(table, col, where=""):
    c = db()
    q = f"SELECT exchange_id ex, ts, {col} v FROM {table} WHERE {col} IS NOT NULL {where}"
    s = pd.read_sql(q, c)
    s["t"] = pd.to_datetime(s.ts.str[:16], utc=True)
    return s.pivot_table(index="t", columns="ex", values="v", aggfunc="last").sort_index().asfreq("1min").ffill(limit=5)


def lookup(grid, ex_series, ts_series, offset_min=0):
    """Value of grid[ex] at floor(ts)+offset for vectors of (ex, ts). NaN when missing."""
    t = ts_series.dt.floor("min") + pd.Timedelta(minutes=offset_min)
    idx = grid.index
    pos = idx.get_indexer(t)
    out = np.full(len(t), np.nan)
    cols = {c: i for i, c in enumerate(grid.columns)}
    arr = grid.values
    for k, (p, e) in enumerate(zip(pos, ex_series)):
        ci = cols.get(e)
        if p >= 0 and ci is not None:
            out[k] = arr[p, ci]
    return out
