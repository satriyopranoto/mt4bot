#!/usr/bin/env python3
# mt4_adx_bot_usdchf.py — ADX + Basis Trend Trading Bot (USDCHF)
# Copy of template, configured for USDCHF.
# BUY & SELL SHORT with Multi TF Moderat entry filter
# BUY:  low>SL & close>basis & ADX>20 & ADX>ADX[5] & +DI>-DI & +DI>+DI[5]
#       + (multi_tf: daily +DI > daily -DI)
# SELL: high<SL & close<basis & ADX>20 & ADX>ADX[5] & -DI>+DI & -DI>-DI[5]
#       + (multi_tf: daily -DI > daily +DI)
# SL: Donchian (2.8x10)
# Exit: (floating>0.4R & high<trailSL) | high<CL (LONG)
#        (abs(float)>0.4R & low>trailSL) | close>CL (SHORT)
# -*- coding: utf-8 -*-

import json
import time
import logging
import sys
import os
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mt4_client import Mt4WebSocketClient, Mt4ApiError

# ───────────────────────────── Setup ─────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(os.path.dirname(__file__), 'adx_bot_usdchf.log'), mode='a'),
    ]
)
log = logging.getLogger('adx_bot')

PID_FILE = os.path.join(os.path.dirname(__file__), 'adx_bot_usdchf.pid')

# ──────────────────────── Config ────────────────────────────────

CONFIG = {
    'host': '127.0.0.1',
    'port': 8222,

    # Symbol & timeframe
    'symbol': 'USDCHF',
    'timeframe': 60,               # H1

    # Mode
    'mode': 'multi_tf',            # 'single_tf' or 'multi_tf'

    # ADX
    'adx_period': 14,
    'daily_adx_period': 14,

    # Basis = SMA(close, 20)
    'bb_period': 20,

    # Donchian SL
    'sl_multiple': 2.8,
    'sl_period': 10,               # ero = int(2.8 * 10) = 28

    # Entry thresholds
    'adx_min': 20,
    'adx_lookback': 5,             # ADX > ADX[5], +DI > +DI[5] / -DI > -DI[5]

    # Take Profit
    'tp_r_multiple': 0.4,          # 0.4R

    # Risk
    'fixed_lot': 0.2,              # Default lot (overridden by symbol_lots if matched)
    'symbol_lots': {               # Per-symbol lot sizes
        'XAUUSD': 0.05,
        'USDCHF': 0.2,
    },

    # Magic number
    'magic_number': 20260706,

    # Check interval (seconds)
    'check_interval': 60,

    # Min bars needed for indicator calculation
    'min_bars': 60,
}


# ──────────────────────── Indicator Functions ───────────────────

def donchian_sl(high, low, atr_multiple=2.8, atr_period=10):
    """Donchian Channel SL — ported from backtester."""
    ero = int(atr_multiple * atr_period)
    s_high = pd.Series(high)
    s_low = pd.Series(low)

    r_prev = s_high.rolling(window=ero).max().shift(1).values
    s_prev = s_low.rolling(window=ero).min().shift(1).values
    r_curr = s_high.rolling(window=ero).max().values
    s_curr = s_low.rolling(window=ero).min().values

    ab = np.where(high > r_prev, 1, np.where(low < s_prev, -1, 0))
    ac = pd.Series(ab).replace(0, np.nan).ffill().fillna(0).values

    sl = np.where(ac == 1, s_curr, r_curr)
    return sl.astype(float)


def calc_basis(close, period=20):
    """Basis = SMA of close (middle Bollinger Band)."""
    return pd.Series(close).rolling(period).mean().values.astype(float)


