"""Capture raw SDK callback bytes from one channel/stream to a file.

Uses the VENDOR-selected backend (Dahua .dhav / Hikvision MPEG-PS). Usage:
  python scripts/capture_raw.py --channel 3 --stream main --seconds 5 --out tests/fixtures/ch3_main.dhav
"""
import argparse
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import relay  # noqa: F401,E402
from dotenv import load_dotenv
from relay.backends.base import get_backend
from relay.config import Config


def main():
    logging.basicConfig(level=logging.INFO)
    load_dotenv()
    cfg = Config.from_env(os.environ)
    backend = get_backend(cfg.vendor)

    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", type=int, required=True)
    ap.add_argument("--stream", choices=list(backend.stream_types), default="main")
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    total = 0
    f = open(args.out, "wb")

    def on_raw(data: bytes):
        nonlocal total
        f.write(data)
        total += len(data)

    client = backend.client_factory()
    client.init()
    client.login(cfg.host, cfg.port, cfg.username, cfg.password)
    client.start_realplay(args.channel, backend.stream_types[args.stream], on_raw)
    time.sleep(args.seconds)
    client.cleanup()
    f.close()
    print(f"Captured {total} bytes to {args.out}")


if __name__ == "__main__":
    main()
