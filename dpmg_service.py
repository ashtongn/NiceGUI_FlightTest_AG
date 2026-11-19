"""DPMG radio service entry. The code is in dpmg/radio/dpmg_service.py.

Every radio container runs `python dpmg_service.py ...` from the root of the
mounted tree, so this file stays here. `import dpmg_service` gives the real
module, and `python3 -m dpmg.radio.dpmg_service` works as well.
"""
import importlib
import sys

if __name__ == "__main__":
    from dpmg.radio.dpmg_service import main
    main()
elif __name__ == "dpmg_service":
    sys.modules[__name__] = importlib.import_module("dpmg.radio.dpmg_service")
# __mp_main__ (a spawned worker re-running this file): nothing to do
