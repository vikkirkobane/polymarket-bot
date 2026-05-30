#!/usr/bin/env python3
"""
Polymarket Bot — Command Center

Run:  python run.py
Open: http://localhost:8000
"""

import uvicorn
import os
import sys
from pathlib import Path

# Ensure backend is importable
sys.path.insert(0, str(Path(__file__).parent))

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    print(f"\n[PolyBot] Command Center starting...")
    print(f"[PolyBot] Dashboard -> http://localhost:{port}\n")
    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=port,
        reload=True,
        log_level="info",
    )
