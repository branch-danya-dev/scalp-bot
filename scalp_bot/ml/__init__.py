"""Offline training and isolated shadow worker; no trading runtime authority."""

STAGE = "M2_TECHNICAL_M3_OFFLINE"
# A checkout does not bundle weights; inspect a saved artifact manifest for trained status.
MODEL_TRAINED = False
RUNTIME_CONNECTED = False
