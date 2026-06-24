"""swap_usdt_bnb.py — Direct USDT→BNB swap via PancakeSwap V2 router.

No external APIs. Just web3 + the verified router contract on BSC.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from web3 import Web3
from eth_account import Account


CHAIN_ID = 56
USDT_ADDR = '0x55d398326f99059fF775485246999027B3197955'
WBNB_ADDR = '0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c'
PANCAKE_V2_ROUTER = '0x10ED43C718714eb63d5aA57B78B54704E256024E'
RPC = 'https://bsc-dataseed.binance.org'

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

ROUTER_ABI = [
    {'name':'getAmountsOut','type':'function','stateMutability':'view',
     'inputs':[{'name':'amountIn','type':'uint256'},{'name':'path','type':'address[]'}],
     'outputs':[{'name':'amounts','type':'uint256[]'}]},
    {'name':'swapExactTokensForETH','type':'function','stateMutability':'nonpayable',
     'inputs':[{'name':'amountIn','type':'uint256'},{'name':'amountOutMin','type':'uint256'},
               {'name':'path','type':'address[]'},{'name':'to','type':'address'},
               {'name':'deadline','type':'uint256'}],
     'outputs':[{'name':'amounts','type':'uint256[]'}]},
]


def load_pk_from_keystore(ks_path, password):
    with open(ks_path) as f:
        keystore_json = f.read()
    return Account.decrypt(keystore_json, password).hex()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--amount-usdt', type=float, required=True)
    ap.add_argument('--keystore', default='/opt/alpha-radar/keystores/body1.json')
    ap.add_argument('--slippage', type=float, default=1.0, help='%')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    pw = os.environ.get('TWAK_KEYSTORE_PASSWORD')
    if not pw:
        print('ERROR: set TWAK_KEYSTORE_PASSWORD env'); sys.exit(1)

    pk = load_pk_from_keystore(args.keystore, pw)
    acct = Account.from_key(pk)
    w3 = Web3(Web3.HTTPProvider(RPC))
    print(f'wallet: {acct.address}')
    print(f'block: {w3.eth.block_number}')

    usdt = w3.eth.contract(address=Web3.to_checksum_address(USDT_ADDR), abi=ERC20_ABI)
    router = w3.eth.contract(address=Web3.to_checksum_address(PANCAKE_V2_ROUTER), abi=ROUTER_ABI)
    dec = usdt.functions.decimals().call()
    bal_raw = usdt.functions.balanceOf(acct.address).call()
    bal = bal_raw / 10**dec
    print(f'USDT balance: {bal:.6f}')
    amount_raw = int(args.amount_usdt * 10**dec)
    if amount_raw > bal_raw:
        print(f'ERROR: insufficient'); sys.exit(1)

    # Get quote
    path = [Web3.to_checksum_address(USDT_ADDR), Web3.to_checksum_address(WBNB_ADDR)]
    amounts = router.functions.getAmountsOut(amount_raw, path).call()
    out_raw = amounts[-1]
    out_bnb = out_raw / 1e18
    rate = args.amount_usdt / out_bnb
    print(f'quote: {args.amount_usdt} USDT → {out_bnb:.6f} BNB (rate 1 BNB = {rate:.2f} USDT)')

    if args.dry_run:
        print('DRY-RUN — exiting'); return

    # Approve if needed
    allow = usdt.functions.allowance(acct.address, PANCAKE_V2_ROUTER).call()
    if allow < amount_raw:
        print(f'approving USDT to router (current allow={allow})...')
        nonce = w3.eth.get_transaction_count(acct.address)
        approve_tx = usdt.functions.approve(PANCAKE_V2_ROUTER, amount_raw).build_transaction({
            'from': acct.address, 'nonce': nonce, 'gas': 80000, 'gasPrice': w3.eth.gas_price, 'chainId': CHAIN_ID,
        })
        signed = acct.sign_transaction(approve_tx)
        txh = w3.eth.send_raw_transaction(signed.raw_transaction)
        print(f'  approve tx: 0x{txh.hex()}')
        r = w3.eth.wait_for_transaction_receipt(txh, timeout=180)
        if r.status != 1:
            print('  APPROVE FAILED'); sys.exit(1)
        print(f'  approved (gas {r.gasUsed})')

    # Swap
    min_out_raw = int(out_raw * (1 - args.slippage/100))
    deadline = int(time.time()) + 600
    print(f'swap: in={amount_raw} min_out={min_out_raw} deadline={deadline}')
    nonce = w3.eth.get_transaction_count(acct.address)
    swap_tx = router.functions.swapExactTokensForETH(
        amount_raw, min_out_raw, path, acct.address, deadline
    ).build_transaction({
        'from': acct.address, 'nonce': nonce, 'gas': 300000,
        'gasPrice': w3.eth.gas_price, 'chainId': CHAIN_ID,
    })
    signed = acct.sign_transaction(swap_tx)
    txh = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f'  swap tx: 0x{txh.hex()}')
    print(f'  https://bscscan.com/tx/0x{txh.hex()}')
    print('waiting...')
    r = w3.eth.wait_for_transaction_receipt(txh, timeout=300)
    if r.status != 1:
        print(f'  SWAP FAILED (gas used {r.gasUsed}/300000)'); sys.exit(1)
    print(f'  CONFIRMED (block {r.blockNumber}, gas {r.gasUsed})')

    # Final
    bal_after_usdt = usdt.functions.balanceOf(acct.address).call() / 10**dec
    bal_after_bnb = w3.eth.get_balance(acct.address) / 1e18
    print(f'final USDT: {bal_after_usdt:.6f}')
    print(f'final BNB:  {bal_after_bnb:.6f}')


if __name__ == '__main__':
    main()
