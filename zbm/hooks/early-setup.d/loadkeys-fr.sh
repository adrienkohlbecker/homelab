#!/bin/bash
# Load the French keyboard layout so the ZBM recovery shell is usable
# on AZERTY keyboards without mentally mapping every key.
loadkeys fr 2>/dev/null || true
