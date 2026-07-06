#!/usr/bin/env python3
# test_mt4_xauusd.py — Test MT4 connection with XAUUSD data
# -*- coding: utf-8 -*-

import json
import time
import threading
from websocket import create_connection, WebSocketConnectionClosedException

# Message types
MSG_COMMAND = 0
MSG_RESPONSE = 1
MSG_SERVICE_REQUEST = 6
SERVICE_REQUEST_EXPERT_LIST = 0

# Command types
CMD_ACCOUNT_BALANCE = 40
CMD_ACCOUNT_EQUITY = 44
CMD_ACCOUNT_CURRENCY = 43
CMD_ACCOUNT_LEVERAGE = 48
CMD_ACCOUNT_NAME = 50
CMD_ACCOUNT_NUMBER = 51
CMD_ACCOUNT_SERVER = 53
CMD_ACCOUNT_PROFIT = 52
CMD_ACCOUNT_FREE_MARGIN = 45
CMD_COPY_RATES = 284
CMD_GET_QUOTE = 290
CMD_GET_SYMBOLS = 291


class Mt4WS:
    def __init__(self, host='127.0.0.1', port=8222):
        self.host = host
        self.port = port
        self.ws = None
        self._next_id = 1
        self._pending = {}
        self._lock = threading.Lock()
        self._connected = False
        self._experts = []
        self._recv_thread = None

    def connect(self, timeout=10):
        url = f"ws://{self.host}:{self.port}/ws"
        try:
            self.ws = create_connection(url, timeout=timeout)
            self._connected = True
            self._recv_thread = threading.Thread(target=self._receiver, daemon=True)
            self._recv_thread.start()
            self._request_expert_list()
            time.sleep(0.5)
            return True
        except Exception as e:
            print(f"  Connection failed: {e}")
            return False

    def disconnect(self):
        self._connected = False
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None

    def _receiver(self):
        while self._connected and self.ws:
            try:
                raw = self.ws.recv()
                if raw:
                    self._handle_message(raw)
            except WebSocketConnectionClosedException:
                break
            except Exception:
                break

    def _handle_message(self, raw):
        try:
            parts = raw.split(';', 1)
            if len(parts) < 2:
                return
            msg_type = int(parts[0])
            payload = parts[1]
            if msg_type == 1:  # MSG_RESPONSE
                sub = payload.split(';', 2)
                if len(sub) >= 3:
                    cmd_id = int(sub[1])
                    with self._lock:
                        if cmd_id in self._pending:
                            evt, _ = self._pending[cmd_id]
                            self._pending[cmd_id] = (evt, sub[2])
                            evt.set()
            elif msg_type == 3:  # MSG_EXPERT_LIST
                self._experts = [int(h) for h in payload.split(',') if h]
        except Exception:
            pass

    def _request_expert_list(self):
        if self.ws:
            self.ws.send(f"{MSG_SERVICE_REQUEST};{SERVICE_REQUEST_EXPERT_LIST}")

    def send_cmd(self, cmd_type, params=None, expert_handle=0, timeout=10):
        if not self._connected or not self.ws:
            raise ConnectionError("Not connected")
        cmd_id = self._next_id
        self._next_id += 1
        payload = json.dumps(params) if params else ""
        evt = threading.Event()
        with self._lock:
            self._pending[cmd_id] = (evt, None)
        msg = f"{MSG_COMMAND};{expert_handle};{cmd_id};{cmd_type};{payload}"
        self.ws.send(msg)
        if evt.wait(timeout):
            with self._lock:
                _, resp = self._pending.pop(cmd_id, (None, None))
            return resp
        else:
            with self._lock:
                self._pending.pop(cmd_id, None)
            raise TimeoutError(f"Command {cmd_type} timed out")

    def get_account(self, eh=0):
        info = {}
        for name, cmd in [
            ('Name', CMD_ACCOUNT_NAME), ('Number', CMD_ACCOUNT_NUMBER),
            ('Balance', CMD_ACCOUNT_BALANCE), ('Equity', CMD_ACCOUNT_EQUITY),
            ('Profit', CMD_ACCOUNT_PROFIT), ('FreeMargin', CMD_ACCOUNT_FREE_MARGIN),
            ('Currency', CMD_ACCOUNT_CURRENCY), ('Leverage', CMD_ACCOUNT_LEVERAGE),
            ('Server', CMD_ACCOUNT_SERVER)
        ]:
            try:
                raw = self.send_cmd(cmd, expert_handle=eh)
                d = json.loads(raw) if raw else {}
                info[name] = d.get('Value', '?')
            except:
                info[name] = '?'
        return info

    def copy_rates(self, symbol, tf, start, count, eh=0):
        raw = self.send_cmd(CMD_COPY_RATES, {"Symbol": symbol, "TimeFrame": tf, "StartPos": start, "Count": count}, eh)
        return json.loads(raw) if raw else None

    def get_quote(self, symbol, eh=0):
        raw = self.send_cmd(CMD_GET_QUOTE, {"Symbol": symbol}, eh)
        return json.loads(raw) if raw else None

    def get_symbols(self, eh=0):
        raw = self.send_cmd(CMD_GET_SYMBOLS, expert_handle=eh)
        return json.loads(raw) if raw else None


