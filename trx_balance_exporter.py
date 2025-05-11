#!/usr/bin/env python3
"""
TRX‑Balance‑Exporter (Prometheus & JSON API)
===========================================
**Change log – 2025‑05‑11 (v1.0.4)**
* `field=`/`fields=` on **single‑token** queries can now return a **bare float** when
  you ask for `balance` only, e.g.

```
/balances?address=<ADDR>&token_name=USDT&field=balance
# →  123.456789
```
* JSON object remains the default; bare number is opt‑in and only when the filter
  resolves to just the `balance` key.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import signal
from dataclasses import dataclass
from decimal import Decimal, getcontext
from typing import Dict, List, Optional, Set

import requests
from fastapi import FastAPI, HTTPException, Query, Response, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CollectorRegistry, Gauge, CONTENT_TYPE_LATEST, generate_latest

# ---------------------------------------------------------------------------
# Utility: Base58Check decode
# ---------------------------------------------------------------------------
_ALPHA = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_IDX = {c: i for i, c in enumerate(_ALPHA)}

def _b58decode(b58: str) -> bytes:
    num = 0
    for ch in b58:
        num = num * 58 + _IDX[ch]
    combined = num.to_bytes((num.bit_length() + 7) // 8, "big")
    pad = len(b58) - len(b58.lstrip("1"))
    return b"\x00" * pad + combined

def base58_to_hex(addr_b58: str) -> str:
    raw = _b58decode(addr_b58)
    if len(raw) != 25:
        raise ValueError("Invalid address length")
    payload, checksum = raw[:-4], raw[-4:]
    if hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4] != checksum:
        raise ValueError("Bad checksum")
    return payload.hex()

getcontext().prec = 40

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class Token:
    symbol: str
    contract: str
    decimals: int = 6

# ---------------------------------------------------------------------------
# Balance checker
# ---------------------------------------------------------------------------
class TronBalanceChecker:
    def __init__(self, api: str = "https://api.trongrid.io", timeout: int = 5):
        self.api_base = api.rstrip("/")
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({"Content-Type": "application/json"})

    def _post(self, ep: str, payload: dict) -> dict:
        try:
            r = self.s.post(f"{self.api_base}{ep}", json=payload, timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as exc:
            print(f"[ERROR] POST {ep}: {exc}")
            return {}

    @staticmethod
    def _enc(addr_b58: str) -> str:
        return base58_to_hex(addr_b58).rjust(64, "0")

    def balance_trx(self, addr: str) -> Decimal:
        data = self._post("/wallet/getaccount", {"address": addr, "visible": True})
        return Decimal(int(data.get("balance", 0))) / Decimal(1_000_000)

    def balance_trc20(self, holder: str, tok: Token) -> Decimal:
        param = self._enc(holder)
        data = self._post(
            "/wallet/triggersmartcontract",
            {
                "contract_address": tok.contract,
                "function_selector": "balanceOf(address)",
                "parameter": param,
                "owner_address": holder,
                "visible": True,
            },
        )
        raw = int(data.get("constant_result", ["0"])[0], 16) if data.get("constant_result") else 0
        return Decimal(raw) / (Decimal(10) ** tok.decimals)

    def all_balances(self, addr: str, tokens: Dict[str, Token]):
        return {
            "address": addr,
            "trx": self.balance_trx(addr),
            "trc20": {sym: self.balance_trc20(addr, t) for sym, t in tokens.items()},
        }

# ---------------------------------------------------------------------------
# ENV config
# ---------------------------------------------------------------------------
INTERVAL = int(os.getenv("FETCH_INTERVAL", "30"))
if not (addr_env := os.getenv("TRACK_ADDRESSES")):
    raise RuntimeError("TRACK_ADDRESSES env var required")
ADDRESSES: List[str] = [a.strip() for a in addr_env.split(",") if a.strip()]
TOKENS: Dict[str, Token] = {
    "USDT": Token("USDT", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", 6)
}
if tok_env := os.getenv("TRACK_TOKENS"):
    for spec in tok_env.split(","):
        if spec:
            try:
                sym, contract, dec = [p.strip() for p in spec.split("|")]
                TOKENS[sym.upper()] = Token(sym.upper(), contract, int(dec))
            except ValueError:
                print(f"[WARN] bad token spec: {spec}")

checker = TronBalanceChecker()

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------
REG = CollectorRegistry()
G_TRX = Gauge("tron_trx_balance", "TRX balance", ["address"], registry=REG)
G_TOK = Gauge("tron_token_balance", "TRC20 balance", ["address", "token"], registry=REG)

async def refresh(intv: int = INTERVAL):
    while True:
        for addr in ADDRESSES:
            try:
                bal = checker.all_balances(addr, TOKENS)
                G_TRX.labels(address=addr).set(float(bal["trx"]))
                for sym, amt in bal["trc20"].items():
                    G_TOK.labels(address=addr, token=sym).set(float(amt))
            except Exception as exc:
                print(f"[ERROR] refresh {addr}: {exc}")
        await asyncio.sleep(intv)

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="TRX Balance Exporter", version="1.0.4")

@app.on_event("startup")
async def _startup():
    asyncio.create_task(refresh())

@app.get("/balances")
async def balances(
    address: str = Query(..., description="TRON Base58 address"),
    token_name: Optional[str] = Query(None, description="Token symbol e.g. USDT"),
    fields: Optional[str] = Query(
        None,
        alias="field",  # back‑compat
        description="Comma‑separated list of keys to keep in the response",
    ),
):
    # Grab full data once
    try:
        data = checker.all_balances(address, TOKENS)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # --------------- single‑token shortcut ----------------
    if token_name:
        token_name = token_name.upper()
        if token_name == "TRX":
            payload = {"address": address, "token": "TRX", "balance": float(data["trx"])}
        elif token_name in data["trc20"]:
            payload = {"address": address, "token": token_name, "balance": float(data["trc20"][token_name])}
        else:
            raise HTTPException(status_code=404, detail="Token not tracked")

        if fields:
            requested = {f.strip().lower() for f in fields.split(',') if f.strip()}
            payload = {k: v for k, v in payload.items() if k.lower() in requested}
            if not payload:
                raise HTTPException(status_code=400, detail="No valid fields requested")
            # Special case: only 'balance' requested → return bare float
            if requested == {"balance"}:
                return PlainTextResponse(str(payload["balance"]), media_type="application/json")
        return JSONResponse(payload)

    # --------------- full payload ------------------------
    data["trx"] = str(data["trx"])
    data["trc20"] = {k: str(v) for k, v in data["trc20"].items()}

    if fields:
        wanted = {f.strip().lower() for f in fields.split(',') if f.strip()}
        allowed = {"address", "trx", "trc20"}
        bad = wanted - allowed
        if bad:
            raise HTTPException(status_code=400, detail=f"Unknown field(s): {', '.join(bad)}")
        data = {k: v for k, v in data.items() if k.lower() in wanted}
        if not data:
            raise HTTPException(status_code=400, detail="No valid fields requested")
    return JSONResponse(data)

@app.get("/metrics")
async def metrics():
    return Response(generate_latest(REG), media_type=CONTENT_TYPE_LATEST)

# ---------------------------------------------------------------------------
# Graceful shutdown helpers
# ---------------------------------------------------------------------------
stop_event = asyncio.Event()

def _signal(*_):
    stop_event.set()

signal.signal(signal.SIGINT, _signal)
signal.signal(signal.SIGTERM, _signal)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("trx_balance_exporter:app", host="0.0.0.0", port=8000)
