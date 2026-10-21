"""Windowless Start menu entry, with local diagnostics if startup fails."""
import os
from pathlib import Path
import runpy
import sys
import traceback

root = Path(__file__).resolve().parents[1]
os.chdir(root)
sys.path.insert(0, str(root))
sys.argv = ['skill-pilot', 'ui']
with (root / 'launcher.log').open('w', encoding='utf-8') as log:
    sys.stdout = sys.stderr = log
    try:
        runpy.run_module('skill_pilot', run_name='__main__')
    except Exception:
        traceback.print_exc()
