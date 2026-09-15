import sys
from pathlib import Path

# the service modules import each other flat (import bars, import store), the
# the same way the service does at runtime, so the service directory goes on the path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
