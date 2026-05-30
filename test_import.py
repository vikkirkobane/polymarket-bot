import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
print("Importing backend.main...")
try:
    from backend.main import app
    print("Successfully imported backend.main")
except Exception as e:
    print(f"Error importing backend.main: {e}")
    import traceback
    traceback.print_exc()
