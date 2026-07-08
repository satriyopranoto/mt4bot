#!/usr/bin/env python3
"""Debug: dump raw order data from MT4."""
import sys, os, json
sys.path.insert(0, os.path.dirname(__file__))
from mt4_client import Mt4WebSocketClient, Mt4ApiError

client = Mt4WebSocketClient()
try:
    client.connect()
    eh = client.expert_handle
    print(f"Connected, handle: {eh}")

    # Get raw orders
    raw = client._send_command(283, {'Pool': 0}, eh)  # CMD_GET_ORDERS
    print(f"\nRaw response:")
    print(raw[:2000] if raw else "None")

    client.disconnect()
except Exception as e:
    print(f"Error: {e}")
