"""
Application settings — loaded from environment variables and config files.
"""

from __future__ import annotations

import os
from pathlib import Path

# Project paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = PROJECT_ROOT / "backend"
CONFIG_DIR = BACKEND_DIR / "config"
DEFAULT_CONFIG_PATH = CONFIG_DIR / "default_config.yaml"

# Database
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"sqlite+aiosqlite:///{PROJECT_ROOT / 'data' / 'trading.db'}",
)

# Broker
BROKER_TYPE = os.getenv("BROKER_TYPE", "rithmic")  # rithmic | tradovate

# Rithmic
RITHMIC_USER = os.getenv("RITHMIC_USER", "")
RITHMIC_PASSWORD = os.getenv("RITHMIC_PASSWORD", "")
RITHMIC_SYSTEM = os.getenv("RITHMIC_SYSTEM", "Rithmic Paper Trading")
RITHMIC_SERVER = os.getenv("RITHMIC_SERVER", "")

# Tradovate (fallback)
TRADOVATE_USERNAME = os.getenv("TRADOVATE_USERNAME", "")
TRADOVATE_PASSWORD = os.getenv("TRADOVATE_PASSWORD", "")
TRADOVATE_APP_ID = os.getenv("TRADOVATE_APP_ID", "")
TRADOVATE_APP_VERSION = os.getenv("TRADOVATE_APP_VERSION", "1.0")
TRADOVATE_CID = os.getenv("TRADOVATE_CID", "")
TRADOVATE_SECRET = os.getenv("TRADOVATE_SECRET", "")
TRADOVATE_DEMO = os.getenv("TRADOVATE_DEMO", "true").lower() == "true"

# Server
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
