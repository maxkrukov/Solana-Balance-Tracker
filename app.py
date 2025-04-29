import os
import sys
import time
import asyncio
import logging
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass
from enum import Enum

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from cachetools import TTLCache
from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    REGISTRY,
    CONTENT_TYPE_LATEST
)

# ======================
# Configuration
# ======================
class HealthStatus(Enum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"

@dataclass
class ApplicationConfiguration:
    CACHE_TIME_TO_LIVE_SECONDS: int = int(os.getenv("CACHE_TTL_SECONDS", "60"))
    SOLANA_RPC_ENDPOINTS: Tuple[str, ...] = (
        "https://api.mainnet-beta.solana.com",
        "https://rpc.ankr.com/solana",
        "https://solana-mainnet.g.alchemy.com/v2/demo",
        "https://solana-api.projectserum.com",
	"https://go.getblock.io/4136d34f90a6488b84214ae26f0ed5f4",
	"https://solana-rpc.publicnode.com",
	"https://api.blockeden.xyz/solana/67nCBdZQSH9z3YqDDjdm",
	"https://solana.drpc.org",
        "https://endpoints.omniatech.io/v1/sol/mainnet/public",
        "https://solana.api.onfinality.io/public"
    )
    AUTO_REFRESH_ADDRESSES: Tuple[str, ...] = tuple(
        address for address in os.getenv("AUTO_REFRESH_ADDRESSES", "").split(",") if address
    )
    TOKEN_REGISTRY_URL: str = (
        "https://raw.githubusercontent.com/solana-labs/token-list/main/src/tokens/solana.tokenlist.json"
    )
    METRICS_UPDATE_INTERVAL_SECONDS: int = 150
    RPC_HEALTH_CHECK_INTERVAL_SECONDS: int = 300
    STALE_ADDRESS_CLEANUP_INTERVAL_SECONDS: int = 600
    SERVER_HOST: str = os.getenv("HOST", "0.0.0.0")
    SERVER_PORT: int = int(os.getenv("PORT", "8000"))

class BlockchainConstants:
    LAMPORTS_PER_SOL: int = 1_000_000_000
    TOKEN_PROGRAM_ID: str = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
    SOLANA_NATIVE_MINT: str = "So11111111111111111111111111111111111111112"
    SOLANA_LOGO_URI: str = (
        "https://raw.githubusercontent.com/solana-labs/token-list/main/assets/mainnet/"
        "So11111111111111111111111111111111111111112/logo.png"
    )

# ======================
# Logging Configuration
# ======================
def configure_logging() -> logging.Logger:
    """Configure logging to stdout with structured format"""
    logger = logging.getLogger("solana-tracker")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(handler)
    
    return logger

logger = configure_logging()

# ======================
# Data Models
# ======================
class TokenMetadata(BaseModel):
    name: Optional[str]
    symbol: Optional[str]
    logo_uri: Optional[str]
    additional_metadata: Optional[Dict[str, Any]]

class TokenBalanceInfo(BaseModel):
    mint_address: str
    raw_amount: float
    decimals: int
    ui_amount: float
    metadata: TokenMetadata
    is_native: bool = False

class AddressBalanceResponse(BaseModel):
    address: str
    solana_balance: float
    tokens: List[TokenBalanceInfo]
    total_assets: int

class RpcEndpointInfo(BaseModel):
    url: str
    provider: str
    status: HealthStatus
    last_checked: Optional[float]
    response_time: Optional[float]

# ======================
# Core Services
# ======================
class ApplicationCacheManager:
    def __init__(self):
        self._balances_cache = TTLCache(
            maxsize=10000,
            ttl=ApplicationConfiguration.CACHE_TIME_TO_LIVE_SECONDS
        )
        self._token_registry_cache = {}
        self._address_access_times = {}
        self._metrics_data = {
            "startup_timestamp": time.time(),
            "last_updated": 0
        }

    async def get_balances(self, address: str) -> AddressBalanceResponse:
        """Retrieve balances from cache"""
        if address in self._balances_cache:
            logger.debug(f"Cache hit for address {address}")
            return self._balances_cache[address]
        logger.debug(f"Cache miss for address {address}")
        raise KeyError(f"Address {address} not in cache")

    async def update_balances(self, address: str, balances: AddressBalanceResponse) -> None:
        """Update cache with fresh balance data"""
        self._balances_cache[address] = balances
        self._address_access_times[address] = time.time()
        self._metrics_data["last_updated"] = time.time()
        logger.info(f"Updated cache for address {address}")

    async def get_all_cached_balances(self) -> List[AddressBalanceResponse]:
        """Retrieve all cached balances"""
        return list(self._balances_cache.values())

    async def cleanup_stale_addresses(self) -> None:
        """Remove inactive addresses from cache"""
        current_time = time.time()
        stale_addresses = [
            address for address, last_access in self._address_access_times.items()
            if current_time - last_access > ApplicationConfiguration.STALE_ADDRESS_CLEANUP_INTERVAL_SECONDS
        ]
        for address in stale_addresses:
            self._balances_cache.pop(address, None)
            self._address_access_times.pop(address, None)
            logger.info(f"Removed stale address: {address}")

    async def load_token_registry(self) -> None:
        """Load token metadata from external source"""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(ApplicationConfiguration.TOKEN_REGISTRY_URL)
                token_list = response.json()
                self._token_registry_cache = {
                    token["address"]: {
                        "name": token.get("name"),
                        "symbol": token.get("symbol"),
                        "logo_uri": token.get("logoURI")
                    }
                    for token in token_list["tokens"]
                }
                logger.info(f"Loaded token registry with {len(self._token_registry_cache)} entries")
        except Exception as error:
            logger.error(f"Failed to load token registry: {error}")

    def get_token_metadata(self, mint_address: str) -> TokenMetadata:
        """Retrieve metadata for a token"""
        cached_data = self._token_registry_cache.get(mint_address, {})
        return TokenMetadata(
            name=cached_data.get("name"),
            symbol=cached_data.get("symbol"),
            logo_uri=cached_data.get("logo_uri"),
            additional_metadata=None
        )

class SolanaRpcEndpointManager:
    def __init__(self):
        self._endpoints = []
        self._current_index = 0
        self._health_status = {}

    async def initialize(self) -> None:
        """Initialize and verify RPC endpoints"""
        await self._refresh_endpoints()
        logger.info(f"Initialized with {len(self._endpoints)} healthy endpoints")

    async def _refresh_endpoints(self) -> None:
        """Check and update endpoint health status"""
        healthy_endpoints = []
        for endpoint in ApplicationConfiguration.SOLANA_RPC_ENDPOINTS:
            is_healthy = await self._check_endpoint_health(endpoint)
            self._health_status[endpoint] = (
                HealthStatus.HEALTHY if is_healthy else HealthStatus.UNHEALTHY
            )
            if is_healthy:
                healthy_endpoints.append(endpoint)
        
        self._endpoints = healthy_endpoints
        logger.info(
            f"RPC Endpoint Status: {len(self._endpoints)}/"
            f"{len(ApplicationConfiguration.SOLANA_RPC_ENDPOINTS)} healthy"
        )

    async def _check_endpoint_health(self, endpoint_url: str) -> bool:
        """Verify if an endpoint is operational"""
        try:
            start_time = time.time()
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    endpoint_url,
                    json={"jsonrpc": "2.0", "id": 1, "method": "getHealth", "params": []}
                )
                response_time = time.time() - start_time
                is_healthy = response.status_code == 200 and response.json().get("result") == "ok"
                logger.debug(
                    f"Health check for {endpoint_url}: {'HEALTHY' if is_healthy else 'UNHEALTHY'} "
                    f"(Response time: {response_time:.3f}s)"
                )
                return is_healthy
        except Exception as error:
            logger.warning(f"Health check failed for {endpoint_url}: {error}")
            return False

    def get_next_endpoint(self) -> Optional[str]:
        """Get next available endpoint using round-robin"""
        if not self._endpoints:
            logger.error("No healthy RPC endpoints available")
            return None
        
        self._current_index = (self._current_index + 1) % len(self._endpoints)
        endpoint = self._endpoints[self._current_index]
        logger.debug(f"Selected RPC endpoint: {endpoint}")
        return endpoint

    async def make_request(self, method: str, params: list = None) -> Dict[str, Any]:
        """Execute RPC request with failover support"""
        params = params or []
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params
        }

        last_error = None
        attempts = 0
        
        while attempts < len(self._endpoints):
            endpoint = self.get_next_endpoint()
            if not endpoint:
                break

            attempts += 1
            start_time = time.time()
            
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    logger.debug(f"Making {method} request to {endpoint}")
                    response = await client.post(endpoint, json=payload)
                    response_time = time.time() - start_time
                    
                    response.raise_for_status()
                    result = response.json()

                    if "error" in result:
                        last_error = result["error"]["message"]
                        logger.warning(f"RPC error from {endpoint}: {last_error}")
                        continue

                    logger.debug(
                        f"Successful {method} request to {endpoint} "
                        f"(Response time: {response_time:.3f}s)"
                    )
                    return result

            except Exception as error:
                last_error = str(error)
                logger.warning(f"Request failed to {endpoint}: {last_error}")
                continue

        logger.error(f"All RPC attempts failed for {method}. Last error: {last_error}")
        raise HTTPException(
            status_code=502,
            detail=f"All RPC endpoints failed. Last error: {last_error}"
        )

    async def periodic_health_check(self) -> None:
        """Continuously monitor endpoint health"""
        while True:
            await self._refresh_endpoints()
            await asyncio.sleep(ApplicationConfiguration.RPC_HEALTH_CHECK_INTERVAL_SECONDS)

