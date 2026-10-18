import os
import time
import json
import random
import string
from typing import AsyncGenerator, Optional
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Enterprise Speedtest", version="1.0.0")

# Allow CORS for multi-location testing (adjust origins in production)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static frontend
app.mount("/static", StaticFiles(directory="static"), name="static")

# ---------------------------------------------------------------------------
# Configuration – edit these for your deployment
# ---------------------------------------------------------------------------
# Multiple test locations (admin-defined). Each entry is a dict with name and base_url.
# If only one, the dropdown is hidden.
TEST_LOCATIONS = [
    {"name": "Primary Datacenter", "base_url": ""},  # empty = same origin
    # Example additional location (uncomment and edit as needed):
    # {"name": "Secondary Site", "base_url": "https://speedtest-secondary.internal.example.com"},
]

# Default download size for curl convenience (100 MiB)
DEFAULT_DOWNLOAD_BYTES = 100 * 1024 * 1024

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_client_ip(request: Request) -> dict:
    """Return the connecting IP as seen by the server (handles proxies)."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        ip = forwarded.split(",")[0].strip()
    else:
        ip = request.client.host if request.client else "unknown"
    
    family = "ipv6" if ":" in ip else "ipv4"
    return {"ip": ip, "family": family}


def random_bytes_generator(size: int, chunk_size: int = 65536) -> AsyncGenerator[bytes, None]:
    """Yield random data without touching disk."""
    remaining = size
    while remaining > 0:
        this_chunk = min(chunk_size, remaining)
        yield os.urandom(this_chunk)
        remaining -= this_chunk


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------
@app.get("/")
async def root():
    """Serve the main frontend page."""
    with open("static/index.html", "r") as f:
        return HTMLResponse(content=f.read())


@app.get("/locations")
async def get_locations():
    """Return configured test locations for the frontend dropdown."""
    return {"locations": TEST_LOCATIONS}


@app.get("/ip")
async def client_ip(request: Request):
    """Report the client's connecting IPv4 or IPv6 address."""
    return get_client_ip(request)


@app.get("/ping")
async def ping(request: Request):
    """Minimal latency probe. Returns server timestamp."""
    return {
        "ts": time.time(),
        "ip": get_client_ip(request)["ip"]
    }


@app.get("/download")
async def download(request: Request, bytes: Optional[int] = None):
    """
    Streaming download endpoint.
    - For curl: curl -o /dev/null http://host/download?bytes=104857600
    - No disk I/O; pure streaming of random data.
    """
    size = bytes if bytes and bytes > 0 else DEFAULT_DOWNLOAD_BYTES
    # Cap at 2 GiB for safety
    size = min(size, 2 * 1024 * 1024 * 1024)

    headers = {
        "Content-Type": "application/octet-stream",
        "Content-Length": str(size),
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
        "X-Test-Size": str(size),
    }
    return StreamingResponse(
        random_bytes_generator(size),
        media_type="application/octet-stream",
        headers=headers,
    )


@app.post("/upload")
async def upload(request: Request):
    """
    Streaming upload endpoint.
    Reads the body in chunks, discards it, and returns size + timing.
    No disk I/O.
    """
    start = time.time()
    total = 0
    chunk_size = 65536

    async for chunk in request.stream():
        total += len(chunk)

    elapsed = time.time() - start
    return {
        "bytes": total,
        "seconds": round(elapsed, 4),
        "mbps": round((total * 8) / (elapsed * 1_000_000), 2) if elapsed > 0 else 0,
        "ip": get_client_ip(request)["ip"],
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Optional: simple config endpoint for frontend
# ---------------------------------------------------------------------------
@app.get("/config")
async def config():
    return {
        "default_download_bytes": DEFAULT_DOWNLOAD_BYTES,
        "locations": TEST_LOCATIONS,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