def calc_adx_local(high, low, close, period=14):
    """ADX, +DI, -DI — ported from backtester."""
    s_high = pd.Series(high)
    s_low = pd.Series(low)
    s_close = pd.Series(close)

    prev_close = s_close.shift(1)

    tr = pd.concat([
        s_high - s_low,
        (s_high - prev_close).abs(),
        (s_low - prev_close).abs(),
    ], axis=1).max(axis=1)

    up_move = s_high - s_high.shift(1)
    down_move = s_low.shift(1) - s_low

    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0), index=s_close.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0), index=s_close.index)

    alpha = 1.0 / period
    smoothed_tr = tr.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    smoothed_plus = plus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    smoothed_minus = minus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean()

    pdi = 100 * smoothed_plus / smoothed_tr.replace(0, np.nan)
    mdi = 100 * smoothed_minus / smoothed_tr.replace(0, np.nan)

    dm_sum = pdi + mdi
    dx = 100 * (pdi - mdi).abs() / dm_sum.replace(0, np.nan)
    adx = dx.ewm(alpha=alpha, adjust=False, min_periods=period).mean()

    return adx.values.astype(float), pdi.values.astype(float), mdi.values.astype(float)


# ──────────────────────── Trading Bot ───────────────────────────

class BasisAdxBot:
    """ADX + Basis trend-following bot — BUY & SELL SHORT (Multi TF Moderat)."""

    OP_BUY = 0
    OP_SELL = 1

    def __init__(self, config=None):
        self.cfg = {**CONFIG, **(config or {})}
        # Resolve symbol-specific lot size
        sym = self.cfg['symbol']
        self.cfg['fixed_lot'] = self.cfg.get('symbol_lots', {}).get(sym, self.cfg['fixed_lot'])
        self.client = None
        self.running = False

        # Position state
        self.position_ticket = None
        self.position_entry_price = None
        self.entry_sl = None          # CL (fixed SL at entry)
        self.tp_threshold_pct = None  # 0.4R in percent
        self.entry_time = None
        self.position_direction = None  # 'BUY' or 'SELL'

        # Cached OHLCV data
        self._high_buffer = deque(maxlen=100)
        self._low_buffer = deque(maxlen=100)
        self._close_buffer = deque(maxlen=100)

    # ──────── Connection ────────

    def connect(self):
        self.client = Mt4WebSocketClient(self.cfg['host'], self.cfg['port'])
        try:
            self.client.connect()
            log.info(f"[OK] Connected to MT4 (handle: {self.client.expert_handle})")
            return True
        except Mt4ApiError as e:
            log.error(f"[FAIL] Connection failed: {e}")
            return False

    def disconnect(self):
        if self.client:
            try:
                self.client.disconnect()
            except Exception:
                pass
            log.info("Disconnected from MT4")

    # ──────── Data Fetching ────────

    def fetch_ohlcv(self, n_bars=60):
        """Fetch recent OHLCV data from MT4."""
        rates = self.client.copy_rates(
            self.cfg['symbol'], self.cfg['timeframe'],
            start_pos=1, count=n_bars
        )
        if not rates or len(rates) < n_bars:
            # Try fetching more bars from further back
            rates = self.client.copy_rates(
                self.cfg['symbol'], self.cfg['timeframe'],
                start_pos=1, count=n_bars + 50
            )
        return rates

    def fetch_and_cache(self):
        """Fetch fresh OHLCV data and update buffers."""
        rates = self.fetch_ohlcv(80)
        if not rates or len(rates) < self.cfg['min_bars']:
            log.warning(f"  Insufficient data: {len(rates) if rates else 0} bars")
            return False

        # Update buffers
        for r in rates:
            self._high_buffer.append(float(r.get('High', 0)))
            self._low_buffer.append(float(r.get('Low', 0)))
            self._close_buffer.append(float(r.get('Close', 0)))

        return True

    # ──────── Current Price ────────

    def get_current_price(self):
        """Get current Bid/Ask via SymbolInfoTick."""
        tick = self.client.get_symbol_info_tick(self.cfg['symbol'])
        if tick:
            return {
                'bid': float(tick.get('Bid', 0)),
                'ask': float(tick.get('Ask', 0)),
            }
        return None

    # ──────── ADX from MT4 ────────

    def get_adx_values(self, shift=0):
        """Fetch ADX, +DI, -DI from MT4 at given shift.

        For live trading, use shift=1 (last complete bar) for main signal,
        and shift=1+lookback for historical comparison.
        """
        CMD_IADX = 100
        base_params = {
            'Symbol': self.cfg['symbol'],
            'Timeframe': self.cfg['timeframe'],
            'Period': self.cfg['adx_period'],
            'AppliedPrice': 0,  # PRICE_CLOSE
        }

        def _fetch(mode, s):
            p = {**base_params, 'Mode': mode, 'Shift': s}
            raw = self.client._send_command(CMD_IADX, p)
            return json.loads(raw).get('Value') if raw else None

        adx = _fetch(0, shift)
        pdi = _fetch(1, shift)
        mdi = _fetch(2, shift)

        return adx, pdi, mdi

    def get_daily_adx_values(self, shift=0):
        """Fetch DAILY ADX, +DI, -DI from MT4 (timeframe=1440/D1).

        For Multi TF Moderat: daily +DI > -DI (BUY) or daily -DI > +DI (SELL).
        Use shift=1 for last complete daily bar.
        """
        CMD_IADX = 100
        base_params = {
            'Symbol': self.cfg['symbol'],
            'Timeframe': 1440,  # PERIOD_D1
            'Period': self.cfg['daily_adx_period'],
            'AppliedPrice': 0,
        }

        def _fetch(mode, s):
            p = {**base_params, 'Mode': mode, 'Shift': s}
            raw = self.client._send_command(CMD_IADX, p)
            return json.loads(raw).get('Value') if raw else None

        adx = _fetch(0, shift)
        pdi = _fetch(1, shift)
        mdi = _fetch(2, shift)

        return adx, pdi, mdi

    # ──────── Account Info ────────

    def get_balance(self):
        try:
            raw = self.client._send_command(40)  # CMD_ACCOUNT_BALANCE
            if raw:
                return float(json.loads(raw).get('Value', 0))
        except Exception:
            pass
        return None

    # ──────── Position Check ────────

    def check_position(self):
        """Check if we already have an open position for this symbol/magic."""
        try:
            orders = self.client.get_orders()
            if not orders:
                return None
            for o in orders:
                if (o.get('Symbol') == self.cfg['symbol'] and
                        o.get('MagicNumber') == self.cfg['magic_number']):
                    return o
            return None
        except Mt4ApiError:
            return None

    # ──────── Signal Detection ────────

    def calc_donchian_sl_current(self):
        """Calculate current Donchian SL from cached data."""
        if len(self._high_buffer) < 40 or len(self._low_buffer) < 40:
            return None
        arr_h = np.array(list(self._high_buffer))
        arr_l = np.array(list(self._low_buffer))
        sl_arr = donchian_sl(arr_h, arr_l, self.cfg['sl_multiple'], self.cfg['sl_period'])
        return float(sl_arr[-1])

    def calc_basis_current(self):
        """Calculate current Basis (SMA 20) from cached data."""
        if len(self._close_buffer) < self.cfg['bb_period']:
            return None
        arr_c = np.array(list(self._close_buffer))
        basis_arr = calc_basis(arr_c, self.cfg['bb_period'])
        return float(basis_arr[-1])

    def _calc_sl(self, highs, lows):
        """Calculate Donchian SL from arrays."""
        sl_arr = donchian_sl(highs, lows, self.cfg['sl_multiple'], self.cfg['sl_period'])
        return float(sl_arr[-1])

    def check_entry(self):
        """
        Check BUY entry conditions (Single TF or Multi TF Moderat).
        Returns dict {'signal': bool, 'reason': str, ...} or None.
        """
        rates = self.fetch_ohlcv(2)
        if not rates or len(rates) < 2:
            return {'signal': False, 'reason': 'no_data'}

        prev = rates[1]
        close = float(prev.get('Close', 0))
        low = float(prev.get('Low', 0))

        all_rates = self.fetch_ohlcv(100)
        if not all_rates or len(all_rates) < self.cfg['min_bars']:
            return {'signal': False, 'reason': 'insufficient_data'}

        closes = np.array([float(r.get('Close', 0)) for r in reversed(all_rates)])
        highs = np.array([float(r.get('High', 0)) for r in reversed(all_rates)])
        lows = np.array([float(r.get('Low', 0)) for r in reversed(all_rates)])

        basis_arr = calc_basis(closes, self.cfg['bb_period'])
        basis = float(basis_arr[-1])
        sl = self._calc_sl(highs, lows)

        adx0, pdi0, mdi0 = self.get_adx_values(1)
        adx5, pdi5, mdi5 = self.get_adx_values(6)
        if any(v is None for v in [adx0, pdi0, mdi0, adx5, pdi5, mdi5]):
            return {'signal': False, 'reason': 'adx_nan'}

        log.info(f"  -- LONG Signal Check --")
        log.info(f"  Close={close:.5f}  Basis={basis:.5f}  SL={sl:.5f}")
        log.info(f"  ADX={adx0:.1f}  ADX[5]={adx5:.1f}")
        log.info(f"  +DI={pdi0:.1f}  +DI[5]={pdi5:.1f}  -DI={mdi0:.1f}")
        log.info(f"  Low={low:.5f}  Low>SL={low>sl}")

        conditions = {
            'low > SL': low > sl,
            'close > basis': close > basis,
            'ADX > 20': adx0 > self.cfg['adx_min'],
            'ADX > ADX[5]': adx0 > adx5,
            '+DI > -DI': pdi0 > mdi0,
            '+DI > +DI[5]': pdi0 > pdi5,
        }

        # Multi TF: add daily +DI > -DI (no close>daily basis required)
        if self.cfg['mode'] == 'multi_tf':
            dpdi, dmdi = self.get_daily_adx_values(1)
            if dpdi is not None and dmdi is not None:
                conditions['daily +DI > -DI'] = dpdi > dmdi
                log.info(f"  Daily +DI={dpdi:.1f}  -DI={dmdi:.1f}  +DI>-DI={dpdi>dmdi}")

        all_ok = all(conditions.values())
        failed = [k for k, v in conditions.items() if not v]

        if all_ok:
            log.info(f"  [BUY] SIGNAL! All conditions met")
            return {
                'signal': True, 'reason': 'all_conditions_met',
                'close': close, 'sl': sl, 'basis': basis,
                'adx': adx0, 'pdi': pdi0, 'mdi': mdi0,
            }
        else:
            log.info(f"  [NO] No LONG. Failed: {', '.join(failed)}")
            return {'signal': False, 'reason': f"failed: {', '.join(failed)}"}

    def check_entry_short(self):
        """
        Check SELL entry conditions (Single TF or Multi TF Moderat).
        Returns dict {'signal': bool, 'reason': str, ...} or None.
        """
        rates = self.fetch_ohlcv(2)
        if not rates or len(rates) < 2:
            return {'signal': False, 'reason': 'no_data'}
        prev = rates[1]
        close = float(prev.get('Close', 0))
        high = float(prev.get('High', 0))

        all_rates = self.fetch_ohlcv(100)
        if not all_rates or len(all_rates) < self.cfg['min_bars']:
            return {'signal': False, 'reason': 'insufficient_data'}
        closes = np.array([float(r.get('Close', 0)) for r in reversed(all_rates)])
        highs = np.array([float(r.get('High', 0)) for r in reversed(all_rates)])
        lows = np.array([float(r.get('Low', 0)) for r in reversed(all_rates)])

        basis_arr = calc_basis(closes, self.cfg['bb_period'])
        basis = float(basis_arr[-1])
        sl = self._calc_sl(highs, lows)

        adx0, pdi0, mdi0 = self.get_adx_values(1)
        adx5, pdi5, mdi5 = self.get_adx_values(6)
        if any(v is None for v in [adx0, pdi0, mdi0, adx5, pdi5, mdi5]):
            return {'signal': False, 'reason': 'adx_nan'}

        log.info(f"  -- SHORT Signal Check --")
        log.info(f"  Close={close:.5f}  Basis={basis:.5f}  SL={sl:.5f}")
        log.info(f"  ADX={adx0:.1f}  ADX[5]={adx5:.1f}")
        log.info(f"  -DI={mdi0:.1f}  -DI[5]={mdi5:.1f}  +DI={pdi0:.1f}")
        log.info(f"  High={high:.5f}  High<SL={high<sl}")

        conditions = {
            'high < SL': high < sl,
            'close < basis': close < basis,
            'ADX > 20': adx0 > self.cfg['adx_min'],
            'ADX > ADX[5]': adx0 > adx5,
            '-DI > +DI': mdi0 > pdi0,
            '-DI > -DI[5]': mdi0 > mdi5,
        }

        # Multi TF: add daily -DI > +DI (bearish daily)
        if self.cfg['mode'] == 'multi_tf':
            dpdi, dmdi = self.get_daily_adx_values(1)
            if dpdi is not None and dmdi is not None:
                conditions['daily -DI > +DI'] = dmdi > dpdi
                log.info(f"  Daily +DI={dpdi:.1f}  -DI={dmdi:.1f}  -DI>+DI={dmdi>dpdi}")

        all_ok = all(conditions.values())
        failed = [k for k, v in conditions.items() if not v]

        if all_ok:
            log.info(f"  [SELL] SHORT SIGNAL! All conditions met")
            return {
                'signal': True, 'reason': 'all_conditions_met',
                'close': close, 'sl': sl, 'basis': basis,
                'adx': adx0, 'pdi': pdi0, 'mdi': mdi0,
            }
        else:
            log.info(f"  [NO] No SHORT. Failed: {', '.join(failed)}")
            return {'signal': False, 'reason': f"failed: {', '.join(failed)}"}

    # ──────── Exit Check ────────

    def check_exit(self):
        """
        Check exit conditions while in position.
          LONG:  TP=(floating>0.4R & high<trailSL) | CL=(high<entrySL)
          SHORT: TP=(abs(float)>0.4R & low>trailSL) | CL=(close>entrySL)
        Returns True if should exit.
        """
        if self.entry_sl is None or self.position_entry_price is None:
            return False

        rates = self.client.copy_rates(
            self.cfg['symbol'], self.cfg['timeframe'],
            start_pos=1, count=2
        )
        if not rates or len(rates) < 2:
            return False

        current = rates[0]
        prev = rates[1]
        close = float(prev.get('Close', 0))
        high = float(prev.get('High', 0))
        low = float(prev.get('Low', 0))
        current_high = float(current.get('High', 0))
        current_low = float(current.get('Low', 0))
        highest = max(high, current_high)
        lowest = min(low, current_low)

        # Calculate current Donchian SL (trailing)
        all_rates = self.fetch_ohlcv(80)
        if not all_rates or len(all_rates) < 30:
            return False
        highs = np.array([float(r.get('High', 0)) for r in reversed(all_rates)])
        lows = np.array([float(r.get('Low', 0)) for r in reversed(all_rates)])
        sl_arr = donchian_sl(highs, lows, self.cfg['sl_multiple'], self.cfg['sl_period'])
        current_sl = float(sl_arr[-1])

        CL = self.entry_sl
        stop_dist_pct = abs(self.position_entry_price - CL) / self.position_entry_price * 100.0
        tp_pct = stop_dist_pct * self.cfg['tp_r_multiple']

        if self.position_direction == 'BUY':
            floating_pct = ((close - self.position_entry_price) / self.position_entry_price) * 100.0
            log.info(f"  -- Exit Check (LONG) --")
            log.info(f"  Price={close:.5f}  Entry={self.position_entry_price:.5f}")
            log.info(f"  Float={floating_pct:.2f}%  TP needed={tp_pct:.2f}%")
            log.info(f"  High={highest:.5f}  TrailSL={current_sl:.5f}  CL={CL:.5f}")

            # TP: floating > 0.4R AND high < trailing SL
            if floating_pct > tp_pct and highest < current_sl:
                log.info(f"  [TP] Take Profit: Float>{tp_pct:.2f}% AND High<SL")
                return True
            # CL: high < entry SL (fixed)
            if highest < CL:
                log.warning(f"  [CL] Cut Loss: High ({highest:.5f}) < CL ({CL:.5f})")
                return True

        elif self.position_direction == 'SELL':
            floating_pct = ((self.position_entry_price - close) / self.position_entry_price) * 100.0
            log.info(f"  -- Exit Check (SHORT) --")
            log.info(f"  Price={close:.5f}  Entry={self.position_entry_price:.5f}")
            log.info(f"  Profit={floating_pct:.2f}%  TP needed={tp_pct:.2f}%")
            log.info(f"  Low={lowest:.5f}  TrailSL={current_sl:.5f}  CL={CL:.5f}")

            # TP for Short: floating > 0.4R AND low > trailing SL
            if floating_pct > tp_pct and lowest > current_sl:
                log.info(f"  [TP] Short TP: Profit>{tp_pct:.2f}% AND Low>SL")
                return True
            # CL for Short: close > entry SL
            if close > CL:
                log.warning(f"  [CL] Short CL: Close ({close:.5f}) > CL ({CL:.5f})")
                return True

        return False

    # ──────── Order Execution ────────

    def place_buy(self, signal_info):
        """Place a BUY order with ATR-based SL (fixed lot)."""
        price_info = self.get_current_price()
        if not price_info:
            log.error("  Cannot get current price")
            return None

        entry_price = price_info['ask']
        sl_price = signal_info['sl']

        if sl_price >= entry_price:
            log.warning(f"  SL ({sl_price:.5f}) >= entry ({entry_price:.5f}), adjusting")
            sl_price = entry_price - (entry_price * 0.01)

        stop_dist = abs(entry_price - sl_price)
        if stop_dist <= 0:
            log.error("  Stop distance is zero, cannot place order")
            return None

        lot_size = self.cfg['fixed_lot']
        tp_price = round(entry_price + (stop_dist * 3), 2)

        log.info(f"  -- Placing BUY --")
        log.info(f"  Symbol={self.cfg['symbol']}  Lots={lot_size}")
        log.info(f"  Entry={entry_price:.5f}  SL={sl_price:.5f}  TP={tp_price:.5f}")
        log.info(f"  Stop dist: {stop_dist:.5f} ({stop_dist/0.0001:.0f} pips)")

        order = {
            'Symbol': self.cfg['symbol'],
            'Cmd': self.OP_BUY,
            'Volume': lot_size,
            'Price': entry_price,
            'Slippage': 3,
            'Stoploss': sl_price,
            'Takeprofit': tp_price,
            'Comment': f'ADXBot {self.cfg["adx_period"]}',
            'Magic': self.cfg['magic_number'],
        }

        try:
            result = self.client.order_send(order)
            if result:
                ticket = int(result) if not isinstance(result, dict) else result.get('Value', 0)
                log.info(f"  [OK] BUY executed! Ticket #{ticket}")
                return ticket
            else:
                log.error("  [FAIL] Order failed: no response")
                return None
        except Mt4ApiError as e:
            log.error(f"  [FAIL] Order failed: {e}")
            return None

    def place_sell(self, signal_info):
        """Place a SELL order with ATR-based SL (fixed lot)."""
        price_info = self.get_current_price()
        if not price_info:
            log.error("  Cannot get current price")
            return None

        entry_price = price_info['bid']
        sl_price = signal_info['sl']

        if sl_price <= entry_price:
            log.warning(f"  SL ({sl_price:.5f}) <= entry ({entry_price:.5f}), adjusting")
            sl_price = entry_price + (entry_price * 0.01)

        stop_dist = abs(entry_price - sl_price)
        if stop_dist <= 0:
            log.error("  Stop distance is zero, cannot place order")
            return None

        lot_size = self.cfg['fixed_lot']
        tp_price = round(entry_price - (stop_dist * 3), 2)

        log.info(f"  -- Placing SELL --")
        log.info(f"  Symbol={self.cfg['symbol']}  Lots={lot_size}")
        log.info(f"  Entry={entry_price:.5f}  SL={sl_price:.5f}  TP={tp_price:.5f}")
        log.info(f"  Stop dist: {stop_dist:.5f} ({stop_dist/0.0001:.0f} pips)")

        order = {
            'Symbol': self.cfg['symbol'],
            'Cmd': self.OP_SELL,
            'Volume': lot_size,
            'Price': entry_price,
            'Slippage': 3,
            'Stoploss': sl_price,
            'Takeprofit': tp_price,
            'Comment': f'ADXBot Short {self.cfg["adx_period"]}',
            'Magic': self.cfg['magic_number'],
        }

        try:
            result = self.client.order_send(order)
            if result:
                ticket = int(result) if not isinstance(result, dict) else result.get('Value', 0)
                log.info(f"  [OK] SELL executed! Ticket #{ticket}")
                return ticket
            else:
                log.error("  [FAIL] SELL order failed: no response")
                return None
        except Mt4ApiError as e:
            log.error(f"  [FAIL] SELL order failed: {e}")
            return None

    def close_position(self, ticket):
        """Close position by ticket."""
        try:
            log.info(f"  Closing position #{ticket}...")
            self.client.order_close(ticket)
            log.info(f"  [OK] Position #{ticket} closed!")
            return True
        except Mt4ApiError as e:
            log.error(f"  [FAIL] Close failed: {e}")
            return False

    # ──────── Main Cycle ────────

    def run_cycle(self):
        """One trading cycle."""
        try:
            price = self.get_current_price()
            log.info(f"-- Cycle at {datetime.now().strftime('%H:%M:%S')} --")
            if price:
                log.info(f"  {self.cfg['symbol']}: Bid={price['bid']:.5f}  Ask={price['ask']:.5f}")

            position = self.check_position()

            if position:
                ticket = position.get('Ticket')
                if ticket != self.position_ticket:
                    # Restore state from position
                    self.position_ticket = ticket
                    self.position_entry_price = float(position.get('OpenPrice', 0))
                    cmd = int(position.get('Cmd', position.get('Operation', -1)))
                    self.position_direction = 'BUY' if cmd == 0 else 'SELL' if cmd == 1 else None
                    if self.entry_sl is None:
                        self.entry_sl = float(position.get('StopLoss', 0))
                    if self.position_entry_price and self.entry_sl:
                        stop_dist_pct = abs(self.position_entry_price - self.entry_sl) / self.position_entry_price * 100.0
                        self.tp_threshold_pct = stop_dist_pct * self.cfg['tp_r_multiple']

                log.info(f"  Position: #{ticket} {'LONG' if self.position_direction=='BUY' else 'SHORT'} @ {self.position_entry_price:.5f}")
                log.info(f"  CL={self.entry_sl}  TP threshold={self.tp_threshold_pct:.3f}%")

                if self.check_exit():
                    self.close_position(ticket)
                    self.position_ticket = None
                    self.position_entry_price = None
                    self.entry_sl = None
                    self.tp_threshold_pct = None
                    self.position_direction = None
                else:
                    log.info(f"  Holding position (no exit signal)")

            else:
                if self.position_ticket is not None:
                    log.info("  Position closed externally")
                    self.position_ticket = None
                    self.position_entry_price = None
                    self.entry_sl = None
                    self.tp_threshold_pct = None
                    self.position_direction = None

                # Check LONG entry first
                log.info(f"  No position. Checking LONG entry...")
                signal = self.check_entry()
                if signal and signal.get('signal'):
                    ticket = self.place_buy(signal)
                    if ticket:
                        self.position_ticket = ticket
                        self.position_entry_price = signal['close']
                        self.entry_sl = signal['sl']
                        self.position_direction = 'BUY'
                        stop_dist_pct = abs(signal['close'] - signal['sl']) / signal['close'] * 100.0
                        self.tp_threshold_pct = stop_dist_pct * self.cfg['tp_r_multiple']
                        log.info(f"  [OK] LONG opened! TP threshold={self.tp_threshold_pct:.3f}%")
                else:
                    # If no LONG, check SHORT
                    log.info(f"  No LONG. Checking SHORT entry...")
                    signal = self.check_entry_short()
                    if signal and signal.get('signal'):
                        ticket = self.place_sell(signal)
                        if ticket:
                            self.position_ticket = ticket
                            self.position_entry_price = signal['close']
                            self.entry_sl = signal['sl']
                            self.position_direction = 'SELL'
                            stop_dist_pct = abs(signal['close'] - signal['sl']) / signal['close'] * 100.0
                            self.tp_threshold_pct = stop_dist_pct * self.cfg['tp_r_multiple']
                            log.info(f"  [OK] SHORT opened! TP threshold={self.tp_threshold_pct:.3f}%")

        except Mt4ApiError as e:
            log.error(f"  Cycle error: {e}")
            raise
        except Exception as e:
            log.exception(f"  Unexpected error: {e}")

    # ──────── Main Loop ────────

    def run(self):
        mode_label = "Multi TF Moderat" if self.cfg['mode'] == 'multi_tf' else "Single TF"
        log.info("=" * 64)
        log.info(f"  ADX + Basis Trading Bot — {mode_label}")
        log.info(f"  Symbol: {self.cfg['symbol']}  |  H1  |  ADX({self.cfg['adx_period']})  |  Basis({self.cfg['bb_period']})")
        log.info(f"  Donchian SL: {self.cfg['sl_multiple']}x{self.cfg['sl_period']}  |  TP: {self.cfg['tp_r_multiple']}R")
        log.info(f"  Lot: {self.cfg['fixed_lot']} fixed  |  BUY & SELL SHORT")
        log.info("=" * 64)

        if not self.connect():
            log.error("Cannot start - connection failed")
            return

        # Sync position state on startup
        try:
            pos = self.check_position()
            if pos:
                self.position_ticket = pos.get('Ticket')
                self.position_entry_price = float(pos.get('OpenPrice', 0))
                self.entry_sl = float(pos.get('StopLoss', 0))
                cmd = int(pos.get('Cmd', pos.get('Operation', -1)))
                self.position_direction = 'BUY' if cmd == 0 else 'SELL' if cmd == 1 else None
                if self.position_entry_price and self.entry_sl:
                    sd = abs(self.position_entry_price - self.entry_sl) / self.position_entry_price * 100.0
                    self.tp_threshold_pct = sd * self.cfg['tp_r_multiple']
                log.info(f"  [RESTORE] Resumed position #{self.position_ticket}")
        except Exception:
            pass

        self.running = True
        reconnect_attempts = 0

        try:
            while self.running:
                try:
                    self.run_cycle()
                    reconnect_attempts = 0
                except Mt4ApiError as e:
                    reconnect_attempts += 1
                    wait = min(30 * reconnect_attempts, 300)
                    log.error(f"Connection error: {e}. Reconnecting in {wait}s...")
                    self.disconnect()
                    time.sleep(wait)
                    if not self.connect():
                        if reconnect_attempts >= 5:
                            log.error("Too many reconnection failures, stopping")
                            break
                        continue

                # Sleep interval (check per-minute for H1)
                for _ in range(self.cfg['check_interval']):
                    if not self.running:
                        break
                    time.sleep(1)

        except KeyboardInterrupt:
            log.info("Bot stopped by user")
        finally:
            self.running = False
            self.disconnect()
            log.info("Bot stopped")

    def stop(self):
        self.running = False


# ─────────────────────── Single Instance ────────────────────────

def check_pid():
    if os.path.exists(PID_FILE):
        with open(PID_FILE) as f:
            pid = f.read().strip()
        try:
            pid = int(pid)
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(1, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                log.warning(f"Bot already running (PID {pid})")
                sys.exit(0)
        except (ValueError, OSError):
            pass
    with open(PID_FILE, 'w') as f:
        f.write(str(os.getpid()))


def remove_pid():
    if os.path.exists(PID_FILE):
        os.remove(PID_FILE)


# ─────────────────────── Entry Point ────────────────────────────

if __name__ == '__main__':
    try:
        check_pid()
        bot = BasisAdxBot()
        bot.run()
    except KeyboardInterrupt:
        pass
    finally:
        remove_pid()
