#!/usr/bin/env python3
"""Daily WikiPulse runner — collect + analyze + report."""

import json
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).parent

def run_step(name, args):
    print(f"\n{'='*50}")
    print(f"  {name}")
    print(f"{'='*50}")
    result = subprocess.run(
        [sys.executable] + args,
        cwd=str(PROJECT_DIR),
        capture_output=True,
        text=True,
        timeout=120
    )
    print(result.stdout)
    if result.returncode != 0:
        print(f"  ERROR: {result.stderr}")
        return False
    return True


def main():
    success = True
    
    if not run_step("COLLECT", ["collect.py"]):
        success = False
    
    if not run_step("ANALYZE", ["analyze.py"]):
        success = False
    
    spikes_json = PROJECT_DIR / "dashboard" / "spikes.json"
    if spikes_json.exists():
        data = json.loads(spikes_json.read_text())
        spikes = data.get("spikes", [])
        
        high = [s for s in spikes if s["spike_tier"] == "high"]
        med = [s for s in spikes if s["spike_tier"] == "med"]
        low = [s for s in spikes if s["spike_tier"] == "low"]
        
        print(f"\n{'='*50}")
        print(f"  REPORT: {data['date']}")
        print(f"{'='*50}")
        print(f"  🚨 Massive: {len(high)} | 📈 Breaking: {len(med)} | 👀 Notable: {len(low)}")
        
        if spikes:
            print(f"\n  Top spikes:")
            for s in spikes[:5]:
                print(f"    {s['spike_multiple']}× — {s['article']} ({s['views_fmt']}, baseline ~{s['baseline_30d']:,})")
        else:
            print(f"\n  📡 All quiet — no anomalous spikes detected.")
        
        return 0 if success else 1
    
    return 1


if __name__ == "__main__":
    sys.exit(main())
