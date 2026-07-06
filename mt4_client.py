#!/usr/bin/env python3
# mt4_client.py — Pure Python WebSocket client for MtApi EA on MT4
# -*- coding: utf-8 -*-

import json
import time
import threading
from websocket import create_connection, WebSocketConnectionClosedException

# Protocol constants
MSG_COMMAND = 0
MSG_RESPONSE = 1
MSG_EVENT = 2
MSG_EXPERT_LIST = 3
MSG_EXPERT_ADDED = 4
MSG_EXPERT_REMOVED = 5
MSG_SERVICE_REQUEST = 6
SERVICE_REQUEST_EXPERT_LIST = 0

# Command types (from MtCommandType.cs)
CMD_ACCOUNT_BALANCE = 40
CMD_ACCOUNT_CREDIT = 41
CMD_ACCOUNT_COMPANY = 42
CMD_ACCOUNT_CURRENCY = 43
CMD_ACCOUNT_EQUITY = 44
CMD_ACCOUNT_FREE_MARGIN = 45
CMD_ACCOUNT_LEVERAGE = 48
CMD_ACCOUNT_MARGIN = 49
CMD_ACCOUNT_NAME = 50
CMD_ACCOUNT_NUMBER = 51
CMD_ACCOUNT_PROFIT = 52
CMD_ACCOUNT_SERVER = 53
CMD_COPY_RATES = 284
CMD_GET_QUOTE = 290
CMD_GET_SYMBOLS = 291
CMD_ORDER_SEND = 1
CMD_ORDER_CLOSE = 2
CMD_ORDER_MODIFY = 12
CMD_GET_ORDERS = 283
CMD_GET_ORDER = 282
CMD_ORDERS_TOTAL = 20
CMD_IS_CONNECTED = 27
CMD_IS_TRADE_ALLOWED = 35
CMD_MARKET_INFO = 59
CMD_SYMBOL_INFO_INTEGER = 203
CMD_SYMBOL_INFO_DOUBLE = 289
CMD_SYMBOL_INFO_STRING = 154
CMD_SYMBOL_INFO_TICK = 288


class Mt4ApiError(Exception):
    pass


