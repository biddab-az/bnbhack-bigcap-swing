"""Sell any ERC20 token → BNB on PancakeSwap V2."""
from __future__ import annotations
import argparse, os, sys, time
from web3 import Web3
from eth_account import Account

CHAIN_ID = 56
WBNB = '0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c'
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
    {'name':'swapExactTokensForETHSupportingFeeOnTransferTokens','type':'function','stateMutability':'nonpayable',
     'inputs':[{'name':'amountIn','type':'uint256'},{'name':'amountOutMin','type':'uint256'},
               {'name':'path','type':'address[]'},{'name':'to','type':'address'},
               {'name':'deadline','type':'uint256'}],'outputs':[]},
]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--token', required=True, help='token address')
    ap.add_argument('--keystore', default='/opt/alpha-radar/keystores/body1.json')
    ap.add_argument('--slippage', type=float, default=3.0, help='%% slippage (3 default for low-liq tokens)')
    ap.add_argument('--all', action='store_true', help='sell entire balance')
    ap.add_argument('--amount', type=float, default=0, help='token units to sell (ignored if --all)')
    args = ap.parse_args()

    pw = os.environ['TWAK_KEYSTORE_PASSWORD']
    with open(args.keystore) as f: ks = f.read()
    pk = Account.decrypt(ks, pw).hex()
    acct = Account.from_key(pk)
    w3 = Web3(Web3.HTTPProvider(RPC))
    print(f'wallet: {acct.address} block: {w3.eth.block_number}')

    tok = w3.eth.contract(address=Web3.to_checksum_address(args.token), abi=ERC20_ABI)
    router = w3.eth.contract(address=Web3.to_checksum_address(PANCAKE_V2_ROUTER), abi=ROUTER_ABI)
    dec = tok.functions.decimals().call()
    bal_raw = tok.functions.balanceOf(acct.address).call()
    bal = bal_raw / 10**dec
    print(f'token balance: {bal:.6f} ({dec} decimals)')
    if args.all:
        amount_raw = bal_raw
    else:
        amount_raw = int(args.amount * 10**dec)
    if amount_raw == 0 or amount_raw > bal_raw:
        print(f'invalid amount {amount_raw} vs bal {bal_raw}'); sys.exit(1)
    print(f'selling: {amount_raw/10**dec:.6f}')

    path = [Web3.to_checksum_address(args.token), Web3.to_checksum_address(WBNB)]
    quote = router.functions.getAmountsOut(amount_raw, path).call()
    out_bnb = quote[-1] / 1e18
    print(f'quote: {out_bnb:.6f} BNB')

    allow = tok.functions.allowance(acct.address, PANCAKE_V2_ROUTER).call()
    if allow < amount_raw:
        print('approving...')
        nonce = w3.eth.get_transaction_count(acct.address)
        tx = tok.functions.approve(PANCAKE_V2_ROUTER, amount_raw).build_transaction({
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
    print(f'swap: in={amount_raw} min_out={min_out}')
    nonce = w3.eth.get_transaction_count(acct.address)
    swap = router.functions.swapExactTokensForETHSupportingFeeOnTransferTokens(
        amount_raw, min_out, path, acct.address, deadline
    ).build_transaction({
        'from': acct.address, 'nonce': nonce, 'gas': 350000,
        'gasPrice': w3.eth.gas_price, 'chainId': CHAIN_ID,
    })
    signed = acct.sign_transaction(swap)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f'  swap tx: 0x{h.hex()}')
    print(f'  https://bscscan.com/tx/0x{h.hex()}')
    r = w3.eth.wait_for_transaction_receipt(h, timeout=300)
    if r.status != 1: print(f'SWAP FAILED gas={r.gasUsed}/350000'); sys.exit(1)
    print(f'  CONFIRMED block={r.blockNumber} gas={r.gasUsed}')

    bal_after = tok.functions.balanceOf(acct.address).call() / 10**dec
    bnb_after = w3.eth.get_balance(acct.address) / 1e18
    print(f'final token: {bal_after:.6f}  BNB: {bnb_after:.6f}')

if __name__ == '__main__':
    main()
