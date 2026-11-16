"""Windowless Start menu entry, with local diagnostics if startup fails."""
from pathlib import Path
import runpy

runpy.run_path(str(Path(__file__).with_name('launch.py')), run_name='__main__')