class SolanaBlockchainService:
    def __init__(self, rpc_manager: SolanaRpcEndpointManager, cache_manager: ApplicationCacheManager):
        self._rpc_manager = rpc_manager
        self._cache = cache_manager

    async def get_address_balances(self, address: str, force_refresh: bool = False) -> AddressBalanceResponse:
        """Get balances for an address"""
        if not force_refresh:
            try:
                return await self._cache.get_balances(address)
            except KeyError:
                logger.debug(f"No cache entry for {address}, fetching fresh data")
                pass

        logger.info(f"Fetching fresh balances for {address}")
        fresh_balances = await self._fetch_balances(address)
        await self._cache.update_balances(address, fresh_balances)
        return fresh_balances

    async def _fetch_balances(self, address: str) -> AddressBalanceResponse:
        """Fetch balance data from blockchain"""
        sol_balance = await self._get_solana_balance(address)
        token_accounts = await self._get_token_accounts(address)
        return self._build_balance_response(address, sol_balance, token_accounts)

    async def _get_solana_balance(self, address: str) -> float:
        """Retrieve native SOL balance"""
        result = await self._rpc_manager.make_request("getBalance", [address])
        return int(result["result"]["value"]) / BlockchainConstants.LAMPORTS_PER_SOL

    async def _get_token_accounts(self, address: str) -> List[Dict[str, Any]]:
        """Retrieve token accounts for an address"""
        result = await self._rpc_manager.make_request(
            "getTokenAccountsByOwner",
            [
                address,
                {"programId": BlockchainConstants.TOKEN_PROGRAM_ID},
                {"encoding": "jsonParsed"}
            ]
        )
        return result["result"]["value"]

    def _build_balance_response(
        self,
        address: str,
        sol_balance: float,
        token_accounts: List[Dict[str, Any]]
    ) -> AddressBalanceResponse:
        """Construct balance response from raw data"""
        tokens = self._build_token_list(sol_balance, token_accounts)
        return AddressBalanceResponse(
            address=address,
            solana_balance=sol_balance,
            tokens=tokens,
            total_assets=len(tokens)
        )

    def _build_token_list(
        self,
        sol_balance: float,
        token_accounts: List[Dict[str, Any]]
    ) -> List[TokenBalanceInfo]:
        """Construct list of token balances"""
        tokens = [
            TokenBalanceInfo(
                mint_address=BlockchainConstants.SOLANA_NATIVE_MINT,
                raw_amount=sol_balance * BlockchainConstants.LAMPORTS_PER_SOL,
                decimals=9,
                ui_amount=sol_balance,
                metadata=TokenMetadata(
                    name="Solana",
                    symbol="SOL",
                    logo_uri=BlockchainConstants.SOLANA_LOGO_URI
                ),
                is_native=True
            )
        ]

        for account in token_accounts:
            info = account["account"]["data"]["parsed"]["info"]
            token_amount = info["tokenAmount"]
            if token_amount["amount"] == "0":
                continue

            mint_address = info["mint"]
            tokens.append(TokenBalanceInfo(
                mint_address=mint_address,
                raw_amount=float(token_amount["amount"]),
                decimals=token_amount["decimals"],
                ui_amount=token_amount["uiAmount"],
                metadata=self._cache.get_token_metadata(mint_address)
            ))

        return tokens

