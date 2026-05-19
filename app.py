#!/usr/bin/env python3
"""
app.py – Entry point for BriskHeat LYNX Dashboard
All logic lives in lynx_dashboard.py.
"""

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

from lynx_dashboard import run

if __name__ == "__main__":
    run()
