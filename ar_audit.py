"""
Single entry point.

  ar_audit ui [--network]     browser UI (default with no command); --network serves it on this PC's LAN address with a secret key
  ar_audit daily              mask the newest 'AR Audit Result*.xlsx' in OneDrive
  ar_audit mask [file ...]    mask workbook(s)          (flags: --date, --no-presidio, --suggest)
  ar_audit doctor             check Python/libraries/folders/port/network and print what is wrong
"""
import subprocess
import sys
import os
from pathlib import Path

if getattr(sys, "frozen", False):
    BASE = Path(sys.executable).parent
else:
    BASE = Path(__file__).parent
    sys.path.insert(0, str(BASE))


def run(mod_main, argv):
    sys.argv = ["ar_audit"] + argv
    mod_main()


def main():
    cmd, rest = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ("ui", [])
    import daily_mask as dm
    if cmd == "ui":
        import webui
        webui.main(network="--network" in rest)
    elif cmd == "doctor":
        import doctor
        doctor.main()
    elif cmd == "mask":
        run(dm.main, rest)
    elif cmd == "daily":
        run(dm.main, rest)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
