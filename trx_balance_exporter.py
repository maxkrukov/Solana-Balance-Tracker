#!/usr/bin/env python3
"""
TRX‑Balance‑Exporter (Prometheus‑ready)
======================================
A production‑ready rewrite of the previous **TronBalanceChecker** that mirrors the
architecture of the *Solana‑Balance‑Tracker* repository but works on the **TRON
network (TRX)** instead.

Key features
------------
* **Prometheus metrics exporter** (`/metrics`) exposing per‑address/token gauges.
* **FastAPI** JSON API (`/balances`) identical to the earlier endpoint.
* **Self‑contained** Base58‑check decoding – no extra RPC for address ↔ hex.
* **Config via ENV** – list one or many TRON addresses & tokens to watch.
* **Background task** refreshes balances every *N* seconds (default 30).
* **Docker‑friendly** – pair with `Dockerfile` & `docker‑compose.yml` (see repo).

Quick start
-----------
```bash
export TRACK_ADDRESSES="TD76D4hM4kmfiBhK9X7BQRVCpWhM4HRmcs,TSs..."
# USDT is pre‑loaded; add more as  SYMBOL|CONTRACT|DECIMALS  comma‑separated
export TRACK_TOKENS="USDC|aMNEhse...|6"
export FETCH_INTERVAL=30
pip install fastapi "uvicorn[standard]" prometheus_client requests
uvicorn trx_balance_exporter:app --reload --port 8000
```
The exporter will scrape balances every 30 s and expose them on
`http://localhost:8000/metrics`.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import signal
from dataclasses import dataclass
from decimal import Decimal, getcontext
from typing import Dict, List, Optional

import requests
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from prometheus_client import CollectorRegistry, Gauge, CONTENT_TYPE_LATEST, generate_latest

# ---------------------------------------------------------------------------
# Utility: Base58Check decode (local, no RPC needed)
# ---------------------------------------------------------------------------
_ALPHABET: str = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_ALPHA_IDX: Dict[str, int] = {c: i for i, c in enumerate(_ALPHABET)}

def _b58decode(b58: str) -> bytes:
    num = 0
    for ch in b58:
        num = num * 58 + _ALPHA_IDX[ch]
    combined = num.to_bytes((num.bit_length() + 7) // 8, "big")
    pad = len(b58) - len(b58.lstrip("1"))
    return b"\x00" * pad + combined

def base58_to_hex(addr_b58: str) -> str:
    raw = _b58decode(addr_b58)
    if len(raw) != 25:
        raise ValueError("Invalid address length (expect 25 bytes)")
    payload, checksum = raw[:-4], raw[-4:]
    if hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4] != checksum:
        raise ValueError("Wrong Base58Check checksum")
    return payload.hex()  # starts with 41...

# High precision for Decimal balances
getcontext().prec = 40

# ---------------------------------------------------------------------------
# Dataclass for tracked TRC‑20 tokens
# ---------------------------------------------------------------------------
@dataclass
class Token:
    symbol: str
    contract: str  # Base58 contract address
    decimals: int = 6

# ---------------------------------------------------------------------------
# Core balance checker
# ---------------------------------------------------------------------------
class TronBalanceChecker:
    """Fetch TRX and TRC‑20 balances for a given TRON address."""

    def __init__(self, api: str = "https://api.trongrid.io", timeout: int = 5):
        self.api_base = api.rstrip("/")
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({"Content-Type": "application/json"})

    # ---------- helpers ----------
    def _post(self, ep: str, payload: dict) -> dict:
        try:
            r = self.s.post(f"{self.api_base}{ep}", json=payload, timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as exc:
            print(f"[ERROR] POST {ep}: {exc}")
            return {}

    @staticmethod
    def _encode_addr(addr_b58: str) -> str:
        return base58_to_hex(addr_b58).rjust(64, "0")  # 32‑byte left pad

    # ---------- public ----------
    def balance_trx(self, addr: str) -> Decimal:
        data = self._post("/wallet/getaccount", {"address": addr, "visible": True})
        return Decimal(int(data.get("balance", 0))) / Decimal(1_000_000)

    def balance_trc20(self, holder: str, token: Token) -> Decimal:
        param = self._encode_addr(holder)
        data = self._post(
            "/wallet/triggersmartcontract",
            {
                "contract_address": token.contract,
                "function_selector": "balanceOf(address)",
                "parameter": param,
                "owner_address": holder,
                "visible": True,
            },
        )
        raw = int(data.get("constant_result", ["0"])[0], 16) if data.get("constant_result") else 0
        return Decimal(raw) / (Decimal(10) ** token.decimals)

    def all_balances(self, addr: str, tokens: Dict[str, Token]):
        return {
            "trx": self.balance_trx(addr),
            "trc20": {sym: self.balance_trc20(addr, tok) for sym, tok in tokens.items()},
        }

# ---------------------------------------------------------------------------
# Config via ENV
# ---------------------------------------------------------------------------
DEF_INTERVAL = int(os.getenv("FETCH_INTERVAL", "30"))
ADDRESS_ENV = os.getenv("TRACK_ADDRESSES", "")
TOKEN_ENV = os.getenv("TRACK_TOKENS", "")

if not ADDRESS_ENV:
    raise RuntimeError("TRACK_ADDRESSES env var is required (comma‑separated TRON addresses)")

ADDRESSES: List[str] = [a.strip() for a in ADDRESS_ENV.split(",") if a.strip()]

TOKENS: Dict[str, Token] = {
    "USDT": Token("USDT", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", 6)
}
if TOKEN_ENV:
    for tok_def in TOKEN_ENV.split(","):
        if not tok_def:
            continue
        try:
            sym, contract, dec = [x.strip() for x in tok_def.split("|")]
            TOKENS[sym.upper()] = Token(sym.upper(), contract, int(dec))
        except ValueError:
            print(f"[WARN] Bad token definition: '{tok_def}' (expect SYMBOL|CONTRACT|DECIMALS)")

# ---------------------------------------------------------------------------
# Prometheus metrics setup
# ---------------------------------------------------------------------------
REG = CollectorRegistry()
GAUGE_TRX = Gauge("tron_trx_balance", "TRX balance", ["address"], registry=REG)
GAUGE_TOKEN = Gauge("tron_token_balance", "TRC20 token balance", ["address", "token"], registry=REG)

checker = TronBalanceChecker()

async def update_loop(interval: int = DEF_INTERVAL):
    while True:
        for addr in ADDRESSES:
            try:
                data = checker.all_balances(addr, TOKENS)
                GAUGE_TRX.labels(address=addr).set(float(data["trx"]))
                for sym, amt in data["trc20"].items():
                    GAUGE_TOKEN.labels(address=addr, token=sym).set(float(amt))
            except Exception as exc:  # noqa: BLE001
                print(f"[ERROR] refresh {addr}: {exc}")
        await asyncio.sleep(interval)

# ---------------------------------------------------------------------------
# FastAPI app with /balances and /metrics
# ---------------------------------------------------------------------------
app = FastAPI(title="TRX Balance Exporter", version="1.0.0")

@app.on_event("startup")
async def _startup():
    # Kick‑off background updater
    asyncio.create_task(update_loop())

@app.get("/balances")
async def api_balances(
    address: str = Query(..., description="TRON Base58 address"),
    token_name: Optional[str] = Query(None, description="Token symbol, e.g., USDT"),
):
    try:
        bal = checker.all_balances(address, TOKENS)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if token_name:
        t = token_name.upper()
        if t == "TRX":
            return {"address": address, "token": "TRX", "balance": str(bal["trx"]) }
        if t in bal["trc20"]:
            return {"address": address, "token": t, "balance": str(bal["trc20"][t]) }
        raise HTTPException(status_code=404, detail="Token not tracked")

    return JSONResponse(
        {
            "address": address,
            "trx": str(bal["trx"]),
            "trc20": {k: str(v) for k, v in bal["trc20"].items()},
        }
    )

@app.get("/metrics")
async def metrics():
    return Response(generate_latest(REG), media_type=CONTENT_TYPE_LATEST)

# ---------------------------------------------------------------------------
# Graceful shutdown (Ctrl‑C)
# ---------------------------------------------------------------------------
stop_event = asyncio.Event()

def _handle_sig(*_):
    stop_event.set()

signal.signal(signal.SIGINT, _handle_sig)
signal.signal(signal.SIGTERM, _handle_sig)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("trx_balance_exporter:app", host="0.0.0.0", port=8000, reload=False)