class PrometheusMetricsService:
    def __init__(self, blockchain_service: SolanaBlockchainService, cache_manager: ApplicationCacheManager):
        self._blockchain_service = blockchain_service
        self._cache = cache_manager

        # Initialize metrics
        self._uptime_gauge = Gauge('solana_tracker_uptime_seconds', 'Service uptime')
        self._healthy_rpc_gauge = Gauge('solana_tracker_healthy_rpcs', 'Healthy RPC count')
        self._request_counter = Counter('solana_tracker_requests_total', 'Total requests')
        self._token_balance_gauge = Gauge(
            'solana_token_balance',
            'Token balances',
            ['address', 'mint', 'symbol', 'is_native']
        )
        self._rpc_status_gauge = Gauge(
            'solana_tracker_rpc_status',
            'RPC endpoint status',
            ['rpc_url']
        )
        self._rpc_usage_counter = Counter(
            'solana_tracker_rpc_requests_total',
            'RPC requests by endpoint',
            ['rpc_url', 'method']
        )
        self._rpc_response_time = Histogram(
            'solana_tracker_rpc_response_seconds',
            'RPC response times',
            ['rpc_url', 'method']
        )

    async def update_metrics(self, address: Optional[str] = None) -> None:
        """Update all metrics"""
        self._uptime_gauge.set(time.time() - self._cache._metrics_data["startup_timestamp"])
        self._healthy_rpc_gauge.set(len(self._blockchain_service._rpc_manager._endpoints))
        
        # Update RPC-specific metrics
        for endpoint in ApplicationConfiguration.SOLANA_RPC_ENDPOINTS:
            status = 1 if endpoint in self._blockchain_service._rpc_manager._endpoints else 0
            self._rpc_status_gauge.labels(rpc_url=endpoint).set(status)

        # Update token balances
        if address:
            balances = await self._blockchain_service.get_address_balances(address)
            self._update_token_metrics(balances)
        else:
            for balances in await self._cache.get_all_cached_balances():
                self._update_token_metrics(balances)

    def _update_token_metrics(self, balances: AddressBalanceResponse) -> None:
        """Update metrics for specific balance data"""
        for token in balances.tokens:
            self._token_balance_gauge.labels(
                address=balances.address,
                mint=token.mint_address,
                symbol=token.metadata.symbol or "unknown",
                is_native=str(token.is_native).lower()
            ).set(token.ui_amount)

    def generate_metrics(self) -> Response:
        """Generate Prometheus metrics response"""
        return Response(
            content=generate_latest(REGISTRY),
            media_type=CONTENT_TYPE_LATEST
        )

