"""Constants for the SIM Anywhere integration."""

from __future__ import annotations

DOMAIN = "dongle_monitor"
DEFAULT_HOST = "openwrt.local"
DEFAULT_TIMEOUT = 10
RECONNECT_SECONDS = 10   # wait before reconnecting after a failed stream
STALL_SECONDS = 30       # the node sends a record every second; no record for this long means a dead stream
FIRST_SAMPLE_SECONDS = 30
PLATFORMS = ["sensor"]

CONF_HOST = "host"
STATUS_PATH = "/cgi-bin/sim-anywhere-api/status"
PAGE_PATH = "/sim-anywhere/"
