"""odos_swap.py — One-off USDT→BNB swap via ODOS API.

Usage:
  TWAK_KEYSTORE_PASSWORD=<TWAK_KEYSTORE_PASSWORD> python odos_swap.py --amount-usdt 350
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import requests
from web3 import Web3
from eth_account import Account


CHAIN_ID = 56  # BSC
USDT_ADDR = '0x55d398326f99059fF775485246999027B3197955'  # USDT BSC (18 decimals)
BNB_PROXY = '0x0000000000000000000000000000000000000000'  # ODOS uses 0x0 for native
RPC = 'https://bsc-dataseed.binance.org'

ODOS_QUOTE = 'https://api.odos.xyz/sor/quote/v2'
ODOS_ASSEMBLE = 'https://api.odos.xyz/sor/assemble'

ERC20_ABI = [
    {'name':'balanceOf','type':'function','stateMutability':'view',
     'inputs':[{'name':'','type':'address'}],'outputs':[{'name':'','type':'uint256'}]},
    {'name':'decimals','type':'function','stateMutability':'view',
     'inputs':[],'outputs':[{'name':'','type':'uint8'}]},
    {'name':'allowance','type':'function','stateMutability':'view',
     'inputs':[{'name':'','type':'address'},{'name':'','type':'address'}],
     'outputs':[{'name':'','type':'uint256'}]},
    {'name':'approve','type':'function','stateMutability':'nonpayable',
     'inputs':[{'name':'','type':'address'},{'name':'','type':'uint256'}],
     'outputs':[{'name':'','type':'bool'}]},
]


def load_pk_from_keystore(ks_path, password):
    with open(ks_path) as f:
        keystore_json = f.read()
    return Account.decrypt(keystore_json, password).hex()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--amount-usdt', type=float, required=True, help='Amount of USDT to swap (whole units)')
    ap.add_argument('--keystore', default='/opt/alpha-radar/keystores/body1.json')
    ap.add_argument('--slippage', type=float, default=0.5, help='Slippage % (default 0.5%)')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    pw = os.environ.get('TWAK_KEYSTORE_PASSWORD')
    if not pw:
        print('ERROR: set TWAK_KEYSTORE_PASSWORD env'); sys.exit(1)

    pk = load_pk_from_keystore(args.keystore, pw)
    acct = Account.from_key(pk)
    print(f'wallet: {acct.address}')

    w3 = Web3(Web3.HTTPProvider(RPC))
    print(f'block: {w3.eth.block_number}')

    # Check USDT balance
    usdt = w3.eth.contract(address=Web3.to_checksum_address(USDT_ADDR), abi=ERC20_ABI)
    dec = usdt.functions.decimals().call()
    bal_raw = usdt.functions.balanceOf(acct.address).call()
    bal = bal_raw / 10**dec
    print(f'USDT balance: {bal:.6f}')
    amount_raw = int(args.amount_usdt * 10**dec)
    if amount_raw > bal_raw:
        print(f'ERROR: insufficient USDT ({bal} < {args.amount_usdt})'); sys.exit(1)

    # Get ODOS quote
    print(f'requesting ODOS quote: {args.amount_usdt} USDT → BNB ...')
    quote_req = {
        'chainId': CHAIN_ID,
        'inputTokens': [{'tokenAddress': USDT_ADDR, 'amount': str(amount_raw)}],
        'outputTokens': [{'tokenAddress': BNB_PROXY, 'proportion': 1}],
        'userAddr': acct.address,
        'slippageLimitPercent': args.slippage,
        'sourceBlacklist': [],
        'pathViz': False,
    }
    r = requests.post(ODOS_QUOTE, json=quote_req, timeout=30)
    if r.status_code != 200:
        print(f'ODOS quote ERROR {r.status_code}: {r.text}'); sys.exit(1)
    quote = r.json()
    path_id = quote['pathId']
    out_amount_raw = int(quote['outAmounts'][0])
    out_amount = out_amount_raw / 1e18
    print(f'  quote: pathId={path_id}')
    print(f'  output: {out_amount:.6f} BNB')
    print(f'  effective rate: 1 BNB = {args.amount_usdt/out_amount:.2f} USDT')
    print(f'  price impact: {quote.get("priceImpact", "n/a")}%')

    if args.dry_run:
        print('DRY-RUN — skipping execute')
        return

    # Assemble tx
    print('assembling tx...')
    assemble_req = {'userAddr': acct.address, 'pathId': path_id, 'simulate': False}
    r = requests.post(ODOS_ASSEMBLE, json=assemble_req, timeout=30)
    if r.status_code != 200:
        print(f'ODOS assemble ERROR {r.status_code}: {r.text}'); sys.exit(1)
    asm = r.json()
    tx_data = asm['transaction']
    router = Web3.to_checksum_address(tx_data['to'])
    print(f'  router: {router}')
    print(f'  value: {int(tx_data["value"])/1e18:.6f} BNB')
    print(f'  gas: {tx_data["gas"]}')

    # Approve USDT to ODOS router if needed
    allow = usdt.functions.allowance(acct.address, router).call()
    if allow < amount_raw:
        print(f'approving USDT to router (current allowance {allow})...')
        nonce = w3.eth.get_transaction_count(acct.address)
        approve_tx = usdt.functions.approve(router, amount_raw).build_transaction({
            'from': acct.address,
            'nonce': nonce,
            'gas': 80000,
            'gasPrice': w3.eth.gas_price,
        })
        signed = acct.sign_transaction(approve_tx)
        txh = w3.eth.send_raw_transaction(signed.raw_transaction)
        print(f'  approve tx: {txh.hex()}')
        receipt = w3.eth.wait_for_transaction_receipt(txh, timeout=180)
        if receipt.status != 1:
            print('  APPROVE FAILED'); sys.exit(1)
        print(f'  approved (gas used {receipt.gasUsed})')
    else:
        print(f'allowance ok ({allow})')

    # Build & send swap tx
    print('sending swap tx...')
    nonce = w3.eth.get_transaction_count(acct.address)
    swap_tx = {
        'from': acct.address,
        'to': router,
        'data': tx_data['data'],
        'value': int(tx_data['value']),
        'nonce': nonce,
        'gas': int(int(tx_data['gas']) * 1.2),  # 20% headroom
        'gasPrice': w3.eth.gas_price,
        'chainId': CHAIN_ID,
    }
    signed = acct.sign_transaction(swap_tx)
    txh = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f'  swap tx: {txh.hex()}')
    print(f'  https://bscscan.com/tx/{txh.hex()}')
    print('waiting for confirmation...')
    receipt = w3.eth.wait_for_transaction_receipt(txh, timeout=300)
    if receipt.status != 1:
        print('  SWAP FAILED'); sys.exit(1)
    print(f'  CONFIRMED (block {receipt.blockNumber}, gas {receipt.gasUsed})')

    # Final balance
    bal_after_usdt = usdt.functions.balanceOf(acct.address).call() / 10**dec
    bal_after_bnb = w3.eth.get_balance(acct.address) / 1e18
    print(f'final USDT: {bal_after_usdt:.6f}')
    print(f'final BNB:  {bal_after_bnb:.6f}')
    print(f'received BNB: {bal_after_bnb - 0.003134:.6f}')


if __name__ == '__main__':
    main()
