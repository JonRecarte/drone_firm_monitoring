#!/usr/bin/env python3
"""Shared Renode monitor (-P) TCP client with telnet handshake."""
from __future__ import annotations

import re
import socket
import time

TELNET_HANDSHAKE = bytes([255, 251, 0, 255, 253, 1, 255, 253, 3])
PROMPT_RE = re.compile(r"\((?:monitor|CF2\.1)\)")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def clean_text(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace").replace("\x00", "")
    return ANSI_RE.sub("", text)


class RenodeClient:
    def __init__(
        self,
        port: int,
        machine: str = "CF2.1",
        connect_timeout: float = 60.0,
        cmd_timeout: float = 5.0,
    ) -> None:
        self.machine = machine
        self.cmd_timeout = cmd_timeout
        try:
            self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        except OSError as exc:
            raise ConnectionError(
                f"Cannot connect to localhost:{port} ({exc}).\n"
                "Start Renode first:\n"
                "  RENODE_MONITOR_PORT=9999 DOCKER=1 ./scripts/run_first_test.sh"
            ) from exc

        self.sock.settimeout(0.2)
        self.buf = b""
        self.text = ""
        self.sock.sendall(TELNET_HANDSHAKE)
        self._wait_for_prompt(connect_timeout)
        self.query(f'mach set "{machine}"')

    def _read_available(self) -> None:
        while True:
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                return
            if not chunk:
                raise ConnectionError(
                    "Renode closed the monitor connection. "
                    "Only one client may own -P; closing it stops Renode."
                )
            self.buf += chunk
            self.text = clean_text(self.buf)

    def _wait_for_prompt(self, deadline_s: float) -> None:
        end = time.time() + deadline_s
        while time.time() < end:
            self._read_available()
            if PROMPT_RE.search(self.text):
                return
        raise TimeoutError("Timed out waiting for Renode monitor prompt")

    def _tail_since(self, marker: int) -> str:
        return clean_text(self.buf[marker:])

    def query(self, cmd: str, expect_prompt: bool = True) -> str:
        marker = len(self.buf)
        self.sock.sendall(f"{cmd}\n".encode("utf-8"))
        end = time.time() + self.cmd_timeout
        while time.time() < end:
            self._read_available()
            chunk = self._tail_since(marker)
            if expect_prompt and not PROMPT_RE.search(chunk):
                continue
            lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
            body: list[str] = []
            for ln in lines:
                if ln == cmd or ln.startswith("("):
                    continue
                if "Renode" in ln or "Monitor available" in ln:
                    continue
                if ln.startswith(("Phase ", "Feed ", "Machine started", "Monitor tips")):
                    continue
                body.append(ln)
            if body:
                return "\n".join(body)
            if expect_prompt and PROMPT_RE.search(chunk):
                return ""
        return ""

    def run(self, cmd: str) -> None:
        """Fire a command and wait for the next prompt (ignore output)."""
        self.query(cmd, expect_prompt=True)

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
