"""Capture a real Chrome simulator reset with CDP (Chrome must already be running)."""
from __future__ import annotations

import asyncio
import base64
import json
import sys
import urllib.request
from pathlib import Path

from websockets.asyncio.client import connect


async def main(port: int, output: Path) -> None:
    pages = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
    page = next(p for p in pages if p.get("type") == "page" and "/sim/" in p.get("url", ""))
    sequence = 0

    async with connect(page["webSocketDebuggerUrl"], max_size=8_000_000) as ws:
        async def call(method: str, params: dict | None = None) -> dict:
            nonlocal sequence
            sequence += 1
            await ws.send(json.dumps({"id": sequence, "method": method,
                                      "params": params or {}}))
            while True:
                response = json.loads(await ws.recv())
                if response.get("id") == sequence:
                    if "error" in response:
                        raise RuntimeError(response["error"])
                    return response.get("result", {})

        async def evaluate(expression: str):
            response = await call("Runtime.evaluate", {
                "expression": expression, "awaitPromise": True,
                "returnByValue": True})
            if "exceptionDetails" in response:
                raise RuntimeError(response["exceptionDetails"])
            return response["result"].get("value")

        async def shot(name: str) -> str:
            response = await call("Page.captureScreenshot", {"format": "png"})
            target = output / name
            target.write_bytes(base64.b64decode(response["data"]))
            return str(target)

        await evaluate("""(async () => {
          for (let i = 0; i < 80; i++) {
            if (window.__SIM__?.controls?.sync()?.sessionId) return true;
            await new Promise(r => setTimeout(r, 500));
          }
          throw new Error('simulator sync timeout');
        })()""")
        before = await evaluate("({sync: __SIM__.controls.sync(), dump: __SIM__.dump()})")
        before_image = await shot("before-reset.png")
        await evaluate("document.querySelector('#btn-play').click(); __SIM__.controls.step(2)")
        await asyncio.sleep(3)
        spawned = await evaluate("({sync: __SIM__.controls.sync(), dump: __SIM__.dump()})")
        await evaluate("document.querySelector('#btn-reset').click()")
        await evaluate("""(async () => {
          const old = '%s';
          for (let i = 0; i < 40; i++) {
            if (__SIM__.controls.sync().sessionId !== old) return true;
            await new Promise(r => setTimeout(r, 500));
          }
          throw new Error('reset did not switch session');
        })()""" % before["sync"]["sessionId"])
        after = await evaluate("({sync: __SIM__.controls.sync(), dump: __SIM__.dump()})")
        await asyncio.sleep(1)
        after_image = await shot("after-reset.png")
        await call("Page.navigate", {"url": "http://127.0.0.1:9626/admin/#/monitor"})
        await asyncio.sleep(1)
        await evaluate("""(async () => {
          for (let i = 0; i < 40; i++) {
            if (document.readyState === 'complete') return true;
            await new Promise(r => setTimeout(r, 250));
          }
          throw new Error('admin page timeout');
        })()""")
        await evaluate("""(async () => {
          const res = await fetch('/api/auth/login', {method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({username: 'admin', password: 'admin123'})});
          const data = await res.json();
          if (!res.ok) throw new Error('admin login failed');
          localStorage.setItem('parking.admin.token', data.token);
          location.reload();
          return true;
        })()""")
        await asyncio.sleep(2)
        admin = await evaluate("""(async () => {
          for (let i = 0; i < 40; i++) {
            const feed = document.querySelector('.feed');
            if (feed && feed.textContent.includes('演示复位'))
              return {history_visible: true, text: feed.textContent.slice(0, 500)};
            await new Promise(r => setTimeout(r, 500));
          }
          return {history_visible: false, text: document.body.textContent.slice(0, 500)};
        })()""")
        admin_image = await shot("admin-history.png")
        result = {"before": before, "spawned": spawned, "after": after,
                  "admin": admin,
                  "screenshots": [before_image, after_image, admin_image]}
        (output / "browser-smoke.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"before_session": before["sync"]["sessionId"],
                          "spawned_vehicles": spawned["dump"]["counts"]["total"],
                          "after_session": after["sync"]["sessionId"],
                          "after_vehicles": after["dump"]["counts"]["total"],
                          "admin_history_visible": admin["history_visible"]},
                         ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]), Path(sys.argv[2])))
