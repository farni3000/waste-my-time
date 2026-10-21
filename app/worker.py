from __future__ import annotations

import sys
import time

from .db import run_migrations
from .jobs import process_next


def main() -> None:
    run_migrations()
    print("Worker gestartet. Warte auf Jobs … (Ctrl+C zum Beenden)", flush=True)
    try:
        while True:
            if not process_next():
                time.sleep(1)
    except KeyboardInterrupt:
        print("\nWorker beendet.", flush=True)
        sys.exit(0)


if __name__ == "__main__":
    main()
