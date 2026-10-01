#!/usr/bin/env python3
"""
app.py – Entry point for BriskHeat LYNX Dashboard
All logic lives in lynx_dashboard.py.
"""

import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Resolve app directory — works under systemd, venv, or direct invocation.
# Priority: LYNX_APP_DIR env var → directory of this file (always abspath).
APP_DIR = os.environ.get("LYNX_APP_DIR") or \
          os.path.dirname(os.path.abspath(__file__))

# Ensure the app directory is on sys.path so sibling modules are importable
# regardless of the current working directory set by systemd.
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

# Change to app directory so any relative path fallbacks resolve correctly
os.chdir(APP_DIR)

import lynx_dashboard
lynx_dashboard.APP_DIR = APP_DIR   # override before run()

from lynx_dashboard import run

if __name__ == "__main__":
    run()
