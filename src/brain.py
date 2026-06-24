"""src/brain.py — Signal generator for Alpha Radar.

Pulls Binance perp basis data from ClickHouse, computes basis_z (168h rolling),
applies rolling TOP-5 selection (forward-looking, no look-ahead), and emits
LONG orders to orders_inbox/ when basis_z > +3.5σ on a TOP-5 token.

Validated strategy: cycle 76/79 p=0/200, Z=+4.35, +245bp/trade NET (BOTH sides
backtest on Binance perp). LONG-only on PancakeSwap spot = +113bp/trade NET
(cycle 90).

Architecture: brain writes JSON files into orders_inbox/, daemon picks them up
atomically and executes on PancakeSwap.

Run via cron every 5 min:
  */5 * * * * /opt/alpha-v2/ml/venv/bin/python /opt/alpha-radar/src/brain.py \\
              --config /opt/alpha-radar/config.yaml \\
              --inbox /opt/alpha-radar/orders_inbox \\
              --ch-pass <pwd>
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd
import yaml
import clickhouse_connect

log = logging.getLogger("brain")
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(name)s] %(message)s")

# === FROZEN strategy params from cycle 76/79/92 ===
HOLD_BARS = 48          # 4h on 5m bars (cycles 95/96/97 all confirmed peak, p=0.0000 Z=+3.96)
Z_THR = 3.5             # basis_z threshold
TP_PCT = 0.12           # +12% take profit (cycle 100: +25% PnL uplift vs 8%, accept overfit risk per user)
SL_PCT = 0.15           # 15% catastrophe stop (positive value, executor handles sign)
TOP_K = 5               # rolling TOP-K
LOOKBACK_DAYS = 14      # cycle 96 grid: +49% PnL uplift vs LB=21
COST_PER_SIDE = 0.0008  # 8bp/side
SIGNAL_COOLDOWN_HOURS = 4  # matches HOLD_BARS=48 (4h)

# === Auto-sizing params ===
WALLET_ADDR = {'body1': '0xDED5e3f1920E9197B145317e58244bA7d78c834D'}
BSC_RPC = 'https://bsc-dataseed.binance.org'
GAS_RESERVE_BNB = 0.05       # never spend below this
DUST_THRESHOLD = 1e-9        # token balance above this counts as open position
SIZE_FRAC_FIRST = 0.60       # 60% of free BNB when zero open positions
SIZE_FRAC_SUBSEQUENT = 0.80  # 80% of remaining free BNB when 1+ open
MIN_TRADE_BNB = 0.02         # don't emit trades smaller than this

# === Liquidity protection (cycle 112: 13/17 tokens toxic on PCS V2, RAVE -98% disaster) ===
LIQUIDITY_BLACKLIST = {
    'BANANAS31', 'BARD', 'BEAT', 'DEXE', 'FF', 'GUA', 'GWEI', 'KITE',
    'MYX', 'NIGHT', 'RAVE', 'SAHARA', 'TAC',
}
MAX_ROUND_TRIP_LOSS_PCT = 20.0
_USDT_ADDR = '0x55d398326f99059fF775485246999027B3197955'
_WBNB_ADDR = '0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c'
_PCS_ROUTER = '0x10ED43C718714eb63d5aA57B78B54704E256024E'
_LIQ_ROUTER_ABI = [{'name':'getAmountsOut','type':'function','stateMutability':'view',
                    'inputs':[{'name':'','type':'uint256'},{'name':'','type':'address[]'}],
                    'outputs':[{'name':'','type':'uint256[]'}]}]

def check_liquidity(token_addr, amount_bnb):
    """Round-trip BNB→token→BNB quote via PCS V2. Returns (rt_loss_pct, ok)."""
    try:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(BSC_RPC))
        router = w3.eth.contract(address=Web3.to_checksum_address(_PCS_ROUTER), abi=_LIQ_ROUTER_ABI)
        bnb_in = int(amount_bnb * 1e18)
        path_f = [Web3.to_checksum_address(_WBNB_ADDR), Web3.to_checksum_address(token_addr)]
        tok_out = router.functions.getAmountsOut(bnb_in, path_f).call()[-1]
        path_b = [Web3.to_checksum_address(token_addr), Web3.to_checksum_address(_WBNB_ADDR)]
        bnb_back = router.functions.getAmountsOut(tok_out, path_b).call()[-1]
        rt_loss = (1 - bnb_back/bnb_in) * 100
        return rt_loss, rt_loss <= MAX_ROUND_TRIP_LOSS_PCT
    except Exception as e:
        log.warning(f'  liquidity check failed: {str(e)[:80]}')
        return None, False


# === Universe: 17 manipulators ===
# H not in PancakeSwap whitelist — exclude
TOKEN_SYMBOLS = ['BANANAS31','BARD','BEAT','COAI','DEXE','FF','GUA','GWEI',
                 'KITE','MYX','NIGHT','RAVE','SAHARA','SIREN','SKYAI','TAC']

# Symbol → BSC address (from config.yaml whitelist verified)
SYMBOL_TO_ADDR = {
    'BANANAS31': '0x3d4f0513e8a29669b960f9dbca61861548a9a760',
    'BARD':      '0xd23a186a78c0b3b805505e5f8ea4083295ef9f3a',
    'BEAT':      '0xcf3232b85b43bca90e51d38cc06cc8bb8c8a3e36',
    'COAI':      '0x0a8d6c86e1bce73fe4d0bd531e1a567306836ea5',
    'DEXE':      '0x6e88056e8376ae7709496ba64d37fa2f8015ce3e',
    'FF':        '0xac23b90a79504865d52b49b327328411a23d4db2',
    'GUA':       '0xa5c8e1513b6a08334b479fe4d71f1253259469be',
    'GWEI':      '0x30117e4bc17d7b044194b76a38365c53b72f7d49',
    'KITE':      '0x904567252d8f48555b7447c67dca23f0372e16be',
    'MYX':       '0xd82544bf0dfe8385ef8fa34d67e6e4940cc63e16',
    'NIGHT':     '0xfe930c2d63aed9b82fc4dbc801920dd2c1a3224f',
    'RAVE':      '0x97693439ea2f0ecdeb9135881e49f354656a911c',
    'SAHARA':    '0xfdffb411c4a70aa7c95d5c981a6fb4da867e1111',
    'SIREN':     '0x997a58129890bbda032231a52ed1ddc845fc18e1',
    'SKYAI':     '0x92aa03137385f18539301349dcfc9ebc923ffb10',
    'TAC':       '0x1219c409fabe2c27bd0d1a565daeed9bd9f271de',
}

# Local state DB for de-dup and brain history
BRAIN_DB = '/opt/alpha-radar/brain_state.db'


def now_utc(): return datetime.now(timezone.utc)


def init_brain_db():
    """SQLite for brain state: track signals fired to avoid duplicates."""
    conn = sqlite3.connect(BRAIN_DB)
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS signals_emitted (
        ts TEXT, token TEXT, side TEXT, basis_z REAL, entry_px REAL,
        client_order_id TEXT PRIMARY KEY, amount_bnb REAL
    );
    CREATE TABLE IF NOT EXISTS runs (
        ts TEXT PRIMARY KEY, n_signals INTEGER, n_emitted INTEGER,
        top_k_tokens TEXT, latest_bar TEXT
    );
    CREATE TABLE IF NOT EXISTS hypothetical_trades (
        bar TEXT, token TEXT, side TEXT, gross_pnl_pct REAL, net_pnl_pct REAL,
        PRIMARY KEY (bar, token)
    );
    ''')
    conn.commit()
    return conn


def pull_data(client, hours_back=200):
    """Pull last ~8d of 5m klines + 7d basis from CH."""
    start_dt = now_utc() - timedelta(hours=hours_back)
    start = start_dt.strftime('%Y-%m-%d %H:%M:%S')
    log.info(f'pulling data since {start} for {len(TOKEN_SYMBOLS)} tokens')

    kdfs = []
    for t in TOKEN_SYMBOLS:
        try:
            d = client.query_df(f"""SELECT toStartOfInterval(ts, INTERVAL 5 MINUTE) AS bar_5m,
            max(high) AS hi, min(low) AS lo, anyLast(close) AS cl
            FROM cex.kline WHERE interval='1m' AND venue='binance' AND symbol='{t}USDT'
            AND ts >= '{start}' GROUP BY bar_5m ORDER BY bar_5m""")
            if len(d):
                d['token']=t; kdfs.append(d)
        except Exception as e:
            log.warning(f'kline skip {t}: {e}')
    if not kdfs:
        return pd.DataFrame(), pd.DataFrame()
    kdf = pd.concat(kdfs, ignore_index=True).rename(columns={'cl':'close_px'})
    if kdf['bar_5m'].dt.tz is None:
        kdf['bar_5m'] = kdf['bar_5m'].dt.tz_localize('UTC')

    bdfs = []
    for t in TOKEN_SYMBOLS:
        try:
            d = client.query_df(f"SELECT toStartOfHour(utc_event_dttm) AS hour, "
                f"avg((mark_price - index_price)/nullIf(index_price,0)) AS basis "
                f"FROM cex.mark_price WHERE utc_event_dttm >= '{start}' "
                f"AND symbol IN ('{t}USDT','{t}USDC') AND index_price > 0 "
                f"GROUP BY hour HAVING basis IS NOT NULL")
            if len(d):
                d['token']=t; bdfs.append(d)
        except Exception:
            pass
    bdf = pd.concat(bdfs, ignore_index=True) if bdfs else pd.DataFrame(columns=['token','hour','basis'])
    if not bdf.empty and bdf['hour'].dt.tz is None:
        bdf['hour'] = bdf['hour'].dt.tz_localize('UTC')
    return kdf, bdf


def build_features(kdf, bdf):
    df = kdf.sort_values(['token','bar_5m']).reset_index(drop=True)
    df['hour'] = df['bar_5m'].dt.floor('1h')
    if not bdf.empty:
        df = df.merge(bdf, on=['token','hour'], how='left')
        df['basis'] = df.groupby('token')['basis'].ffill()
    else:
        df['basis'] = np.nan

    def roll_z(s, w):
        m = s.rolling(w, min_periods=w//4).mean()
        sd = s.rolling(w, min_periods=w//4).std()
        return (s-m)/sd

    df['basis_z'] = df.groupby('token')['basis'].transform(lambda s: roll_z(s, 168))
    # Forward-looking exec windows (cycle 68 bug fix)
    df['entry_px'] = df.groupby('token')['close_px'].transform(lambda s: s.shift(-1))
    df['exit_px']  = df.groupby('token')['close_px'].transform(lambda s: s.shift(-(1+HOLD_BARS)))
    df['max_hi']   = df.groupby('token')['hi'].transform(lambda s: s.rolling(HOLD_BARS, min_periods=1).max().shift(-(1+HOLD_BARS)))
    df['min_lo']   = df.groupby('token')['lo'].transform(lambda s: s.rolling(HOLD_BARS, min_periods=1).min().shift(-(1+HOLD_BARS)))
    return df


def simulate(row, side='long'):
    """Return realized NET PnL for trade (with 16bp roundtrip cost)."""
    e=row['entry_px']; x=row['exit_px']; mh=row['max_hi']; ml=row['min_lo']
    if pd.isna(e) or e<=0 or pd.isna(x) or pd.isna(mh) or pd.isna(ml): return None
    if side == 'long':
        max_ret=(mh-e)/e; min_ret=(ml-e)/e
    else:
        max_ret=(e-ml)/e; min_ret=(e-mh)/e
    if min_ret <= -SL_PCT: return -SL_PCT - 2*COST_PER_SIDE
    if max_ret >= TP_PCT: return TP_PCT - 2*COST_PER_SIDE
    if side == 'long': return (x-e)/e - 2*COST_PER_SIDE
    return (e-x)/e - 2*COST_PER_SIDE


def compute_rolling_top_k(df, k=TOP_K, lookback_days=LOOKBACK_DAYS):
    """LONG-only rolling TOP-K ranking by trailing 21d hypothetical LONG-only PnL.

    Per cycle 90: when we constrain to LONG-only, the ranking should be done by
    LONG-only PnL too (cycle 79 method, side-specific). This makes ranking
    consistent with execution.
    """
    sub = df.dropna(subset=['basis_z','entry_px','exit_px','max_hi','min_lo']).copy()
    trades = []
    last_entry = {t:None for t in TOKEN_SYMBOLS}
    for _, r in sub.iterrows():
        t = r['token']
        if last_entry.get(t) is not None and (r['bar_5m']-last_entry[t]).total_seconds() < HOLD_BARS*5*60:
            continue
        # LONG-only logic
        if r['basis_z'] > Z_THR:
            pnl = simulate(r, 'long')
            if pnl is not None:
                trades.append({'token':t,'bar':r['bar_5m'],'pnl':pnl})
                last_entry[t] = r['bar_5m']
    td = pd.DataFrame(trades)
    if len(td) == 0:
        # Warm-start fallback: cycle 83 dashboard TOP-5 LONG-only
        return ['BEAT', 'MYX', 'GUA', 'SKYAI', 'FF']  # known LONG-profitable
    cutoff = now_utc() - timedelta(days=lookback_days)
    recent = td[td['bar'] >= cutoff]
    if len(recent) < 10:
        return ['BEAT', 'MYX', 'GUA', 'SKYAI', 'FF']
    ranking = recent.groupby('token')['pnl'].sum().sort_values(ascending=False)
    return list(ranking.head(k).index)


def get_recent_emitted(conn, hours=SIGNAL_COOLDOWN_HOURS):
    """Tokens for which we've emitted a signal in last N hours."""
    cutoff = (now_utc() - timedelta(hours=hours)).isoformat()
    rows = conn.execute("SELECT DISTINCT token FROM signals_emitted WHERE ts >= ?", (cutoff,)).fetchall()
    return set(r[0] for r in rows)


