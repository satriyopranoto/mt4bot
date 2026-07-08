# MT4 Bot — ADX + Basis Trading Bot

Python trading bot for MetaTrader 4 using ADX + Basis (SMA20) strategy with Multi TF Moderat filter. Connects to MT4 via [MtApi](https://mtapi.net/) WebSocket bridge.

## Prerequisites

- **MetaTrader 4** (MT4) — from your broker
- **MtApi** — WebSocket bridge between MT4 and Python
- **Python 3.11+** with `websocket-client`, `numpy`, `pandas`

## 1. Download & Install MetaTrader 4

1. **Download** from your broker's website (e.g., FTMO, IC Markets, etc.)
   - OR from MetaQuotes: https://www.metatrader4.com/
2. **Install** — run the installer, typical location:
   ```
   C:\Users\<you>\AppData\Roaming\MetaQuotes\Terminal\<instance_id>\MQL4\Experts\
   ```

## 2. Download MtApi

MtApi is the bridge that lets Python talk to MT4 via WebSocket.

1. **Download** MtApi MSI installer:
   - Official site: https://mtapi.net/
   - Direct: https://mtapi.net/download/MtApi_5.0.5.msi (check latest version)

2. **Install** the MSI:
   ```
   MtApi_5.0.5.msi
   ```
   Default path: `C:\Program Files (x86)\MtApi\`

3. **Verify** the install includes:
   - `MtApi.dll` — the bridge library
   - `MtApi.ex4` — the EA for MT4
   - `MtApiMonitor.exe` — connection monitor

## 3. Install MtApi EA in MT4

1. **Open MT4** and log in to your trading account

2. **Open MetaEditor** (F4 or Tools → MetaQuotes Language Editor)

3. **Locate Experts folder:**
   - In MetaEditor: File → Open Data Folder → `MQL4` → `Experts`
   - OR browse directly:
     ```
     %APPDATA%\MetaQuotes\Terminal\<instance_id>\MQL4\Experts\
     ```

4. **Copy MtApi EA files** from MtApi install folder:
   ```
   C:\Program Files (x86)\MtApi\MtApi.ex4
   ```
   Copy to your MT4 experts folder:
   ```
   %APPDATA%\MetaQuotes\Terminal\<instance_id>\MQL4\Experts\
   ```

5. **Enable automated trading:**
   - In MT4: Tools → Options → Expert Advisors
   - Check ✅ "Allow Automated Trading"
   - Check ✅ "Allow DLL imports"
   - Check ✅ "Allow WebRequest for URL: ws://127.0.0.1:8222"

6. **Attach EA to a chart:**
   - Drag `MtApi.ex4` from Navigator (View → Navigator or Ctrl+N) onto your desired chart (e.g., USDCHF H1)
   - In the Common tab: ✅ "Allow DLL imports" ✅ "Allow live trading"
   - Click OK

7. **Verify connection:**
   - You should see a smiley face 😊 on the top-right of the chart
   - MtApiMonitor.exe should show "Connected"

## 4. Setup Python Environment

```bash
# Clone the repo
git clone https://github.com/satriyopranoto/mt4bot.git
cd mt4bot

# Create virtual environment
python -m venv .venv

# Activate (Windows)
.venv\Scripts\activate

# Install dependencies
pip install websocket-client numpy pandas
```

## 5. Configuration

Edit `mt4_adx_bot.py` — look for the `CONFIG` dict:

```python
CONFIG = {
    'symbol': 'USDCHF',        # Trading symbol
    'timeframe': 60,           # H1
    'mode': 'multi_tf',        # 'multi_tf' or 'single_tf'

    'adx_period': 14,
    'bb_period': 20,

    # Donchian SL
    'sl_multiple': 2.8,
    'sl_period': 10,

    # Entry thresholds
    'adx_min': 20,
    'adx_lookback': 5,

    # Take Profit
    'tp_r_multiple': 0.4,

    # Lot size (auto-resolved by symbol)
    'fixed_lot': 0.2,
    'symbol_lots': {
        'XAUUSD': 0.05,
        'USDCHF': 0.2,
    },

    'magic_number': 20260706,
    'check_interval': 60,      # seconds
}
```

**Symbol lot auto-resolution:**
| Symbol  | Lot |
|---------|-----|
| USDCHF  | 0.2 |
| XAUUSD  | 0.05 |
| (default) | `fixed_lot` |

## 6. Run the Bot

```bash
# Make sure MT4 is running with MtApi EA attached

# Activate venv & run
.venv\Scripts\activate
python mt4_adx_bot.py
```

The bot will:
1. Connect to MT4 via WebSocket (127.0.0.1:8222)
2. Check for existing position → resume if found
3. Every 60 seconds:
   - If in position → check exit conditions
   - If no position → check entry signals

## 7. Check Position

```bash
python check_position.py
```

Shows account info, open positions, floating P&L.

## 8. Strategy Overview

### Entry — BUY (LONG)

**Single TF** (H1):
```
Low > Donchian SL
Close > Basis (SMA 20)
ADX > 20
ADX > ADX[5]
+DI > -DI
+DI > +DI[5]
```

**Multi TF Moderat** (H1 + daily):
```
All Single TF conditions
+ Daily +DI > Daily -DI   (no daily close > basis required)
```

### Entry — SELL (SHORT)

**Single TF** (H1):
```
High < Donchian SL
Close < Basis (SMA 20)
ADX > 20
ADX > ADX[5]
-DI > +DI
-DI > -DI[5]
```

**Multi TF Moderat** (H1 + daily):
```
All Single TF conditions
+ Daily -DI > Daily +DI
```

### Exit

**LONG:**
- **Take Profit:** `floating > 0.4R` AND `high < trailing Donchian SL`
- **Cut Loss:** `high < entry SL` (fixed)

**SHORT:**
- **Take Profit:** `abs(floating) > 0.4R` AND `low > trailing Donchian SL`
- **Cut Loss:** `close > entry SL` (fixed)

### SL Calculation

Donchian Channel SL:
- `ero = SL_multiple × SL_period = 28 bars`
- **LONG:** SL = lowest low of last 28 bars
- **SHORT:** SL = highest high of last 28 bars

## 9. Files

| File | Description |
|------|-------------|
| `mt4_adx_bot.py` | Main trading bot (template — XAUUSD) |
| `mt4_adx_bot_usdchf.py` | USDCHF variant of the bot |
| `mt4_client.py` | WebSocket client for MtApi |
| `check_position.py` | Check open positions & account |
| `debug_order.py` | Debug order details |
| `test_xauusd.py` | Test connection with XAUUSD |
| `test_mt4_connect.py` | Test MT4 connection |
| `adx_bot.log` | Bot activity log (XAUUSD) |
| `adx_bot_usdchf.log` | Bot activity log (USDCHF) |
| `adx_bot.pid` | PID file (XAUUSD) |
| `adx_bot_usdchf.pid` | PID file (USDCHF) |

## 10. Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| ❌ Connection refused | MtApi EA not attached to chart | Attach MtApi.ex4 to a chart |
| ❌ No data | Wrong symbol/timeframe | Check symbol name matches MT4 |
| ❌ Order failed | Market closed / no liquidity | Wait for market hours |
| ❌ Bot already running (PID) | Stale PID file | Delete `adx_bot.pid` |
| ⚠️ Wrong price decimals | XAUUSD vs Forex format | Bot auto-uses `.5f` for all |
