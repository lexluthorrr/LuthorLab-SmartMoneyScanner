# TESTER: six filters before you trust an edge

Most trading ideas look great on the first test. This checklist is the order in which they
usually die. Run every idea through all six filters; stop at the first one it fails.

The example numbers below come from 13 real ideas tested on Polymarket, Kalshi, Hyperliquid
and BTC options data. Each filter killed at least one of them.

| # | Filter | The question | Real example |
|---|--------|--------------|--------------|
| 1 | In-sample | Does the rule work on the data where I found it? | 43 technical-analysis tests: best one had \|t\| = 2.53. Corrected for running 43 tests, p x N = 0.49: noise. |
| 2 | Out-of-sample | Does it work on data it has never seen? | 97.9% win rate on 48 bars (entry ~51c) -> 72.4% on 116 new bars (entry ~71.4c), about one point above break-even. |
| 3 | Fees | Is anything left after the taker fee? | "Ride the favorite": 89% win rate at an average entry of 86.2c. After the taker fee the t-stat fell 3.21 -> 1.24 (SOL) and 1.84 -> 0.71 (BTC). |
| 4 | Honest fill | Would my order have filled at that price, with a real queue in front of me? | Cloning a whale's trades showed +$159,820. Only ~19.8% of his volume was reachable for a copier, and copying just that part gave -$2,657. |
| 5 | Pessimism | Does it survive a worst-case haircut on every leg? | Entering 3 minutes before close: t = 3.96 on paper prices, 2.55 at the prices actually paid (1.08c worse on average). The bar was 3. |
| 6 | Tiny live | Does real money agree with the simulation? | Buying both sides of a market below $1: backtest +7.04%, live 250 bars: $4,517.81 in, $4,323.62 back (-$194.19, win rate 34%). |

## How to run each filter

1. **In-sample.** Count how many variants you tried. The best of N random rules shows
   t ~ sqrt(2 ln N) by luck alone (about 2.9 for 72 variants). Correct for it or you are
   measuring your own search.
2. **Out-of-sample.** Freeze the rule and its parameters, then run it on later data. A walk-forward
   (pick on one window, test on the next, repeat) is the simplest honest version.
3. **Fees.** Subtract the real fee schedule of the venue for your order type (maker vs taker).
   A 3-point edge at 86c disappears after one taker fee.
4. **Honest fill.** Assume you enter after the signal is known (next bar), behind the existing
   queue, and that your limit orders fill mostly when price moves against you. Book-based
   simulations that assume "price touched my level, so I filled" overstate fills.
5. **Pessimism.** Stress costs, latency and fill rate, each leg separately, to worse than
   observed. Require a t-stat you set in advance (3 is a reasonable bar).
6. **Tiny live.** Trade the smallest size the venue allows and compare live numbers to row 5.
   The cheapest lesson is a small one.

Minimum sample: at least 7 days of data and 50+ trades before reading anything into a result.
Win rate alone says nothing about P&L: 86% of trades can win while the account loses money.

## Walk-forward template

`walkforward.py` runs a simple, editable rule through filters 1-5 on any price CSV.
It never places orders.

```bash
# synthetic random walk: no edge by construction, watch the in-sample "edge" vanish
python -m lab.tester.walkforward --demo

# public hourly BTC candles from Coinbase Exchange (no key), saved to data/
python -m lab.tester.walkforward --fetch BTC-USD --granularity 3600 --bars 3000

# your own CSV (columns: time, close)
python -m lab.tester.walkforward data/my_prices.csv --fee-bps 10 --slippage-bps 5 --stress 2 --t-min 3
```

Output format:

```
filter                                 trades   win %  mean bps  t-stat  verdict
--------------------------------------------------------------------------------
1 in-sample (best of grid)                ...     ...       ...     ...  pass | DEAD
2 out-of-sample                           ...
3 after fees (10 bps/side)                ...
4 honest fill (next bar, +5 bps/side)     ...
5 pessimism (costs x2)                    ...
6 tiny live                                         yours: small real money, compare with row 5
```

To test your own idea, edit `PARAM_GRID` and `rule()` at the top of `walkforward.py`.
`rule()` must use only prices up to and including the current bar.
