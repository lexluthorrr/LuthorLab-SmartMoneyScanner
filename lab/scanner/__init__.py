"""SCANNER: read-only collectors for public market data. No API keys.

perps      - Hyperliquid whale positions and perp P&L leaders, big USDT/USDC transfers on Ethereum
memes      - DexScreener / pump.fun / GeckoTerminal / Solana RPC / RugCheck
polymarket - leaders with profile numbers and big bets in the last 24 h
signals    - fresh wallets, precisely timed bets, losers, celebrity markets, stocks, AI trends
"""

MODULES = ("perps", "memes", "polymarket", "signals")
