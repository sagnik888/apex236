import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'python_scanner'))

import json
from scanner_engine import ScannerEngine

if __name__ == '__main__':
    engine = ScannerEngine()
    engine.run_all_scans(["1h"])
    chart = engine.get_chart_data("ABBOTINDIA", "1h")

    with open("test_chart_output.json", "w") as f:
        json.dump(chart, f, indent=2)

    print(f"Candles generated: {len(chart['candles'])}")
    print(f"Signals generated: {len(chart['signals'])}")

