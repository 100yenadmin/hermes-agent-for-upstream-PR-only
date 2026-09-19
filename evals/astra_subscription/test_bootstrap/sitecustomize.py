"""Bound canonical run_tests.sh's compileall -j 0 to four workers on this host."""
import os

_actual_cpu_count = os.cpu_count
os.cpu_count = lambda: min(4, _actual_cpu_count() or 1)
