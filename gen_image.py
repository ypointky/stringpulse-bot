import sys
import os
from pathlib import Path

# Add skill dir to path so we can import stringpulse
SKILL_DIR = Path("/Users/hs/.openclaw/workspace/skills/stringpulse")
sys.path.append(str(SKILL_DIR))

import stringpulse

data = stringpulse._load_data()
racket_id = "d2d4cf16-2424-4d57-b670-dfa61003bef4"
racket = stringpulse._find_racket(data, racket_id)

if not racket or not racket.get("measurements"):
    print("Error: No measurements found")
    sys.exit(1)

# Get the latest measurement
measurement = racket["measurements"][-1]
loss = measurement.get("loss")
status = stringpulse._calc_status(loss)

img_path = stringpulse.IMAGES_DIR / f"{measurement['id']}.png"
res = stringpulse.generate_result_image(racket, measurement, status, img_path)

if res:
    print(res)
else:
    print("Error generating image (maybe matplotlib missing?)")
    sys.exit(1)
