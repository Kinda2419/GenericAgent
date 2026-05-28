import os
import runpy
import sys
from datetime import datetime
from pathlib import Path


FRONTENDS_DIR = Path(__file__).resolve().parent
APP_DIR = FRONTENDS_DIR.parent
INSTANCE_NAME = APP_DIR.parent.name
TEMP_DIR = APP_DIR / "temp"
TEMP_DIR.mkdir(parents=True, exist_ok=True)

out_log = TEMP_DIR / f"fsapp-{INSTANCE_NAME}.out.log"
err_log = TEMP_DIR / f"fsapp-{INSTANCE_NAME}.err.log"
(TEMP_DIR / f"fsapp-{INSTANCE_NAME}.pid").write_text(str(os.getpid()), encoding="utf-8")

sys.stdout = out_log.open("a", encoding="utf-8", buffering=1)
sys.stderr = err_log.open("a", encoding="utf-8", buffering=1)

print(f"\n=== Feishu hidden start {datetime.now().isoformat(timespec='seconds')} ===", flush=True)
runpy.run_path(str(FRONTENDS_DIR / "fsapp.py"), run_name="__main__")
