"""swap_bnb_usdt.py — Swap N BNB → USDT via PancakeSwap V2 (swapExactETHForTokens)."""
from __future__ import annotations
import argparse, os, sys, time
from web3 import Web3
from eth_account import Account

CHAIN_ID = 56
USDT = '0x55d398326f99059fF775485246999027B3197955'
WBNB = '0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c'
ROUTER = '0x10ED43C718714eb63d5aA57B78B54704E256024E'
RPC = 'https://bsc-dataseed.binance.org'

ROUTER_ABI = [
    {'name':'getAmountsOut','type':'function','stateMutability':'view',
     'inputs':[{'name':'amountIn','type':'uint256'},{'name':'path','type':'address[]'}],
     'outputs':[{'name':'amounts','type':'uint256[]'}]},
    {'name':'swapExactETHForTokens','type':'function','stateMutability':'payable',
     'inputs':[{'name':'amountOutMin','type':'uint256'},{'name':'path','type':'address[]'},
               {'name':'to','type':'address'},{'name':'deadline','type':'uint256'}],
     'outputs':[{'name':'amounts','type':'uint256[]'}]},
]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--keystore', required=True)
    ap.add_argument('--bnb-amount', type=float, required=True, help='BNB to swap')
    ap.add_argument('--slippage', type=float, default=1.0)
    args = ap.parse_args()

    pw = os.environ['TWAK_KEYSTORE_PASSWORD']
    with open(args.keystore) as f: ks = f.read()
    pk = Account.decrypt(ks, pw).hex()
    acct = Account.from_key(pk)
    w3 = Web3(Web3.HTTPProvider(RPC))
    print(f'wallet: {acct.address}')

    bnb_bal = w3.eth.get_balance(acct.address) / 1e18
    print(f'BNB balance: {bnb_bal:.6f}')
    if args.bnb_amount + 0.001 > bnb_bal:
        print(f'ERROR insufficient BNB'); sys.exit(1)

    amount_wei = int(args.bnb_amount * 10**18)
    router = w3.eth.contract(address=Web3.to_checksum_address(ROUTER), abi=ROUTER_ABI)
    path = [Web3.to_checksum_address(WBNB), Web3.to_checksum_address(USDT)]
    quote = router.functions.getAmountsOut(amount_wei, path).call()
    out_usdt = quote[-1] / 1e18
    print(f'quote: {args.bnb_amount} BNB → {out_usdt:.4f} USDT')

    min_out = int(quote[-1] * (1 - args.slippage/100))
    deadline = int(time.time()) + 600
    nonce = w3.eth.get_transaction_count(acct.address)
    tx = router.functions.swapExactETHForTokens(
        min_out, path, acct.address, deadline
    ).build_transaction({
        'from': acct.address, 'value': amount_wei,
        'nonce': nonce, 'gas': 200000,
        'gasPrice': w3.eth.gas_price, 'chainId': CHAIN_ID,
    })
    signed = acct.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f'tx: 0x{h.hex()}')
    print(f'https://bscscan.com/tx/0x{h.hex()}')
    r = w3.eth.wait_for_transaction_receipt(h, timeout=300)
    if r.status != 1: print(f'FAILED gas={r.gasUsed}'); sys.exit(1)
    print(f'CONFIRMED block={r.blockNumber} gas={r.gasUsed}')

if __name__ == '__main__':
    main()