def emit_order(inbox_dir, wallet, token_symbol, side, amount_bnb, entry_px, basis_z, conn):
    """Write order JSON atomically to inbox folder."""
    if token_symbol not in SYMBOL_TO_ADDR:
        log.error(f'no BSC address for {token_symbol}')
        return None
    addr = SYMBOL_TO_ADDR[token_symbol]
    bar_ts = now_utc().strftime('%Y%m%dT%H%M')
    coid = f'alpharadar_{bar_ts}_{token_symbol}_{uuid.uuid4().hex[:6]}'

    order = {
        'client_order_id': coid,
        'wallet': wallet,
        'side': 'buy',  # spot only buys
        'token': addr,
        'amount': round(amount_bnb, 8),
        'type': 'market',
        'take_profit_pct': TP_PCT,
        'stop_loss_pct': SL_PCT,
        'max_hold_seconds': HOLD_BARS * 5 * 60,  # auto-close after 4h (HOLD_BARS=48 × 5min)
        'tag': f'basis_z={basis_z:+.2f} sym={token_symbol}',
    }

    os.makedirs(inbox_dir, exist_ok=True)
    fname = os.path.join(inbox_dir, f'{coid}.json')
    tmp = fname + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(order, f, indent=2)
    os.replace(tmp, fname)  # atomic
    conn.execute(
        "INSERT OR REPLACE INTO signals_emitted VALUES (?,?,?,?,?,?,?)",
        (now_utc().isoformat(), token_symbol, side, float(basis_z), float(entry_px), coid, amount_bnb)
    )
    conn.commit()
    log.info(f'EMITTED order {coid}: BUY {token_symbol} ({addr}) amount={amount_bnb:.6f} BNB basis_z={basis_z:+.2f}')
    return coid


