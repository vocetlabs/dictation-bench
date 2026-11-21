# /// script
# requires-python = ">=3.11"
# dependencies = ["websocket-client==1.8.0"]
# ///
"""Runs one JavaScript expression in every page of an Electron app started with
--remote-debugging-port=<port> (Chrome DevTools Protocol), and prints how many pages it ran in. Used by
setup scripts to write the settings an app's onboarding would write to its localStorage.

    uv run apps/cdp_eval.py <port> '<expression>'
"""
import json
import sys
import time
import urllib.request

import websocket


def main() -> int:
    port, expr = sys.argv[1], sys.argv[2]
    pages = []
    for _ in range(60):
        try:
            targets = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2).read())
            pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
            if pages:
                break
        except OSError:
            pass
        time.sleep(1)
    if not pages:
        print("no debuggable page", file=sys.stderr)
        return 1
    done = 0
    for p in pages:
        ws = websocket.create_connection(p["webSocketDebuggerUrl"], timeout=10, suppress_origin=True)
        ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": expr}}))
        reply = json.loads(ws.recv())
        ws.close()
        done += "error" not in reply and not reply.get("result", {}).get("exceptionDetails")
    print(f"ran in {done}/{len(pages)} pages")
    return 0 if done else 1


if __name__ == "__main__":
    sys.exit(main())
