#!/usr/bin/env python3
"""
Chess Trainer - Stockfish WebSocket + HTTP Server
Serves chess-trainer.html over HTTP (port 8080) and
handles the Stockfish WebSocket (port 8765) separately.
Both share the same origin hostname so Safari on iOS is happy.

Usage:
    python3 chess_server.py [--stockfish /path/to/stockfish] \
                            [--ws-port 8765] [--http-port 8080]

Requirements:
    pip install websockets
"""

import asyncio
import subprocess
import sys
import argparse
import shutil
import socket
import mimetypes
from pathlib import Path
from http import HTTPStatus

try:
    import websockets
except ImportError:
    print("Missing dependency. Install it with:")
    print("  pip install websockets")
    sys.exit(1)


# ── Helpers ────────────────────────────────────────────────────────────────

def find_stockfish():
    candidates = [
        "stockfish",
        "/usr/games/stockfish",
        "/usr/bin/stockfish",
        "/usr/local/bin/stockfish",
        str(Path.home() / ".local/bin/stockfish"),
    ]
    for c in candidates:
        path = shutil.which(c) or (c if Path(c).exists() else None)
        if path:
            return path
    return None


def get_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "unknown"


# ── Stockfish bridge ───────────────────────────────────────────────────────

class StockfishBridge:
    def __init__(self, stockfish_path):
        self.path = stockfish_path
        self.proc = None

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(
            self.path,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        await self._send("uci")
        await self._wait_for("uciok")
        await self._send("isready")
        await self._wait_for("readyok")
        print("  Stockfish ready.")

    async def _send(self, cmd):
        self.proc.stdin.write((cmd + "\n").encode())
        await self.proc.stdin.drain()

    async def _wait_for(self, token):
        while True:
            line = (await self.proc.stdout.readline()).decode().strip()
            if token in line:
                return line

    async def stream_command(self, cmd, websocket):
        await self._send(cmd)
        while True:
            try:
                line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=10.0)
            except asyncio.TimeoutError:
                break
            line = line.decode().strip()
            if not line:
                continue
            await websocket.send(line)
            if line.startswith("bestmove"):
                break

    async def send_raw(self, cmd):
        await self._send(cmd)

    async def stop(self):
        if self.proc:
            try:
                await self._send("quit")
                await asyncio.wait_for(self.proc.wait(), timeout=2.0)
            except Exception:
                self.proc.kill()


# ── WebSocket handler ──────────────────────────────────────────────────────

async def handle_chess_client(websocket, stockfish_path):
    print(f"Chess client connected: {websocket.remote_address}")
    bridge = StockfishBridge(stockfish_path)
    try:
        await bridge.start()
        await websocket.send("engine_ready")
        async for message in websocket:
            message = message.strip()
            if not message:
                continue
            if message.startswith("go ") or message == "go":
                await bridge.stream_command(message, websocket)
            else:
                await bridge.send_raw(message)
    except Exception:
        pass
    finally:
        await bridge.stop()
        print(f"Chess client disconnected: {websocket.remote_address}")


# ── Plain HTTP server (serves chess-trainer.html) ──────────────────────────

BASE_DIR = Path(__file__).parent

async def handle_http(reader, writer):
    try:
        request_line = (await reader.readline()).decode(errors="replace").strip()
        # Drain headers
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break

        if not request_line:
            writer.close()
            return

        parts = request_line.split()
        raw_path = parts[1] if len(parts) >= 2 else "/"
        clean_path = raw_path.split("?")[0].lstrip("/") or "chess-trainer.html"
        file_path = (BASE_DIR / clean_path).resolve()

        # Safety: only serve files in BASE_DIR
        if BASE_DIR.resolve() not in file_path.parents and file_path != BASE_DIR.resolve() / clean_path:
            body = b"Forbidden"
            writer.write(
                b"HTTP/1.1 403 Forbidden\r\nContent-Length: 9\r\nConnection: close\r\n\r\nForbidden"
            )
            await writer.drain()
            writer.close()
            return

        if not file_path.exists() or not file_path.is_file():
            body = b"Not found"
            writer.write(
                b"HTTP/1.1 404 Not Found\r\nContent-Length: 9\r\nConnection: close\r\n\r\nNot found"
            )
            await writer.drain()
            writer.close()
            return

        body = file_path.read_bytes()
        mime, _ = mimetypes.guess_type(str(file_path))
        mime = mime or "application/octet-stream"
        header = (
            f"HTTP/1.1 200 OK\r\n"
            f"Content-Type: {mime}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Cache-Control: no-cache\r\n"
            f"Connection: close\r\n\r\n"
        ).encode()
        writer.write(header + body)
        await writer.drain()
    except Exception as e:
        print(f"HTTP error: {e}")
    finally:
        writer.close()


# ── Main ───────────────────────────────────────────────────────────────────

async def main(stockfish_path, ws_port, http_port):
    lan_ip = get_lan_ip()

    print(f"Using Stockfish: {stockfish_path}")
    print()
    print(f"  On this machine : http://localhost:{http_port}/chess-trainer.html")
    print(f"  On iPad / phone : http://{lan_ip}:{http_port}/chess-trainer.html")
    print()
    print("Press Ctrl+C to stop.\n")

    async def ws_handler(websocket):
        await handle_chess_client(websocket, stockfish_path)

    http_server = await asyncio.start_server(handle_http, "0.0.0.0", http_port)
    ws_server   = await websockets.serve(ws_handler, "0.0.0.0", ws_port)

    async with http_server, ws_server:
        await asyncio.Future()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Chess Trainer Server")
    parser.add_argument("--stockfish",  help="Path to stockfish binary")
    parser.add_argument("--ws-port",   type=int, default=8765, help="WebSocket port (default: 8765)")
    parser.add_argument("--http-port", type=int, default=8080, help="HTTP port (default: 8080)")
    args = parser.parse_args()

    sf_path = args.stockfish or find_stockfish()
    if not sf_path:
        print("ERROR: Could not find stockfish binary.")
        print("Specify with:  python3 chess_server.py --stockfish /path/to/stockfish")
        sys.exit(1)

    try:
        asyncio.run(main(sf_path, args.ws_port, args.http_port))
    except KeyboardInterrupt:
        print("\nServer stopped.")