# ======================
# API Endpoints
# ======================
class ApiRequestHandler:
    def __init__(
        self,
        blockchain_service: SolanaBlockchainService,
        metrics_service: PrometheusMetricsService
    ):
        self._blockchain_service = blockchain_service
        self._metrics_service = metrics_service

    async def handle_balance_request(
        self,
        address: str,
        token_name: Optional[str] = None,
        fields: Optional[str] = None,
        refresh: bool = False
    ) -> Any:
        """Handle balance request"""
        self._metrics_service._request_counter.inc()
        logger.info(f"Balance request for {address} (refresh: {refresh})")

        balances = await self._blockchain_service.get_address_balances(address, force_refresh=refresh)

        if token_name:
            logger.debug(f"Filtering for token: {token_name}")
            return self._format_token_response(balances, token_name)
        if fields:
            logger.debug(f"Filtering fields: {fields}")
            return self._format_filtered_response(balances, fields)
        
        return balances

    def _format_token_response(self, balances: AddressBalanceResponse, token_name: str) -> PlainTextResponse:
        """Format response for specific token request"""
        token_name_upper = token_name.upper()
        matching_token = next(
            (token for token in balances.tokens
             if (token.metadata.symbol and token.metadata.symbol.upper() == token_name_upper) or
                (token.metadata.name and token.metadata.name.upper() == token_name_upper) or
                (token.mint_address == token_name)),
            None
        )
        return PlainTextResponse(content=str(matching_token.ui_amount if matching_token else 0))

    def _format_filtered_response(self, balances: AddressBalanceResponse, fields: str) -> Any:
        """Format response with only requested fields"""
        requested_fields = [field.strip() for field in fields.split(",")]
        filtered_tokens = []

        for token in balances.tokens:
            token_data = {}
            for field in requested_fields:
                if hasattr(token, field):
                    token_data[field] = getattr(token, field)
                elif hasattr(token.metadata, field):
                    token_data[field] = getattr(token.metadata, field)
            filtered_tokens.append(token_data)

        if len(filtered_tokens) == 1:
            return PlainTextResponse(content="\n".join(str(v) for v in filtered_tokens[0].values()))
        return JSONResponse(content=filtered_tokens)

    async def handle_metrics_request(self, address: Optional[str] = None) -> Response:
        """Handle metrics request"""
        logger.info(f"Metrics request for {address or 'all addresses'}")
        await self._metrics_service.update_metrics(address)
        return self._metrics_service.generate_metrics()

