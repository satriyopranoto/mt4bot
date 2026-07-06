#!/usr/bin/env python3
# test_mt4_connect.py — Test connection to MT4 via MtApi bridge
# -*- coding: utf-8 -*-

import os, sys, time
import clr

# Path ke MtApi DLL (sesuai install location)
MTAPI_PATH = r"C:\Program Files (x86)\MtApi"
sys.path.append(MTAPI_PATH)

# Load MtApi assembly with full path
asm = clr.AddReference(os.path.join(MTAPI_PATH, 'MtApi.dll'))
import MtApi as mt

# Default connection settings
MTSERV = '127.0.0.1'
MTPORT = 8222  # MT4 default port

print("=" * 60)
print("  MT4 Connection Test via MtApi Bridge")
print("=" * 60)
print(f"  MtApi DLL path: {MTAPI_PATH}")
print(f"  Server: {MTSERV}:{MTPORT}")
print()

# Create MtApi client
client = mt.MtApiClient()

# Connection states
# 0 = Disconnected, 1 = Connecting, 2 = Connected, 3 = Failed

print(f"  Initial connection state: {client.ConnectionState}")
print()
print(f"  Connecting to {MTSERV}:{MTPORT}...")

try:
    # Attempt connection
    client.BeginConnect(MTSERV, MTPORT)

    # Wait for connection with timeout
    max_attempts = 30
    for attempt in range(1, max_attempts + 1):
        time.sleep(1)
        state = client.ConnectionState
        if state == 0:
            print(f"  [{attempt}] Disconnected...")
        elif state == 1:
            print(f"  [{attempt}] Connecting...", end='\r')
        elif state == 2:
            print(f"  [{attempt}] ✅ CONNECTED!")
            break
        elif state == 3:
            print(f"  [{attempt}] ❌ Connection FAILED")
            break

    print()
    if client.ConnectionState == 2:
        print("  ✅ Successfully connected to MT4!")
        print(f"  Connection state: {client.ConnectionState}")
        
        # Get account info
        try:
            account = client.AccountInfo()
            print(f"\n  Account Info:")
            print(f"    - Name:     {account.Name}")
            print(f"    - Balance:  {account.Balance}")
            print(f"    - Equity:   {account.Equity}")
            print(f"    - Currency: {account.Currency}")
            print(f"    - Leverage: {account.Leverage}")
            print(f"    - Server:   {account.Server}")
        except Exception as e:
            print(f"\n  ⚠️  Could not get account info: {e}")
        
        # Try to get some market data
        try:
            # Get EURUSD H1 bars
            print(f"\n  Fetching EURUSD H1 data...")
            rates = client.CopyRates("EURUSD", mt.ENUM_TIMEFRAMES.PERIOD_H1, 1, 5)
            print(f"  Got {len(rates)} candles")
            for i, r in enumerate(rates):
                print(f"    [{i}] Time:{r.Time}  O:{r.Open:.5f}  H:{r.High:.5f}  L:{r.Low:.5f}  C:{r.Close:.5f}  V:{r.TickVolume}")
        except Exception as e:
            print(f"  ⚠️  Could not fetch rates: {e}")
            # Try with different approach
            try:
                sym = "EURUSD"
                tf = mt.ENUM_TIMEFRAMES.PERIOD_H1
                print(f"  Trying alternative: CopyRates({sym}, {tf}, 0, 3)...")
                rates2 = client.CopyRates(sym, tf, 0, 3)
                print(f"  Got {len(rates2)} candles")
            except Exception as e2:
                print(f"  ⚠️  Also failed: {e2}")
    else:
        print("  ❌ Failed to connect to MT4.")
        print()
        print("  Possible reasons:")
        print("  1. MT4 is not running — start MT4 first")
        print("  2. MtApi.ex4 EA not attached to any chart")
        print("  3. DLL imports not enabled in MT4 (Tools > Options > Expert Advisors > Allow DLL imports)")
        print("  4. Auto-trading not enabled (Alt+T or button)")
        print("  5. Port mismatch — check EA parameters (default: 8222)")
        print()
        print("  Quick steps to fix:")
        print("  a) Open MT4")
        print("  b) Drag MtApi.ex4 from Navigator panel to a chart (e.g. EURUSD)")
        print("  c) In the EA Properties dialog, go to 'Common' tab")
        print("  d) Check 'Allow DLL imports'")
        print("  e) Check 'Allow live trading'")
        print("  f) Click OK")
        print("  g) Make sure AutoTrading button is green (Alt+T)")

finally:
    # Clean disconnect
    if client.ConnectionState == 2:
        print(f"\n  Disconnecting...")
        client.BeginDisconnect()
        time.sleep(1)
    
print()
print("=" * 60)
print("  Test complete.")
print("=" * 60)
