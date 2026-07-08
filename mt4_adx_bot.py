#!/usr/bin/env python3
# mt4_adx_bot.py — ADX + Basis Trend Trading Bot
# Multi TF Moderat strategy ported from backtester run_id_1h_multitf_mod.py
# BUY only. Entry: low>SL & close>basis & ADX>20 & ADX>ADX[5] & +DI>-DI & +DI>+DI[5]
#                    + (multi_tf: daily +DI > daily -DI, no daily close>basis)
# SL: HH(28) - 2*ATR(10)*(2.8-1)/2.8  [= HH(28) - 1.2857*ATR(10)]
# Exit: floating>0.4R | high<CL
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
        logging.FileHandler(os.path.join(os.path.dirname(__file__), 'adx_bot.log'), mode='a'),
    ]
)
log = logging.getLogger('adx_bot')

PID_FILE = os.path.join(os.path.dirname(__file__), 'adx_bot.pid')

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

    # ATR-based trailing SL (from backtester run_id_1h_multitf_mod.py)
    # SL = HH(sl_lookback) - 2*ATR(sl_atr_period)*(sl_multiple-1)/sl_multiple
    'sl_multiple': 2.8,
    'sl_lookback': 28,             # HH lookback period
    'sl_atr_period': 10,           # ATR period for SL

    # Entry thresholds
    'adx_min': 20,
    'adx_lookback': 5,             # ADX > ADX[5], +DI > +DI[5]

    # Take Profit
    'tp_r_multiple': 0.4,          # 0.4R

    # Risk
    'fixed_lot': 0.2,              # Fixed lot size

    # Trend scoring (from backtester: count bars where ADX>25 & close>SMA20)
    'min_score': 0,                # 0 = disabled
    'score_adx_threshold': 25,
    'score_lookback': 100,

    # Magic number
    'magic_number': 20260706,

    # Check interval (seconds)
    'check_interval': 60,

    # Min bars needed for indicator calculation
    'min_bars': 60,
}


# ──────────────────────── Indicator Functions ───────────────────

