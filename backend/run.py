"""Extension-owned entry point. Run with python -I -S -u backend/run.py."""
import pathlib
import sys

if sys.version_info < (3, 12):
    sys.stderr.write("Amberog requires Python 3.12 or newer.\n")
    raise SystemExit(1)

# Isolated mode excludes the working directory, PYTHONPATH, and user site packages.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from dev_snapshot.rpc import main

if __name__ == "__main__":
    main()