_ERC20_ABI = [
    {'name':'balanceOf','type':'function','stateMutability':'view',
     'inputs':[{'name':'','type':'address'}],'outputs':[{'name':'','type':'uint256'}]},
]

def get_wallet_snapshot(wallet_name):
    """Return (free_bnb, open_position_count) for the wallet via on-chain query."""
    from web3 import Web3
    addr = WALLET_ADDR[wallet_name]
    w3 = Web3(Web3.HTTPProvider(BSC_RPC))
    bnb = w3.eth.get_balance(Web3.to_checksum_address(addr)) / 1e18
    open_count = 0
    for sym, tok_addr in SYMBOL_TO_ADDR.items():
        try:
            c = w3.eth.contract(address=Web3.to_checksum_address(tok_addr), abi=_ERC20_ABI)
            bal = c.functions.balanceOf(Web3.to_checksum_address(addr)).call()
            if bal > 0 and (bal / 1e18) > DUST_THRESHOLD:
                open_count += 1
        except Exception as e:
            log.warning(f'  balance probe failed for {sym}: {e}')
    return bnb, open_count


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', default='/opt/alpha-radar/config.yaml')
    ap.add_argument('--inbox', default='/opt/alpha-radar/orders_inbox')
    ap.add_argument('--wallet', default='body1', help='wallet name from config')
    ap.add_argument('--amount-bnb', type=float, default=0.001, help='fallback BNB per trade if --auto-size off')
    ap.add_argument('--auto-size', action='store_true',
                    help='dynamic sizing: first trade 60%% of free BNB, 80%% of remainder for subsequent')
    ap.add_argument('--ch-host', default='localhost')
    ap.add_argument('--ch-pass', default='<CH_PASSWORD>')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    log.info(f'brain starting (wallet={args.wallet}, auto_size={args.auto_size}, fallback_amount={args.amount_bnb} BNB, dry_run={args.dry_run})')

    conn = init_brain_db()

    ch = clickhouse_connect.get_client(
        host=args.ch_host, port=8123, user='mcp_agent', password=args.ch_pass,
        settings={'max_threads':4,'max_memory_usage':4*1024**3,'max_execution_time':60}
    )

    kdf, bdf = pull_data(ch)
    if kdf.empty:
        log.error('no kline data — abort')
        return
    log.info(f'pulled {len(kdf)} klines, {len(bdf)} basis rows')

    df = build_features(kdf, bdf)
    latest_bar = df['bar_5m'].max()
    log.info(f'latest bar: {latest_bar}')

    # Latest snapshot per token
    latest = df.groupby('token').tail(1).set_index('token')

    # Rolling TOP-5 by trailing 21d LONG-only PnL
    top_k = compute_rolling_top_k(df)
    log.info(f'TOP-{TOP_K} (LONG-only ranking): {top_k}')

    # Recently emitted (cooldown)
    recent_emitted = get_recent_emitted(conn)
    if recent_emitted:
        log.info(f'cooldown active (last {SIGNAL_COOLDOWN_HOURS}h): {recent_emitted}')

    # Wallet snapshot for auto-sizing
    free_bnb = None
    open_count = 0
    if args.auto_size:
        bnb_total, open_count = get_wallet_snapshot(args.wallet)
        free_bnb = max(0.0, bnb_total - GAS_RESERVE_BNB)
        log.info(f'auto-size: wallet BNB={bnb_total:.6f} reserve={GAS_RESERVE_BNB} free={free_bnb:.6f} open_positions={open_count}')

    # Check signals (LONG-only since PancakeSwap spot)
    n_signals = 0
    n_emitted = 0
    for token in TOKEN_SYMBOLS:
        if token not in latest.index: continue
        row = latest.loc[token]
        bz = row.get('basis_z')
        if pd.isna(bz): continue
        if bz <= Z_THR: continue  # LONG-only — only positive z
        n_signals += 1
        if token not in top_k:
            log.info(f'  ⚠️ {token} basis_z={bz:+.2f} but NOT in TOP-{TOP_K} — SKIP')
            continue
        if token in recent_emitted:
            log.info(f'  ⚠️ {token} basis_z={bz:+.2f} but cooldown active — SKIP')
            continue

        # Sizing
        if args.auto_size:
            frac = SIZE_FRAC_FIRST if open_count == 0 else SIZE_FRAC_SUBSEQUENT
            amount_bnb = round(frac * free_bnb, 6)
            if amount_bnb < MIN_TRADE_BNB:
                log.warning(f'  ⚠️ {token} auto-size={amount_bnb:.6f} BNB < min {MIN_TRADE_BNB} (free={free_bnb:.6f} open={open_count}) — SKIP')
                continue
        else:
            amount_bnb = args.amount_bnb

        # === LIQUIDITY GUARD (after RAVE -98% disaster, cycle 112) ===
        if token in LIQUIDITY_BLACKLIST:
            log.warning(f'  ⚠️ {token} in LIQUIDITY_BLACKLIST (cycle 112) — SKIP')
            continue
        rt_loss, liq_ok = check_liquidity(SYMBOL_TO_ADDR[token], amount_bnb)
        if not liq_ok:
            log.warning(f'  ⚠️ {token} live PCS rt_loss={rt_loss}% > {MAX_ROUND_TRIP_LOSS_PCT}% — SKIP')
            continue
        log.info(f'  ✓ {token} liquidity OK rt_loss={rt_loss:.2f}%')

        # Emit order
        entry_px = float(row['close_px'])
        if args.dry_run:
            log.info(f'  [DRY-RUN] would emit BUY {token} basis_z={bz:+.2f} at px={entry_px:.6f} amount={amount_bnb:.6f} BNB')
        else:
            coid = emit_order(args.inbox, args.wallet, token, 'long', amount_bnb,
                              entry_px, float(bz), conn)
            if coid:
                n_emitted += 1
                if args.auto_size:
                    free_bnb -= amount_bnb
                    open_count += 1
                    log.info(f'  → after emit: free_bnb={free_bnb:.6f} open_count={open_count}')

    conn.execute(
        "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?)",
        (now_utc().isoformat(), n_signals, n_emitted, json.dumps(top_k), str(latest_bar))
    )
    conn.commit()
    log.info(f'DONE — signals_seen={n_signals} emitted={n_emitted}')


if __name__ == '__main__':
    main()
