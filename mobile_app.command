#!/bin/bash
cd "$(dirname "$0")" || exit 1
python3 mobile_app.py --host 0.0.0.0 --port 8765
