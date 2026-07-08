#!/usr/bin/env python3
"""Check MT4 position status and floating P&L."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from mt4_client import Mt4WebSocketClient, Mt4ApiError

OP_NAMES = {0: 'BUY', 1: 'SELL', 2: 'BUYLIMIT', 3: 'SELLLIMIT', 4: 'BUYSTOP', 5: 'SELLSTOP'}

client = Mt4WebSocketClient()
try:
    client.connect()
    eh = client.expert_handle
    print(f"  ✅ Connected, handle: {eh}")

    # Account info
    acc = client.get_account_info(eh)
    bal = float(acc.get('Balance', 0))
    eq  = float(acc.get('Equity', 0))
    fl  = float(acc.get('Profit', 0))
    fm  = float(acc.get('FreeMargin', 0))
    print(f"\n  📊 Account")
    print(f"     Balance:    ${bal:,.2f}")
    print(f"     Equity:     ${eq:,.2f}")
    print(f"     Floating:   ${fl:,.2f}")
    print(f"     FreeMargin: ${fm:,.2f}")

    # Open orders
    orders = client.get_orders(eh)
    if not orders:
        print(f"\n  ℹ️  No open positions.")
    else:
        for o in orders:
            ticket  = o.get('Ticket', '?')
            symbol  = o.get('Symbol', '?')
            op      = o.get('Operation')
            op_name = OP_NAMES.get(op, f'OP_{op}')
            lots    = o.get('Lots', 0)
            price   = o.get('OpenPrice', 0)
            sl      = o.get('StopLoss', 0)
            tp      = o.get('TakeProfit', 0)
            profit  = float(o.get('Profit', 0))
            swap    = float(o.get('Swap', 0))
            comm    = float(o.get('Commission', 0))
            total   = profit + swap + comm
            magic   = o.get('MagicNumber', '?')
            comment = o.get('Comment', '')

            print(f"\n  📈 #{ticket} {symbol} {op_name} {lots} lot")
            print(f"     Open: {price}  SL: {sl}  TP: {tp}")
            print(f"     Profit: ${profit:.2f}  Swap: ${swap:.2f}  Comm: ${comm:.2f}")
            print(f"     Total: ${total:.2f}  Magic: {magic}")
            if comment:
                print(f"     Comment: {comment}")

    client.disconnect()
except Mt4ApiError as e:
    print(f"  ❌ Error: {e}")
except Exception as e:
    print(f"  ❌ Unexpected: {e}")