class Mt4WebSocketClient:
    """
    Pure Python WebSocket client for MtApi EA on MetaTrader 4.
    Connects directly to the EA's WebSocket server without needing .NET DLLs.
    """

    def __init__(self, host='127.0.0.1', port=8222):
        self.host = host
        self.port = port
        self.ws = None
        self._next_id = 1
        self._pending = {}
        self._lock = threading.Lock()
        self._connected = False
        self._expert_handle = 0
        self._recv_thread = None

    # ───────────────────────── Connection ─────────────────────────

    def connect(self, timeout=10):
        """Connect to MT4 EA via WebSocket."""
        url = f"ws://{self.host}:{self.port}/ws"
        try:
            self.ws = create_connection(url, timeout=timeout)
            self._connected = True
            self._recv_thread = threading.Thread(target=self._receiver, daemon=True)
            self._recv_thread.start()
            # Wait a bit for expert list
            self._request_expert_list()
            time.sleep(0.5)
            return True
        except Exception as e:
            raise Mt4ApiError(f"Connection to {url} failed: {e}")

    def disconnect(self):
        self._connected = False
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None

    @property
    def is_connected(self):
        return self._connected

    @property
    def expert_handle(self):
        return self._expert_handle

    # ───────────────────────── Internal ─────────────────────────

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

            if msg_type == MSG_RESPONSE:
                # Format: 1;{ExpertHandle};{CommandId};{Payload}
                sub = payload.split(';', 2)
                if len(sub) >= 3:
                    cmd_id = int(sub[1])
                    with self._lock:
                        if cmd_id in self._pending:
                            evt, _ = self._pending[cmd_id]
                            self._pending[cmd_id] = (evt, sub[2])
                            evt.set()
            elif msg_type == MSG_EXPERT_LIST:
                self._experts = [int(h) for h in payload.split(',') if h]
                if self._experts:
                    self._expert_handle = self._experts[0]
            elif msg_type == MSG_EXPERT_ADDED:
                h = int(payload)
                if not self._expert_handle:
                    self._expert_handle = h
            elif msg_type == MSG_EXPERT_REMOVED:
                pass  # would remove from list
        except Exception:
            pass

    def _request_expert_list(self):
        if self.ws:
            self.ws.send(f"{MSG_SERVICE_REQUEST};{SERVICE_REQUEST_EXPERT_LIST}")

    def _send_command(self, command_type, params=None, expert_handle=None, timeout=10):
        """Low-level: send command, wait for response, return raw payload string."""
        if not self._connected or not self.ws:
            raise Mt4ApiError("Not connected to MT4")

        eh = expert_handle if expert_handle is not None else self._expert_handle
        cmd_id = self._next_id
        self._next_id += 1
        payload = json.dumps(params) if params else ""

        evt = threading.Event()
        with self._lock:
            self._pending[cmd_id] = (evt, None)

        msg = f"{MSG_COMMAND};{eh};{cmd_id};{command_type};{payload}"
        self.ws.send(msg)

        if evt.wait(timeout):
            with self._lock:
                _, resp = self._pending.pop(cmd_id, (None, None))
            return resp
        else:
            with self._lock:
                self._pending.pop(cmd_id, None)
            raise Mt4ApiError(f"Command {command_type} timed out after {timeout}s")

    def _send_and_parse(self, command_type, params=None, expert_handle=None, timeout=10):
        """Send command and parse JSON response. Returns parsed dict."""
        raw = self._send_command(command_type, params, expert_handle, timeout)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"Raw": raw}

    def _get_value(self, data):
        """Extract 'Value' from response dict, handling error codes."""
        if data is None:
            return None
        if isinstance(data, dict):
            ec = data.get('ErrorCode')
            if ec is not None and str(ec) != '0':
                err = data.get('ErrorMessage', '')
                raise Mt4ApiError(f"Error {ec}: {err}")
            return data.get('Value')
        return data

    # ──────────────────────── Account Info ────────────────────────

    def get_account_info(self, expert_handle=None):
        """Get account information as a dict."""
        fields = {
            'Name': CMD_ACCOUNT_NAME,
            'Number': CMD_ACCOUNT_NUMBER,
            'Balance': CMD_ACCOUNT_BALANCE,
            'Equity': CMD_ACCOUNT_EQUITY,
            'Profit': CMD_ACCOUNT_PROFIT,
            'FreeMargin': CMD_ACCOUNT_FREE_MARGIN,
            'Currency': CMD_ACCOUNT_CURRENCY,
            'Leverage': CMD_ACCOUNT_LEVERAGE,
            'Server': CMD_ACCOUNT_SERVER,
            'Company': CMD_ACCOUNT_COMPANY,
        }
        info = {}
        for name, cmd in fields.items():
            try:
                raw = self._send_command(cmd, expert_handle=expert_handle)
                if raw:
                    d = json.loads(raw)
                    info[name] = d.get('Value')
            except Exception:
                info[name] = None
        return info

    # ─────────────────────── Market Data ────────────────────────

    def copy_rates(self, symbol, timeframe, start_pos=1, count=10, expert_handle=None):
        """
        Get historical OHLCV candle data.

        Args:
            symbol: Symbol name (e.g. 'XAUUSD', 'EURUSD')
            timeframe: Minutes (1=M1, 5=M5, 15=M15, 30=M30, 60=H1, 240=H4, 1440=D1, 10080=W1, 43200=MN1)
            start_pos: Starting candle index (1 = current incomplete candle)
            count: Number of candles to fetch
        """
        params = {
            'SymbolName': symbol,
            'Timeframe': timeframe,
            'CopyRatesType': 1,  # 1=by position
            'StartPos': start_pos,
            'Count': count,
        }
        data = self._send_and_parse(CMD_COPY_RATES, params, expert_handle)
        val = self._get_value(data)
        return val if isinstance(val, list) else []

    def get_quote(self, symbol, expert_handle=None):
        """Get current quote (bid/ask) for a symbol."""
        data = self._send_and_parse(CMD_GET_QUOTE, {'Symbol': symbol}, expert_handle)
        return self._get_value(data)

    def get_symbols(self, expert_handle=None):
        """Get list of available symbols."""
        data = self._send_and_parse(CMD_GET_SYMBOLS, expert_handle=expert_handle)
        val = self._get_value(data)
        if isinstance(val, dict):
            return val.get('Symbols', [])
        return []

    def get_symbol_info_tick(self, symbol, expert_handle=None):
        """Get current tick info for a symbol."""
        data = self._send_and_parse(CMD_SYMBOL_INFO_TICK, {'Symbol': symbol}, expert_handle)
        return self._get_value(data)

    # ─────────────────────── Trading ───────────────────────────

    def get_orders(self, expert_handle=None):
        """Get all open orders (Pool=0 = MODE_TRADES)."""
        data = self._send_and_parse(CMD_GET_ORDERS, {'Pool': 0}, expert_handle)
        return self._get_value(data) or []

    def get_order(self, ticket, expert_handle=None):
        """Get a specific order by ticket number."""
        data = self._send_and_parse(CMD_GET_ORDER, {'Ticket': ticket}, expert_handle)
        return self._get_value(data)

    def order_send(self, order_params, expert_handle=None):
        """
        Send/place an order.

        Args:
            order_params: dict with order fields:
                - Symbol: str
                - OperationType: int (0=Buy, 1=Sell)
                - Volume: float (lots)
                - Price: float (optional, default=current)
                - Slippage: int (optional)
                - StopLoss: float (optional)
                - TakeProfit: float (optional)
                - Comment: str (optional)
                - Magic: int (optional)
        """
        data = self._send_and_parse(CMD_ORDER_SEND, order_params, expert_handle)
        return self._get_value(data)

    def order_close(self, ticket, slippage=0, expert_handle=None):
        """Close an order by ticket."""
        data = self._send_and_parse(CMD_ORDER_CLOSE, {'Ticket': ticket, 'Slippage': slippage}, expert_handle)
        return self._get_value(data)

    # ─────────────────────── Utilities ─────────────────────────

    def _format_time(self, mt_time):
        """Convert MT4 timestamp to readable string."""
        if mt_time and isinstance(mt_time, (int, float)) and mt_time > 0:
            return time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(mt_time))
        return str(mt_time)