def main():
    print("=" * 64)
    print("  MT4 Connection Test — XAUUSD")
    print("=" * 64)

    client = Mt4WS()
    if not client.connect():
        print("\n  ❌ Gagal connect. Pastikan EA sudah di-attach ke chart.")
        return

    experts = client._experts
    print(f"\n  ✅ Connected! Expert handles: {experts}")

    if not experts:
        print("  ❌ No expert registered.")
        client.disconnect()
        return

    eh = experts[0]
    print(f"  Using handle: {eh}\n")

    # Account
    acc = client.get_account(eh)
    print(f"  📊 Account")
    print(f"     Name:     {acc.get('Name')}")
    print(f"     Number:   {acc.get('Number')}")
    print(f"     Balance:  ${float(acc.get('Balance',0)):,.2f}")
    print(f"     Equity:   ${float(acc.get('Equity',0)):,.2f}")
    print(f"     Profit:   ${float(acc.get('Profit',0)):,.2f}")
    print(f"     Currency: {acc.get('Currency')}")
    print(f"     Leverage: 1:{acc.get('Leverage')}")
    print(f"     Server:   {acc.get('Server')}")

    # XAUUSD H1 candles
    print(f"\n  📈 XAUUSD H1 (10 candles)...")
    try:
        rates = client.copy_rates("XAUUSD", 60, 1, 10, eh)
        if rates and rates.get('Rates'):
            rl = rates['Rates']
            print(f"     Got {len(rl)} candles  (ErrorCode: {rates.get('ErrorCode')})")
            for i, r in enumerate(rl):
                print(f"     [{i}] {r.get('MtTime','?'):>19s}  "
                      f"O:{float(r.get('Open',0)):.2f}  H:{float(r.get('High',0)):.2f}  "
                      f"L:{float(r.get('Low',0)):.2f}  C:{float(r.get('Close',0)):.2f}  "
                      f"V:{r.get('TickVolume',0)}")
        else:
            print(f"     No data. ErrorCode: {rates.get('ErrorCode') if rates else 'N/A'}")
    except Exception as e:
        print(f"     Error: {e}")

    # XAUUSD Quote
    print(f"\n  💰 XAUUSD Quote...")
    try:
        q = client.get_quote("XAUUSD", eh)
        if q:
            print(f"     Bid: {q.get('Bid')}   Ask: {q.get('Ask')}   Time: {q.get('Time')}")
        else:
            print("     No quote data")
    except Exception as e:
        print(f"     Error: {e}")

    # List all symbols
    print(f"\n  🔍 Getting available symbols...")
    try:
        syms = client.get_symbols(eh)
        if syms:
            symbols_list = syms.get('Symbols', [])
            print(f"     Total: {len(symbols_list)} symbols")
            # Show first 20
            for s in symbols_list[:20]:
                print(f"     - {s}")
            if len(symbols_list) > 20:
                print(f"     ... and {len(symbols_list)-20} more")
    except Exception as e:
        print(f"     Error: {e}")

    client.disconnect()
    print(f"\n  ✅ Done.")
    print("=" * 64)


if __name__ == '__main__':
    main()
