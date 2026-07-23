"""Self-check for the UIA action runtime: drive Calculator structurally.

Launches Calculator, presses 2 + 3 = via the UIA tree (no pixels, no
screenshots), reads the display control back, and asserts the result is 5 —
the whole thesis (act structurally, verify by reading) in one runnable check.
Opens a real Calculator window briefly.
"""
import subprocess
import sys
import time

sys.path.insert(0, "dashboard")
import uia


def main():
    subprocess.Popen(["calc.exe"])
    last = None
    for _ in range(40):  # calculator can take a few seconds to appear
        try:
            uia.read(title_re="Calculator", locator={"auto_id": "CalculatorResults"})
            break
        except uia.UiaError as e:
            last = e
            time.sleep(0.5)
    else:
        raise SystemExit("Calculator never became automatable: %s" % last)

    for btn in ("num2Button", "plusButton", "num3Button", "equalButton"):
        out = uia.act(title_re="Calculator", locator={"auto_id": btn}, action="invoke")
        assert out["ok"], btn

    display = uia.read(title_re="Calculator",
                       locator={"auto_id": "CalculatorResults"})
    print("display state:", display)
    assert "5" in display["name"], "expected 5 in %r" % display

    tr = uia.tree(title_re="Calculator", depth=2)
    assert tr["nodes"] > 5, "tree too small: %s" % tr["nodes"]

    from pywinauto import Desktop
    Desktop(backend="uia").window(title_re="Calculator").close()
    print("UIA_SELF_CHECK_OK")


if __name__ == "__main__":
    main()
