"""
WebSocket Endpoints for Real-Time Data Streaming

Provides WebSocket connections for live updates of Powerwall data without polling.
All routes are prefixed with /ws (configured in main.py).

Routes:
    - WS /ws/aggregate            -> Real-time aggregated data from all gateways
    - WS /ws/gateway/{gateway_id} -> Real-time data for specific gateway
    
Connection Flow:
    1. Client connects to WebSocket endpoint
    2. Server accepts connection and adds to active connections
    3. Server pushes JSON data every 1 second
    4. Connection remains open until client disconnects or error occurs
    5. Dead connections are automatically cleaned up

Data Format:
    - Aggregate endpoint: AggregateData model (all gateways combined)
    - Gateway endpoint: GatewayStatus model (single gateway data)
    
Usage Example:
    JavaScript:
        const ws = new WebSocket('ws://localhost:8580/ws/aggregate');
        ws.onmessage = (event) => {
            const data = JSON.parse(event.data);
            console.log('Battery:', data.total_battery_percent);
        };
    
    Python:
        import websockets
        async with websockets.connect('ws://localhost:8580/ws/gateway/default') as ws:
            while True:
                data = await ws.recv()
                print(json.loads(data))

Design Notes:
    - Updates push every 1 second (no client polling needed)
    - Graceful handling of client disconnects (no errors logged)
    - Automatic cleanup of broken connections
    - ConnectionManager broadcasts to all clients efficiently
    - Empty except blocks are intentional (normal disconnect flow)
"""
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import asyncio
import logging
import time

from app.core.gateway_manager import gateway_manager

router = APIRouter()
logger = logging.getLogger(__name__)


# Serialize-once caches for streamed payloads.
#
# The aggregate snapshot embeds every gateway's full GatewayStatus (vitals,
# system_status, tedapi_config, ...) and can run to hundreds of KB.  Without
# a cache, every connected client independently rebuilds and re-serializes
# an identical payload every second on the event loop — a steady CPU tax
# that competes with the poller.  All handlers run on one event loop, so a
# plain dict is safe (no locking needed).
_STREAM_CACHE_SECONDS = 1.0
_aggregate_cache: dict = {"ts": 0.0, "text": ""}
_gateway_cache: dict = {}  # gateway_id -> {"ts": float, "text": str}


def _aggregate_json() -> str:
    """Return the aggregate snapshot as JSON, rebuilt at most once per second."""
    now = time.monotonic()
    if not _aggregate_cache["text"] or now - _aggregate_cache["ts"] >= _STREAM_CACHE_SECONDS:
        _aggregate_cache["text"] = gateway_manager.get_aggregate_data().model_dump_json()
        _aggregate_cache["ts"] = now
    return _aggregate_cache["text"]


def _gateway_json(gateway_id: str) -> str:
    """Return one gateway's status as JSON, rebuilt at most once per second."""
    now = time.monotonic()
    entry = _gateway_cache.get(gateway_id)
    if entry is None or now - entry["ts"] >= _STREAM_CACHE_SECONDS:
        status = gateway_manager.get_gateway(gateway_id)
        text = status.model_dump_json() if status else '{"error": "Gateway not found"}'
        entry = {"ts": now, "text": text}
        _gateway_cache[gateway_id] = entry
    return entry["text"]


class ConnectionManager:
    """
    Manages WebSocket connections for broadcasting data.

    Maintains a list of active WebSocket connections and provides methods
    for connecting, disconnecting, and broadcasting messages to all clients.

    Automatically cleans up dead connections during broadcast to prevent
    memory leaks from clients that disconnect without proper close handshake.
    """

    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        """Accept and register a new WebSocket connection."""
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        """Remove a WebSocket connection from active list."""
        self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        """
        Broadcast message to all connected clients.

        Automatically detects and removes dead connections that fail to
        receive data. This handles cases where clients disconnect without
        sending a proper WebSocket close frame.
        """
        dead_connections = []
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception as e:
                logger.error(f"Error sending to websocket: {e}")
                dead_connections.append(connection)

        # Clean up dead connections
        for connection in dead_connections:
            if connection in self.active_connections:
                self.active_connections.remove(connection)


manager = ConnectionManager()


@router.websocket("/aggregate")
async def websocket_aggregate(websocket: WebSocket):
    """
    Stream aggregated data from all gateways to client.

    WebSocket endpoint: /ws/aggregate

    Pushes combined battery, power, and energy data from all configured
    gateways every second. Useful for dashboard displays showing total
    system capacity and performance.

    Data includes:
        - total_battery_percent: Combined battery level
        - total_battery_capacity: Total kWh capacity
        - total_site_power: Combined grid power
        - total_battery_power: Combined battery charge/discharge
        - total_load_power: Combined load consumption
    """
    await manager.connect(websocket)
    try:
        while True:
            # Send aggregate data every second (serialized once for all clients)
            await websocket.send_text(_aggregate_json())
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket error (aggregate): {type(e).__name__}: {e}")
        manager.disconnect(websocket)


@router.websocket("/gateway/{gateway_id}")
async def websocket_gateway(websocket: WebSocket, gateway_id: str):
    """
    Stream data for a specific gateway to client.

    WebSocket endpoint: /ws/gateway/{gateway_id}

    Pushes complete gateway status including vitals, aggregates, battery
    level, and online status every second. Use this for monitoring a
    specific Powerwall system in detail.

    Args:
        gateway_id: Gateway identifier (e.g., "default", "home", "cabin")

    Returns error message if gateway_id is not found or goes offline.
    """
    await manager.connect(websocket)
    try:
        while True:
            await websocket.send_text(_gateway_json(gateway_id))
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket error (gateway={gateway_id}): {type(e).__name__}: {e}")
        manager.disconnect(websocket)
