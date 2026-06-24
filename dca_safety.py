"""dca_safety.py — Min 1 trade/day fallback для не-DQ.

Логика:
1. Раз в день (22:30 UTC) проверяем сколько trades было сегодня
2. Если 0 — форсируем малый buy на TOP-5[0] токен
3. Сумма small (0.005 BNB ~ $3) — minimal trading capital hit, но satisfies min-trade rule

Hackathon rule: "Minimum trades to qualify: at least 1 trade per day (7 over the trading week)"
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone, timedelta

# Re-use brain's symbol → address mapping and TOP-5 computation
sys.path.insert(0, '/opt/alpha-radar')
from src.brain import (
    SYMBOL_TO_ADDR, TOKEN_SYMBOLS,
    TP_PCT, SL_PCT,
    pull_data, build_features, compute_rolling_top_k,
    init_brain_db, BRAIN_DB,
)
import clickhouse_connect

log = logging.getLogger("dca")
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(name)s] %(message)s")

DCA_AMOUNT_BNB = 0.05  # $3 — small forced trade
DCA_LOOKBACK_HOURS = 22  # if no trades in 22h, force DCA


def count_trades_today(conn):
    """Count brain signals emitted in last 22h (proxy for actual on-chain trades)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=DCA_LOOKBACK_HOURS)).isoformat()
    n = conn.execute(
        "SELECT COUNT(*) FROM signals_emitted WHERE ts >= ?", (cutoff,)
    ).fetchone()[0]
    return n


def count_orders_in_inbox_today(inbox_dir):
    """Count order JSON files dropped today (including processed/failed)."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=DCA_LOOKBACK_HOURS)
    cutoff_ts = cutoff.timestamp()
    n = 0
    for sub in ['', 'processed', 'failed']:
        d = os.path.join(inbox_dir, sub) if sub else inbox_dir
        if not os.path.isdir(d): continue
        for f in os.listdir(d):
            if not f.endswith('.json'): continue
            fp = os.path.join(d, f)
            if os.path.getmtime(fp) >= cutoff_ts:
                n += 1
    return n


def emit_dca_order(inbox_dir, wallet, token_symbol, amount_bnb):
    if token_symbol not in SYMBOL_TO_ADDR:
        log.error(f'no BSC address for {token_symbol}')
        return None
    addr = SYMBOL_TO_ADDR[token_symbol]
    bar_ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M')
    coid = f'dca_safety_{bar_ts}_{token_symbol}_{uuid.uuid4().hex[:6]}'
    order = {
        'client_order_id': coid,
        'wallet': wallet,
        'side': 'buy',
        'token': addr,
        'amount': amount_bnb,
        'type': 'market',
        'take_profit_pct': TP_PCT,
        'stop_loss_pct': SL_PCT,
        'tag': f'DCA_SAFETY min_trade_qualifier sym={token_symbol}',
    }
    os.makedirs(inbox_dir, exist_ok=True)
    fname = os.path.join(inbox_dir, f'{coid}.json')
    tmp = fname + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(order, f, indent=2)
    os.replace(tmp, fname)
    log.info(f'DCA SAFETY emitted: BUY {token_symbol} {amount_bnb} BNB → {fname}')
    return coid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--inbox', default='/opt/alpha-radar/orders_inbox')
    ap.add_argument('--wallet', default='body1')
    ap.add_argument('--amount-bnb', type=float, default=DCA_AMOUNT_BNB)
    ap.add_argument('--ch-host', default='localhost')
    ap.add_argument('--ch-pass', default='<CH_PASSWORD>')
    ap.add_argument('--force', action='store_true', help='emit DCA even if recent trades exist')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    log.info(f'dca_safety checking (lookback={DCA_LOOKBACK_HOURS}h, amount={args.amount_bnb})')

    conn = init_brain_db()
    n_signals = count_trades_today(conn)
    n_inbox = count_orders_in_inbox_today(args.inbox)
    log.info(f'last {DCA_LOOKBACK_HOURS}h: brain_signals={n_signals} inbox_files={n_inbox}')

    if not args.force and (n_signals + n_inbox) > 0:
        log.info('PASS — trades already happened today, no DCA needed')
        return

    log.info('NO TRADES TODAY — emitting DCA safety order')

    # Compute current TOP-5 to pick best target
    ch = clickhouse_connect.get_client(
        host=args.ch_host, port=8123, user='mcp_agent', password=args.ch_pass,
        settings={'max_threads':4,'max_memory_usage':4*1024**3,'max_execution_time':60}
    )
    kdf, bdf = pull_data(ch)
    if kdf.empty:
        log.warning('no data — falling back to BEAT (cycle 77 best historic per-trade)')
        target = 'BEAT'
    else:
        df = build_features(kdf, bdf)
        top_k = compute_rolling_top_k(df)
        target = top_k[0] if top_k else 'BEAT'

    log.info(f'DCA target: {target}')

    if args.dry_run:
        log.info(f'[DRY-RUN] would emit BUY {target} {args.amount_bnb} BNB')
        return

    coid = emit_dca_order(args.inbox, args.wallet, target, args.amount_bnb)
    log.info(f'DCA emitted: {coid}')


if __name__ == '__main__':
    main()
