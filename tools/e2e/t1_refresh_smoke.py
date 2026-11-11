"""Check moving-vehicle recovery through Chrome DevTools Protocol."""
from __future__ import annotations

import asyncio
import base64
import json
import sys
import urllib.request
from pathlib import Path

from websockets.asyncio.client import connect


async def main(port: int, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    pages = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
    page = next(p for p in pages if p.get("type") == "page" and "/sim/" in p.get("url", ""))
    counter = 0
    async with connect(page["webSocketDebuggerUrl"], max_size=8_000_000) as ws:
        async def call(method, params=None):
            nonlocal counter
            counter += 1
            await ws.send(json.dumps({"id": counter, "method": method, "params": params or {}}))
            while True:
                message = json.loads(await ws.recv())
                if message.get("id") == counter:
                    if "error" in message:
                        raise RuntimeError(message["error"])
                    return message.get("result", {})

        async def evaluate(expression):
            result = await call("Runtime.evaluate", {"expression": expression,
                                                       "awaitPromise": True,
                                                       "returnByValue": True})
            if "exceptionDetails" in result:
                raise RuntimeError(result["exceptionDetails"])
            return result["result"].get("value")

        async def screenshot(name):
            data = await call("Page.captureScreenshot", {"format": "png"})
            (output / name).write_bytes(base64.b64decode(data["data"]))

        ready = await evaluate("""(async () => {
          for (let i = 0; i < 120; i++) {
            if (window.__SIM__?.controls?.sync()?.sessionId) return true;
            await new Promise(r => setTimeout(r, 500));
          }
          return false;
        })()""")
        if not ready:
            diagnosis = await evaluate("({ready: document.readyState, title: document.title,"
                                       "body: document.body.innerText.slice(0, 1500),"
                                       "sim: !!window.__SIM__})")
            await screenshot("startup-timeout.png")
            raise RuntimeError(f"simulator sync timeout: {diagnosis}")
        await evaluate("__SIM__.simApi.resetDemo()")
        await evaluate("""(async () => {
          __SIM__.controls.setPaused(true);
          await __SIM__.scheduler.spawnEntry({kind: 'blue', vehicleId: 'refresh_probe'});
          __SIM__.controls.step(0.5);
          const vehicle = __SIM__.scheduler.vehicles.get('refresh_probe');
          if (!__SIM__.scheduler._requestZone(vehicle, 'Z1', __SIM__.scheduler.simTime))
            throw new Error('could not reserve Z1 before refresh');
          await __SIM__.simApi.reportRuntime(__SIM__.scheduler.runtimeSnapshot());
        })()""")
        before = await evaluate("({session: __SIM__.controls.sync(), dump: __SIM__.dump()})")
        origin = await evaluate("performance.timeOrigin")
        await screenshot("before-refresh.png")
        await call("Page.reload", {"ignoreCache": True})
        await asyncio.sleep(0.75)
        await evaluate("""(async () => {
          for (let i = 0; i < 80; i++) {
            if (performance.timeOrigin !== %s &&
                window.__SIM__?.dump?.()?.vehicles?.some(v => v.id === 'refresh_probe'))
              return true;
            await new Promise(r => setTimeout(r, 500));
          }
          throw new Error('vehicle recovery timeout');
        })()""" % origin)
        await evaluate("__SIM__.controls.setPaused(true)")
        after = await evaluate("({session: __SIM__.controls.sync(), dump: __SIM__.dump()})")
        await screenshot("after-refresh.png")
        prior = next(v for v in before["dump"]["vehicles"] if v["id"] == "refresh_probe")
        restored = [v for v in after["dump"]["vehicles"] if v["id"] == "refresh_probe"]
        assert len(restored) == 1, after
        assert before["session"]["sessionId"] == after["session"]["sessionId"]
        assert abs(prior["pos"]["x"] - restored[0]["pos"]["x"]) < 0.5
        assert abs(prior["pos"]["z"] - restored[0]["pos"]["z"]) < 0.5
        assert "Z1" in prior["reservedZones"]
        if "Z1" not in restored[0]["reservedZones"]:
            runtime = await evaluate("__SIM__.scheduler.runtimeSnapshot()")
            raise AssertionError({"before": prior, "after": restored[0],
                                  "runtime": runtime,
                                  "reservations": after["dump"].get("reservations")})
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/json/new?{page['url']}", method="PUT")
        second_page = json.load(urllib.request.urlopen(request))
        async with connect(second_page["webSocketDebuggerUrl"], max_size=8_000_000) as other:
            other_counter = 0

            async def other_call(method, params=None):
                nonlocal other_counter
                other_counter += 1
                await other.send(json.dumps({"id": other_counter, "method": method,
                                             "params": params or {}}))
                while True:
                    message = json.loads(await other.recv())
                    if message.get("id") == other_counter:
                        if "error" in message:
                            raise RuntimeError(message["error"])
                        return message.get("result", {})

            async def other_eval(expression):
                result = await other_call("Runtime.evaluate", {
                    "expression": expression, "awaitPromise": True,
                    "returnByValue": True})
                if "exceptionDetails" in result:
                    raise RuntimeError(result["exceptionDetails"])
                return result["result"].get("value")

            mirror = await other_eval("""(async () => {
              for (let i = 0; i < 80; i++) {
                const sim = window.__SIM__;
                if (sim?.scheduler?.vehicles?.has('remote:' +
                    __SIM__.simApi.ownerId + ':refresh_probe')) return null;
                if (sim?.scheduler?.vehicles && [...sim.scheduler.vehicles.keys()]
                    .some(id => id.endsWith(':refresh_probe'))) {
                  return {session: sim.controls.sync(), dump: sim.dump(),
                    owned: sim.scheduler.runtimeSnapshot()};
                }
                await new Promise(r => setTimeout(r, 500));
              }
              throw new Error('remote vehicle timeout');
            })()""")
            assert mirror["session"]["sessionId"] == after["session"]["sessionId"]
            assert any(v["id"].endswith(":refresh_probe") for v in mirror["dump"]["vehicles"])
            assert mirror["owned"] == []
            await other_eval("__SIM__.controls.cameraPreset('entry')")
            shot = await other_call("Page.captureScreenshot", {"format": "png"})
            (output / "second-client.png").write_bytes(base64.b64decode(shot["data"]))
            await other_call("Emulation.setDeviceMetricsOverride", {
                "width": 390, "height": 844, "deviceScaleFactor": 1,
                "mobile": True})
            await asyncio.sleep(1)
            shot = await other_call("Page.captureScreenshot", {"format": "png"})
            (output / "mobile-second-client.png").write_bytes(base64.b64decode(shot["data"]))
            await evaluate("__SIM__.simApi.resetDemo()")
            cleared = await other_eval("""(async () => {
              for (let i = 0; i < 40; i++) {
                const sim = window.__SIM__;
                if (sim?.controls?.sync()?.sessionId !== '%s' &&
                    sim?.dump?.()?.counts?.total === 0) {
                  return {session: sim.controls.sync(), dump: sim.dump()};
                }
                await new Promise(r => setTimeout(r, 500));
              }
              throw new Error('second client did not clear on reset');
            })()""" % after["session"]["sessionId"])
            shot = await other_call("Page.captureScreenshot", {"format": "png"})
            (output / "second-client-after-reset.png").write_bytes(base64.b64decode(shot["data"]))
        result = {"before": before, "after": after, "second_client": mirror,
                  "second_after_reset": cleared}
        (output / "refresh-smoke.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"before": prior, "after": restored[0],
                          "vehicle_count": after["dump"]["counts"]["total"]},
                         ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]), Path(sys.argv[2])))
