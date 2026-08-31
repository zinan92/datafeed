# Market Data Database

The bounded context for maintaining durable, source-identified market candle data for Park's research systems. It is designed to grow by expanding its instrument universe without weakening its data, recovery, or operating contracts.

## Language

**Market Data Database**:
A long-lived maintained source of truth for market candles, their instrument identities, and their source provenance.
_Avoid_: Cache, scrape dump, throwaway database, temporary datafeed

**MVP Universe**:
A deliberately bounded set of 100 A-share stocks and 100 US-listed company stocks, plus the approved cross-market index/commodity/crypto instruments, used to prove the real database lifecycle before expanding coverage. Membership combines objective liquidity with explicit theme coverage; it reduces breadth, not durability, correctness, recoverability, or observability.
_Avoid_: Toy dataset, fixture universe, all-market database

**Candle Instrument**:
An instrument or price index that can truthfully provide OHLC candles for its declared timeframes. Daily Treasury yield and curve-level series such as DGS2, DGS10, and T10Y2Y are outside this database.
_Avoid_: Macro level series, synthetic Treasury candle

**Index Identity**:
A canonical market index is stored as its own instrument identity, separate from an ETF proxy that tracks it. The MVP uses S&P 500 Index (`SPX`) and Nasdaq-100 Index (`NDX`), not SPY or QQQ substitutes.
_Avoid_: ETF proxy as index, SPY-as-SPX, QQQ-as-NDX

**MVP Manifest Freeze**:
After the first successful end-to-end run, the 100 A-share and 100 US-stock members remain unchanged for 30 calendar days. New hot or liquid candidates enter a reserve pool and only join through a versioned manifest change.
_Avoid_: Daily silent rotation, live ranking as membership, replacing missing data with a new stock
