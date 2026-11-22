from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "replay-1.0"
MAX_BYTES = 300 * 1024
SAMPLES = {"nightly": "华东夜班回填", "clean": "两分区演练"}
ADOPTION_VERSION = "adopt-1.0"
ADOPTION_SAMPLES = {"arrivals": "夜班双源到达", "arrivals_clean": "双分区演练"}