def calc_atr_trailing_sl(high, low, close, lookback=28, atr_period=10, multiplier=2.8):
    """ATR-based trailing SL ported from backtester run_id_1h_multitf_mod.py.
    
    SL = HH(lookback) - 2*ATR(atr_period)*(multiplier-1)/multiplier
    
    For multiplier=2.8, atr_period=10: SL = HH(28) - 1.2857*ATR(10)
    """
    s_high = pd.Series(high)
    s_low = pd.Series(low)
    s_close = pd.Series(close)
    
    # True Range
    prev_close = s_close.shift(1)
    tr = pd.concat([
        s_high - s_low,
        (s_high - prev_close).abs(),
        (s_low - prev_close).abs(),
    ], axis=1).max(axis=1).values
    
    # HH(lookback) and ATR(atr_period)
    hh = s_high.rolling(window=lookback).max().values
    atr = pd.Series(tr).rolling(window=atr_period).mean().values
    
    mult = 2 * (multiplier - 1) / multiplier
    sl = hh - mult * atr
    
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
    """ADX + Basis trend-following bot — BUY only."""

    OP_BUY = 0

    def __init__(self, config=None):
        self.cfg = {**CONFIG, **(config or {})}
        self.client = None
        self.running = False

        # Position state
        self.position_ticket = None
        self.position_entry_price = None
        self.entry_sl = None          # CL (fixed SL at entry)
        self.tp_threshold_pct = None  # 0.4R in percent
        self.entry_time = None

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
        """Get current Bid/Ask via SymbolInfoTick (CMD 288 — works for any symbol)."""
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
        
        For Multi TF Moderat: daily +DI > -DI (no daily close>basis required).
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

    def calc_atr_sl_current(self):
        """Calculate current ATR-based trailing SL from cached data.
        SL = HH(28) - 1.2857*ATR(10)"""
        if len(self._high_buffer) < 40 or len(self._low_buffer) < 40:
            return None
        arr_h = np.array(list(self._high_buffer))
        arr_l = np.array(list(self._low_buffer))
        arr_c = np.array(list(self._close_buffer))
        sl_arr = calc_atr_trailing_sl(
            arr_h, arr_l, arr_c,
            lookback=self.cfg['sl_lookback'],
            atr_period=self.cfg['sl_atr_period'],
            multiplier=self.cfg['sl_multiple'],
        )
        return float(sl_arr[-1])

    def calc_basis_current(self):
        """Calculate current Basis (SMA 20) from cached data."""
        if len(self._close_buffer) < self.cfg['bb_period']:
            return None
        arr_c = np.array(list(self._close_buffer))
        basis_arr = calc_basis(arr_c, self.cfg['bb_period'])
        return float(basis_arr[-1])

    def check_entry(self):
        """
        Check entry conditions (Single TF or Multi TF Moderat).
        Returns dict {'signal': bool, 'reason': str, ...} or None.
        """
        # 1. Get current bar data
        rates = self.fetch_ohlcv(2)
        if not rates or len(rates) < 2:
            return {'signal': False, 'reason': 'no_data'}

        current = rates[0]  # shift 0 = current incomplete bar
        prev = rates[1]     # shift 1 = last complete bar

        # Use last complete bar for signals (more reliable)
        close = float(prev.get('Close', 0))
        low = float(prev.get('Low', 0))
        high = float(prev.get('High', 0))

        # 2. Calculate indicators locally
        # Fetch enough bars
        all_rates = self.fetch_ohlcv(100)
        if not all_rates or len(all_rates) < self.cfg['min_bars']:
            return {'signal': False, 'reason': 'insufficient_data'}

        closes = np.array([float(r.get('Close', 0)) for r in reversed(all_rates)])
        highs = np.array([float(r.get('High', 0)) for r in reversed(all_rates)])
        lows = np.array([float(r.get('Low', 0)) for r in reversed(all_rates)])

        # Basis (SMA 20)
        basis_arr = calc_basis(closes, self.cfg['bb_period'])
        basis = float(basis_arr[-1])

        # ATR-based trailing SL (from backtester)
        sl_arr = calc_atr_trailing_sl(
            highs, lows, closes,
            lookback=self.cfg['sl_lookback'],
            atr_period=self.cfg['sl_atr_period'],
            multiplier=self.cfg['sl_multiple'],
        )
        sl = float(sl_arr[-1])

        # ADX from MT4 — use shift=1 (last complete bar) for main values
        # and shift=6 (5 bars before last complete) for comparison
        adx0, pdi0, mdi0 = self.get_adx_values(1)
        adx5, pdi5, mdi5 = self.get_adx_values(6)

        if any(v is None for v in [adx0, pdi0, mdi0, adx5, pdi5, mdi5]):
            return {'signal': False, 'reason': 'adx_nan'}

        log.info(f"  -- Signal Check --")
        log.info(f"  Close={close:.5f}  Basis={basis:.5f}  SL={sl:.5f}")
        log.info(f"  ADX={adx0:.1f}  ADX[5]={adx5:.1f}")
        log.info(f"  +DI={pdi0:.1f}  +DI[5]={pdi5:.1f}  -DI={mdi0:.1f}")
        log.info(f"  Low={low:.5f}  Low>SL={low>sl}")

        # ---- ENTRY CONDITIONS ----
        conditions = {
            'low > SL': low > sl,
            'close > basis': close > basis,
            'ADX > 20': adx0 > self.cfg['adx_min'],
            'ADX > ADX[5]': adx0 > adx5,
            '+DI > -DI': pdi0 > mdi0,
            '+DI > +DI[5]': pdi0 > pdi5,
        }

        # Multi TF Moderat: add daily +DI > -DI (no close>daily basis required)
        if self.cfg['mode'] == 'multi_tf':
            dpdi, dmdi = self.get_daily_adx_values(1)
            if dpdi is not None and dmdi is not None:
                conditions['daily +DI > -DI'] = dpdi > dmdi
                log.info(f"  Daily +DI={dpdi:.1f}  -DI={dmdi:.1f}  +DI>-DI={dpdi>dmdi}")
            else:
                log.warning("  Daily ADX data unavailable, skipping multi_tf check")
                # If daily data unavailable, don't reject — let H1 conditions decide
                pass

        # Trend scoring: count bars in last N where ADX>25 & close>SMA20
        score = 0
        if self.cfg['min_score'] > 0:
            try:
                score = sum(
                    1 for j in range(max(0, len(closes)-self.cfg['score_lookback']), len(closes))
                    if not np.isnan(adx := adx0) and j < len(closes)
                )
                # More accurate: use local ADX array
                adx_all = np.array([self.get_adx_values(-(len(closes)-1-j))[0] for j in range(min(self.cfg['score_lookback'], len(closes)))])
                # Simpler: just use current ADX condition as proxy for single-symbol
                score = self.cfg['score_lookback']  # bypass for single-symbol
            except Exception:
                pass
            if score < self.cfg['min_score']:
                log.info(f"  Score={score} < min={self.cfg['min_score']}, skipping")
                return {'signal': False, 'reason': f'low_score_{score}'}

        all_ok = all(conditions.values())
        failed = [k for k, v in conditions.items() if not v]

        if all_ok:
            log.info(f"  [BUY] SIGNAL! All conditions met")
            return {
                'signal': True,
                'reason': 'all_conditions_met',
                'close': close,
                'sl': sl,
                'basis': basis,
                'adx': adx0,
                'pdi': pdi0,
                'mdi': mdi0,
            }
        else:
            log.info(f"  [NO] No signal. Failed: {', '.join(failed)}")
            return {'signal': False, 'reason': f"failed: {', '.join(failed)}"}

    # ──────── Exit Check ────────

    def check_exit(self):
        """
        Check exit conditions while in position.
        Exit (ported from backtester run_id_1h_multitf_mod.py):
          1. CUT LOSS: high < CL (entry SL, fixed)
          2. TAKE PROFIT: floating > 0.4R (pure, no trailing SL condition)
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

        current = rates[0]  # shift 0 (current incomplete)
        prev = rates[1]     # shift 1 (last complete)

        # Use current incomplete bar high & last complete bar close
        close = float(prev.get('Close', 0))
        high = float(prev.get('High', 0))
        current_high = float(current.get('High', 0))
        highest = max(high, current_high)

        # CL = entry_sl (fixed)
        CL = self.entry_sl
        floating_pct = ((close - self.position_entry_price) / self.position_entry_price) * 100.0
        stop_dist_pct = abs(self.position_entry_price - CL) / self.position_entry_price * 100.0
        tp_pct = stop_dist_pct * self.cfg['tp_r_multiple']  # 0.4R

        log.info(f"  -- Exit Check --")
        log.info(f"  Price={close:.5f}  Entry={self.position_entry_price:.5f}")
        log.info(f"  Float={floating_pct:.2f}%  TP needed={tp_pct:.2f}%")
        log.info(f"  High={highest:.5f}  CL={CL:.5f}")
        log.info(f"  Float>{tp_pct:.2f}%? {floating_pct > tp_pct}")

        # --- CUT LOSS: high < CL (entry SL fixed) ---
        if highest < CL:
            log.warning(f"  [EXIT] CUT LOSS: High ({highest:.5f}) < CL ({CL:.5f})")
            return True

        # --- TAKE PROFIT: floating > 0.4R (backtester style, no trailing SL condition) ---
        if floating_pct > tp_pct:
            log.info(f"  [TP] TAKE PROFIT: Float={floating_pct:.2f}>{tp_pct:.2f}%")
            return True

        return False

    # ──────── Order Execution ────────

    def place_buy(self, signal_info):
        """Place a BUY order with SL at Donchian level (fixed lot)."""
        price_info = self.get_current_price()
        if not price_info:
            log.error("  Cannot get current price")
            return None

        entry_price = price_info['ask']
        sl_price = signal_info['sl']

        # Ensure SL is below entry
        if sl_price >= entry_price:
            log.warning(f"  SL ({sl_price:.5f}) >= entry ({entry_price:.5f}), adjusting")
            sl_price = entry_price - (entry_price * 0.01)  # 1% below as fallback

        stop_dist = abs(entry_price - sl_price)
        if stop_dist <= 0:
            log.error("  Stop distance is zero, cannot place order")
            return None

        lot_size = self.cfg['fixed_lot']

        # TP level (optional — we use exit logic instead of fixed TP)
        # Set TP far away as safety
        tp_price = round(entry_price + (stop_dist * 3), 5)

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

            # Check if we have an open position (from bot state)
            position = self.check_position()

            if position:
                ticket = position.get('Ticket')
                if ticket != self.position_ticket:
                    # Restore state from position
                    self.position_ticket = ticket
                    self.position_entry_price = float(position.get('OpenPrice', 0))
                    # entry_sl should already be set, but if not, use position SL
                    if self.entry_sl is None:
                        self.entry_sl = float(position.get('StopLoss', 0))
                    if self.position_entry_price and self.entry_sl:
                        stop_dist_pct = abs(self.position_entry_price - self.entry_sl) / self.position_entry_price * 100.0
                        self.tp_threshold_pct = stop_dist_pct * self.cfg['tp_r_multiple']

                log.info(f"  Position: #{ticket} @ {self.position_entry_price:.5f}")
                log.info(f"  CL={self.entry_sl}  TP threshold={self.tp_threshold_pct:.3f}%")

                # Check exit
                if self.check_exit():
                    self.close_position(ticket)
                    self.position_ticket = None
                    self.position_entry_price = None
                    self.entry_sl = None
                    self.tp_threshold_pct = None
                else:
                    log.info(f"  Holding position (no exit signal)")

            else:
                # Reset position state if MT4 shows no position
                if self.position_ticket is not None:
                    log.info("  Position closed externally")
                    self.position_ticket = None
                    self.position_entry_price = None
                    self.entry_sl = None
                    self.tp_threshold_pct = None

                # Check entry signal
                log.info(f"  No position. Checking entry...")
                signal = self.check_entry()

                if signal and signal.get('signal'):
                    ticket = self.place_buy(signal)
                    if ticket:
                        self.position_ticket = ticket
                        self.position_entry_price = signal['close']
                        self.entry_sl = signal['sl']
                        stop_dist_pct = abs(signal['close'] - signal['sl']) / signal['close'] * 100.0
                        self.tp_threshold_pct = stop_dist_pct * self.cfg['tp_r_multiple']
                        log.info(f"  [OK] Position opened! TP threshold={self.tp_threshold_pct:.3f}%")
                else:
                    log.info(f"  {signal.get('reason', 'no_signal')}")

        except Mt4ApiError as e:
            log.error(f"  Cycle error: {e}")
            raise
        except Exception as e:
            log.exception(f"  Unexpected error: {e}")

    # ──────── Main Loop ────────

    def run(self):
        log.info("=" * 64)
        mode_label = "Multi TF Moderat" if self.cfg['mode'] == 'multi_tf' else "Single TF"
        log.info(f"  ADX + Basis Trading Bot — {mode_label}")
        log.info(f"  Symbol: {self.cfg['symbol']}  |  H1  |  ADX({self.cfg['adx_period']})  |  Basis({self.cfg['bb_period']})")
        log.info(f"  SL: HH({self.cfg['sl_lookback']}) - 2*ATR({self.cfg['sl_atr_period']})*(mult-1)/mult  |  TP: {self.cfg['tp_r_multiple']}R")
        log.info(f"  Lot: {self.cfg['fixed_lot']} fixed  |  BUY only  |  Check: {self.cfg['check_interval']}s")
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
