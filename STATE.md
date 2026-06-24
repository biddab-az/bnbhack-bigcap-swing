# STATE

- Agent: Alpha Radar (BNB Hack Track 1), spot long-only on PancakeSwap V2, USDT base.
- Signal: perp basis z-score (168h), rolling TOP-5, LONG on basis_z > +3.5 sigma. See `src/brain.py`.
- Execution: direct PancakeSwap V2 (`src/executor.py` -> `src/direct_adapter.py`), TP/SL/trailing in `src/triggers.py`.
- Brain runs on cron every 5 min, writes to `orders_inbox/`; daemon consumes and executes.
- Secrets are out of the repo: wallet key via `env:` or encrypted keystore, ClickHouse and keystore passwords via CLI args / env.
- Charts: not bundled. Live trade history so far is too thin to plot a meaningful equity curve; skipped intentionally rather than ship a misleading chart.
