# SPDX-License-Identifier: GPL-3.0-or-later
"""Asegura que el root del repo sea importable (from src.… ) en pytest."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