# ═══════════════════════════════════════════════════════════════
#  Demo / Test
# ═══════════════════════════════════════════════════════════════

def main():
    print("=" * 64)
    print("  MT4 WebSocket Client — Test")
    print("=" * 64)

    client = Mt4WebSocketClient()

    try:
        client.connect()
        print(f"  ✅ Connected! Expert handle: {client.expert_handle}\n")
    except Mt4ApiError as e:
        print(f"  ❌ {e}")
        print()
        print("  Pastikan:")
        print("  - MT4 running dengan MtApi.ex4 di-attach ke chart")
        print("  - DLL imports & AutoTrading enabled")
        print("  - Port default 8222")
        return

    eh = client.expert_handle

    # Account info
    try:
        acc = client.get_account_info(eh)
        print(f"  📊 Account")
        print(f"     Name:     {acc.get('Name','?')}")
        print(f"     Number:   {acc.get('Number','?')}")
        print(f"     Balance:  ${float(acc.get('Balance',0)):,.2f}")
        print(f"     Equity:   ${float(acc.get('Equity',0)):,.2f}")
        print(f"     Profit:   ${float(acc.get('Profit',0)):,.2f}")
        print(f"     Currency: {acc.get('Currency','?')}")
        print(f"     Leverage: 1:{acc.get('Leverage','?')}")
        print(f"     Server:   {acc.get('Server','?')}")
    except Mt4ApiError as e:
        print(f"  ⚠️  Account: {e}")

    # Symbols
    print()
    try:
        syms = client.get_symbols(eh)
        print(f"  🔍 Symbols: {len(syms)} available")
        for s in syms[:30]:
            print(f"     - {s}")
        if len(syms) > 30:
            print(f"     ... and {len(syms)-30} more")
    except Mt4ApiError as e:
        print(f"  ⚠️  Symbols: {e}")

    # XAUUSD H1 candles
    print()
    try:
        print(f"  📈 XAUUSD H1 (5 candles)...")
        rates = client.copy_rates('XAUUSD', 60, 1, 5, eh)
        if rates:
            print(f"     Got {len(rates)} candles")
            for i, r in enumerate(rates):
                print(f"     [{i}] {client._format_time(r.get('MtTime'))}  "
                      f"O:{float(r.get('Open',0)):.2f}  H:{float(r.get('High',0)):.2f}  "
                      f"L:{float(r.get('Low',0)):.2f}  C:{float(r.get('Close',0)):.2f}  "
                      f"V:{r.get('TickVolume',0)}")
        else:
            print("     No data")
    except Mt4ApiError as e:
        print(f"  ⚠️  Rates: {e}")

    # EURUSD H1 candles
    print()
    try:
        print(f"  📈 EURUSD H1 (10 candles)...")
        rates = client.copy_rates('EURUSD', 60, 1, 10, eh)
        if rates:
            print(f"     Got {len(rates)} candles")
            for i, r in enumerate(rates):
                print(f"     [{i}] {client._format_time(r.get('MtTime'))}  "
                      f"O:{float(r.get('Open',0)):.5f}  H:{float(r.get('High',0)):.5f}  "
                      f"L:{float(r.get('Low',0)):.5f}  C:{float(r.get('Close',0)):.5f}  "
                      f"V:{r.get('TickVolume',0)}")
        else:
            print("     No data")
    except Mt4ApiError as e:
        print(f"  ⚠️  EURUSD: {e}")

    # Current quote
    print()
    try:
        q = client.get_quote('XAUUSD', eh)
        if q:
            tick = q.get('Tick', {})
            print(f"  💰 XAUUSD Quote")
            print(f"     Bid: {tick.get('Bid','?')}   Ask: {tick.get('Ask','?')}")
            print(f"     Time: {client._format_time(tick.get('Time'))}")
    except Mt4ApiError as e:
        print(f"  ⚠️  Quote: {e}")

    client.disconnect()
    print(f"\n  ✅ Done.")
    print("=" * 64)


if __name__ == '__main__':
    main()
