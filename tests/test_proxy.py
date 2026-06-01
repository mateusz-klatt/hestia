"""End-to-end integration test: fake device ↔ hestia.proxy ↔ fake cloud.

No real hardware or cloud. Verifies the verbatim relay (both ways), the decoding
tap into State, and control-port injection — including that an injected frame
deframes back into exactly one checksum-valid frame (i.e. it never embeds the
0x7e delimiter).
"""
from __future__ import annotations

import asyncio
import json
import unittest

from hestia import proxy
from hestia.protocol import FLAG, Frame, build_frame, iter_frames, tlv

DOOR_OPEN = build_frame(
    0x1E, 0x09,
    tlv(0x0047, b"\x12") + tlv(0x0046, bytes.fromhex("7105000000ff061600")) + tlv(0x001F, b"\x00\xb4"),
)
CLOUD_CMD = build_frame(
    0x1E, 0x07,
    tlv(0x0046, b"\x25\x01\xff") + tlv(0x0048, b"\x00") + tlv(0x0047, b"\x0e") + tlv(0x001F, (1).to_bytes(4, "big")),
)
TIMEOUT = 2.0


class ProxyIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_end_to_end(self):
        cloud_recv = bytearray()

        async def fake_cloud(reader, writer):
            async def pusher():
                await asyncio.sleep(0.1)
                writer.write(CLOUD_CMD)
                await writer.drain()
            asyncio.create_task(pusher())
            try:
                while True:
                    data = await reader.read(4096)
                    if not data:
                        break
                    cloud_recv.extend(data)
            except ConnectionError:
                pass

        cloud_srv = await asyncio.start_server(fake_cloud, "127.0.0.1", 0)
        cloud_port = cloud_srv.sockets[0].getsockname()[1]

        rt = proxy.ProxyRuntime()
        config = proxy.ProxyConfig(
            listen_host="127.0.0.1", listen_port=0,
            cloud_host="127.0.0.1", cloud_port=cloud_port,
            control_host="127.0.0.1", control_port=0,
        )
        proxy_srv, control_srv = await proxy._start(rt, config)
        dev_port = proxy_srv.sockets[0].getsockname()[1]
        ctl_port = control_srv.sockets[0].getsockname()[1]

        dev_reader, dev_writer = await asyncio.open_connection("127.0.0.1", dev_port)
        dev_writer.write(DOOR_OPEN)
        await dev_writer.drain()

        got_cmd = await asyncio.wait_for(dev_reader.readexactly(len(CLOUD_CMD)), TIMEOUT)
        self.assertEqual(got_cmd, CLOUD_CMD, "cloud->device relay corrupted the frame")

        await asyncio.sleep(0.1)
        self.assertIn(DOOR_OPEN, bytes(cloud_recv), "device->cloud relay failed")
        self.assertEqual(rt.state.doors.get(0x12), "open", "tap did not update State")

        ctl_reader, ctl_writer = await asyncio.open_connection("127.0.0.1", ctl_port)
        ctl_writer.write(b'{"op": "cover", "node": 5, "value": 0}\n')
        await ctl_writer.drain()
        resp = json.loads(await asyncio.wait_for(ctl_reader.readline(), TIMEOUT))
        self.assertTrue(resp.get("ok"), f"control rejected the command: {resp}")

        injected = bytes.fromhex(resp["sent"])
        got_inject = await asyncio.wait_for(dev_reader.readexactly(len(injected)), TIMEOUT)
        self.assertEqual(got_inject, injected, "injected command not delivered")
        # The forged frame must round-trip as exactly one checksum-valid frame and
        # carry no internal 0x7e (the bug the safe-seq counter prevents).
        bodies = list(iter_frames(injected))
        self.assertEqual(len(bodies), 1, "injected frame embeds a 0x7e delimiter")
        self.assertTrue(Frame(bodies[0]).checksum_ok)
        self.assertNotIn(FLAG, injected[1:-1])

        ctl_writer.write(b'{"op": "state"}\n')
        await ctl_writer.drain()
        snap = json.loads(await asyncio.wait_for(ctl_reader.readline(), TIMEOUT))
        self.assertTrue(snap.get("ok") and snap["state"]["doors"].get("0x12") == "open", snap)

        # Teardown — close clients + servers; guard wait_closed with a timeout so a
        # lingering connection (a known asyncio 3.12 wait_closed gotcha) can't hang
        # the test. The session's own cleanup is covered by test_proxy_unit.
        for w in (dev_writer, ctl_writer):
            w.close()
        for srv in (proxy_srv, control_srv, cloud_srv):
            srv.close()
        try:
            await asyncio.wait_for(
                asyncio.gather(proxy_srv.wait_closed(), control_srv.wait_closed(),
                               cloud_srv.wait_closed(), return_exceptions=True),
                timeout=2.0,
            )
        except asyncio.TimeoutError:
            pass


if __name__ == "__main__":
    unittest.main()