# ======================
# Background Tasks
# ======================
class BackgroundTaskManager:
    def __init__(
        self,
        rpc_manager: SolanaRpcEndpointManager,
        blockchain_service: SolanaBlockchainService,
        cache_manager: ApplicationCacheManager
    ):
        self._rpc_manager = rpc_manager
        self._blockchain_service = blockchain_service
        self._cache = cache_manager

    async def start_tasks(self) -> None:
        """Start all background maintenance tasks"""
        asyncio.create_task(self._refresh_auto_addresses())
        asyncio.create_task(self._rpc_manager.periodic_health_check())
        asyncio.create_task(self._cleanup_stale_addresses())
        logger.info("Background tasks started")

    async def _refresh_auto_addresses(self) -> None:
        """Periodically refresh auto-refresh addresses"""
        while True:
            if ApplicationConfiguration.AUTO_REFRESH_ADDRESSES:
                logger.info(
                    f"Refreshing {len(ApplicationConfiguration.AUTO_REFRESH_ADDRESSES)} "
                    "auto-refresh addresses"
                )
                tasks = [
                    self._blockchain_service.get_address_balances(address, force_refresh=True)
                    for address in ApplicationConfiguration.AUTO_REFRESH_ADDRESSES
                ]
                await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(ApplicationConfiguration.METRICS_UPDATE_INTERVAL_SECONDS)

    async def _cleanup_stale_addresses(self) -> None:
        """Periodically clean up stale cache entries"""
        while True:
            logger.debug("Running stale address cleanup")
            await self._cache.cleanup_stale_addresses()
            await asyncio.sleep(ApplicationConfiguration.STALE_ADDRESS_CLEANUP_INTERVAL_SECONDS)

# ======================
# Application Setup
# ======================
def create_application() -> FastAPI:
    """Initialize and configure the FastAPI application"""
    application = FastAPI(
        title="Solana Tracker API",
        description="Comprehensive Solana blockchain monitoring service",
        version="1.0.0"
    )

    # Initialize services
    cache_manager = ApplicationCacheManager()
    rpc_manager = SolanaRpcEndpointManager()
    blockchain_service = SolanaBlockchainService(rpc_manager, cache_manager)
    metrics_service = PrometheusMetricsService(blockchain_service, cache_manager)
    request_handler = ApiRequestHandler(blockchain_service, metrics_service)
    background_tasks = BackgroundTaskManager(rpc_manager, blockchain_service, cache_manager)

    # Add middleware
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Startup event
    @application.on_event("startup")
    async def startup_event():
        logger.info("="*60)
        logger.info(f"Starting Solana Tracker API v1.0")
        logger.info(f"Current time: {time.ctime()}")
        logger.info(f"Configured RPCs: {ApplicationConfiguration.SOLANA_RPC_ENDPOINTS}")
        logger.info("="*60)

        await cache_manager.load_token_registry()
        await rpc_manager.initialize()
        await background_tasks.start_tasks()
        logger.info(f"Server ready on http://{ApplicationConfiguration.SERVER_HOST}:{ApplicationConfiguration.SERVER_PORT}")

    # API endpoints
    @application.get("/balances", response_model=AddressBalanceResponse)
    async def get_balances(
        address: str = Query(..., description="Solana address to query"),
        token_name: Optional[str] = Query(None, description="Specific token to filter by"),
        fields: Optional[str] = Query(None, description="Comma-separated fields to return"),
        refresh: bool = Query(False, description="Force fresh data")
    ):
        return await request_handler.handle_balance_request(address, token_name, fields, refresh)

    @application.get("/metrics")
    async def get_metrics(address: Optional[str] = Query(None, description="Specific address to monitor")):
        return await request_handler.handle_metrics_request(address)

    return application

# ======================
# Main Application
# ======================
app = create_application()

if __name__ == "__main__":
    uvicorn.run(
        app,
        host=ApplicationConfiguration.SERVER_HOST,
        port=ApplicationConfiguration.SERVER_PORT,
        log_level="info",
        access_log=False  # Disable Uvicorn's default access logs
    )
