"""swap_usdt_token.py — USDT → ANY token via PancakeSwap V2 (route USDT→WBNB→token)."""
from __future__ import annotations
import argparse, os, sys, time
from web3 import Web3
from eth_account import Account

CHAIN_ID = 56
USDT = '0x55d398326f99059fF775485246999027B3197955'
WBNB = '0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c'
ROUTER = '0x10ED43C718714eb63d5aA57B78B54704E256024E'
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
    {'name':'swapExactTokensForTokensSupportingFeeOnTransferTokens','type':'function','stateMutability':'nonpayable',
     'inputs':[{'name':'amountIn','type':'uint256'},{'name':'amountOutMin','type':'uint256'},
               {'name':'path','type':'address[]'},{'name':'to','type':'address'},
               {'name':'deadline','type':'uint256'}],'outputs':[]},
]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--keystore', required=True)
    ap.add_argument('--token', required=True, help='target token address')
    ap.add_argument('--amount-usdt', type=float, required=True)
    ap.add_argument('--slippage', type=float, default=3.0)
    args = ap.parse_args()

    pw = os.environ['TWAK_KEYSTORE_PASSWORD']
    with open(args.keystore) as f: ks = f.read()
    pk = Account.decrypt(ks, pw).hex()
    acct = Account.from_key(pk)
    w3 = Web3(Web3.HTTPProvider(RPC))
    print(f'wallet: {acct.address}')

    usdt = w3.eth.contract(address=Web3.to_checksum_address(USDT), abi=ERC20_ABI)
    usdt_bal = usdt.functions.balanceOf(acct.address).call() / 1e18
    print(f'USDT balance: {usdt_bal:.6f}')
    amount_wei = int(args.amount_usdt * 10**18)
    if amount_wei > usdt.functions.balanceOf(acct.address).call():
        print(f'ERROR insufficient'); sys.exit(1)

    router = w3.eth.contract(address=Web3.to_checksum_address(ROUTER), abi=ROUTER_ABI)
    path = [Web3.to_checksum_address(USDT), Web3.to_checksum_address(WBNB),
            Web3.to_checksum_address(args.token)]
    quote = router.functions.getAmountsOut(amount_wei, path).call()
    # need target token decimals for human display
    tok = w3.eth.contract(address=Web3.to_checksum_address(args.token), abi=ERC20_ABI)
    dec = tok.functions.decimals().call()
    out_h = quote[-1] / 10**dec
    print(f'quote: {args.amount_usdt} USDT → {out_h:.6f} tokens (decimals={dec})')

    allow = usdt.functions.allowance(acct.address, ROUTER).call()
    if allow < amount_wei:
        print('approving USDT to router...')
        nonce = w3.eth.get_transaction_count(acct.address)
        tx = usdt.functions.approve(ROUTER, amount_wei).build_transaction({
            'from': acct.address, 'nonce': nonce, 'gas': 80000,
            'gasPrice': w3.eth.gas_price, 'chainId': CHAIN_ID,
        })
        signed = acct.sign_transaction(tx)
        h = w3.eth.send_raw_transaction(signed.raw_transaction)
        r = w3.eth.wait_for_transaction_receipt(h, timeout=180)
        if r.status != 1: print('APPROVE FAILED'); sys.exit(1)
        print(f'  approved gas={r.gasUsed}')

    min_out = int(quote[-1] * (1 - args.slippage/100))
    deadline = int(time.time()) + 600
    print(f'swap: in={amount_wei} min_out={min_out}')
    nonce = w3.eth.get_transaction_count(acct.address)
    tx = router.functions.swapExactTokensForTokensSupportingFeeOnTransferTokens(
        amount_wei, min_out, path, acct.address, deadline
    ).build_transaction({
        'from': acct.address, 'nonce': nonce, 'gas': 400000,
        'gasPrice': w3.eth.gas_price, 'chainId': CHAIN_ID,
    })
    signed = acct.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f'tx: 0x{h.hex()}')
    print(f'https://bscscan.com/tx/0x{h.hex()}')
    r = w3.eth.wait_for_transaction_receipt(h, timeout=300)
    if r.status != 1: print(f'FAILED gas={r.gasUsed}'); sys.exit(1)
    print(f'CONFIRMED block={r.blockNumber} gas={r.gasUsed}')
    final_tok = tok.functions.balanceOf(acct.address).call() / 10**dec
    print(f'final token balance: {final_tok:.6f}')

if __name__ == '__main__':
    main()
