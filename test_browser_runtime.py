"""Self-check: the browser runtime drives a real (headless) Edge/Chrome over
CDP with verified readback. Uses a data: URL — no network needed."""
import sys
import urllib.request

sys.path.insert(0, "dashboard")
import browser

PAGE = ("data:text/html,<title>wisp-test</title>"
        "<input id='q'><button id='b' "
        "onclick=\"document.title='clicked-'+document.getElementById('q').value\">"
        "go</button>")


def main():
    out = browser.ensure(headless=True)
    print("ensure:", out)
    try:
        opened = browser.open_url(PAGE)
        tab = opened["id"]

        read = browser.act(tab_id=tab, action="read")
        assert read["after"]["title"] == "wisp-test", read

        fill = browser.act(tab_id=tab, action="fill", selector="#q",
                           value="uber-eats")
        assert fill["verified"], fill

        click = browser.act(tab_id=tab, action="click", selector="#b")
        assert click["verified"], click

        after = browser.act(tab_id=tab, action="eval", js="document.title")
        assert after["after"] == "clicked-uber-eats", after

        missing = browser.act(tab_id=tab, action="click", selector="#nope")
        assert missing["verified"] is False, missing  # honest about absence

        assert any(t["id"] == tab for t in browser.tabs())
        print("BROWSER_SELF_CHECK_OK")
    finally:
        # close ONLY the Wisp instance via its browser-level CDP socket —
        # never touch the user's own Edge windows
        try:
            import json as j
            import websocket
            ver = j.load(urllib.request.urlopen(browser.BASE + "/json/version",
                                                timeout=3))
            ws = websocket.create_connection(ver["webSocketDebuggerUrl"],
                                             timeout=5, suppress_origin=True)
            ws.send(j.dumps({"id": 1, "method": "Browser.close"}))
            ws.close()
        except Exception as e:
            print("cleanup note:", e)


if __name__ == "__main__":
    main()
