import json
import time
from pathlib import Path
from datetime import datetime, timezone

import requests
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

CACHE_TTL_SECONDS = 60
HISTORY_MAX = 120
STATE_FILE = Path("api_state_history.json")

# 메모리 캐시
TOKEN_CACHE = {}

# 레이트 리밋 설정
RATE_LIMIT_WINDOW_SECONDS = 60   # 60초
RATE_LIMIT_MAX_REQUESTS = 10     # 60초 동안 최대 10회
REQUEST_LOG = {}

app = FastAPI(
    title="Project L0 API",
    version="0.4",
    description="Liquidity-related token data feed for AI agents"
)


def safe_float(x, default=None):
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default


def pct_change(now: float, prev: float):
    if prev is None or prev == 0 or now is None:
        return None
    return (now - prev) / prev * 100.0


def load_history():
    if not STATE_FILE.exists():
        return []
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        return []
    except Exception:
        return []


def save_history(history):
    trimmed = history[-HISTORY_MAX:]
    STATE_FILE.write_text(
        json.dumps(trimmed, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def find_snapshot_approx(history, seconds_ago: int, chain_id: str, token_address: str):
    if not history:
        return None

    target_ts = time.time() - seconds_ago
    best = None
    best_diff = None
    tolerance = CACHE_TTL_SECONDS * 2 + 10

    for snap in history:
        if snap.get("chain") != chain_id:
            continue
        if snap.get("token") != token_address:
            continue

        ts = snap.get("ts")
        if ts is None:
            continue

        diff = abs(ts - target_ts)
        if diff <= tolerance:
            if best is None or diff < best_diff:
                best = snap
                best_diff = diff

    return best


def pick_best_pair(pairs):
    if not pairs:
        return None

    def liq_usd(p):
        liq = p.get("liquidity") or {}
        try:
            return float(liq.get("usd") or 0)
        except Exception:
            return 0.0

    return max(pairs, key=liq_usd)


def fetch_pairs(chain_id: str, token_address: str):
    url = f"https://api.dexscreener.com/tokens/v1/{chain_id}/{token_address}"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return r.json()


def check_rate_limit(client_id: str):
    now = time.time()

    if client_id not in REQUEST_LOG:
        REQUEST_LOG[client_id] = []

    # 현재 윈도우 내 요청만 남기기
    REQUEST_LOG[client_id] = [
        ts for ts in REQUEST_LOG[client_id]
        if now - ts < RATE_LIMIT_WINDOW_SECONDS
    ]

    # 초과 여부 확인
    if len(REQUEST_LOG[client_id]) >= RATE_LIMIT_MAX_REQUESTS:
        return False, len(REQUEST_LOG[client_id])

    # 현재 요청 기록
    REQUEST_LOG[client_id].append(now)
    return True, len(REQUEST_LOG[client_id])


@app.get("/")
def root():
    return {
        "service": "Project L0 API",
        "version": "0.4",
        "description": "Liquidity-related token data feed for AI agents",
        "endpoints": {
            "health": "/health",
            "token": "/token?chain=base&address=TOKEN_ADDRESS"
        },
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
        "rate_limit": {
            "window_seconds": RATE_LIMIT_WINDOW_SECONDS,
            "max_requests": RATE_LIMIT_MAX_REQUESTS
        }
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "Project L0 API",
        "as_of": datetime.now(timezone.utc).isoformat(),
        "cache_size": len(TOKEN_CACHE),
        "tracked_clients": len(REQUEST_LOG)
    }


@app.get("/token")
def get_token_data(
    request: Request,
    chain: str = Query(..., description="base / ethereum / solana"),
    address: str = Query(..., description="token contract address")
):
    start_time = time.time()

    # 클라이언트 식별 (지금은 IP 기반)
    client_ip = request.client.host if request.client else "unknown"

    allowed, current_count = check_rate_limit(client_ip)
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={
                "error": "rate_limit_exceeded",
                "detail": f"Too many requests. Max {RATE_LIMIT_MAX_REQUESTS} per {RATE_LIMIT_WINDOW_SECONDS} seconds.",
                "client_ip": client_ip
            }
        )

    chain = chain.strip().lower()
    address = address.strip()

    if not chain or not address:
        return JSONResponse(
            status_code=400,
            content={"error": "chain and address are required"}
        )

    cache_key = f"{chain}:{address}"
    now_ts = time.time()

    # 1) 캐시 확인
    cached = TOKEN_CACHE.get(cache_key)
    if cached:
        age = int(now_ts - cached["stored_at"])
        if age < CACHE_TTL_SECONDS:
            payload = cached["payload"].copy()
            payload["meta"] = payload.get("meta", {}).copy()
            payload["meta"]["cache_age_seconds"] = age
            payload["meta"]["served_from_cache"] = True
            payload["meta"]["request_count_in_window"] = current_count
            return payload

    # 2) 외부 API 호출
    try:
        data = fetch_pairs(chain, address)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "error": "dexscreener_fetch_failed",
                "detail": str(e)
            }
        )

    pairs = data if isinstance(data, list) else data.get("pairs", [])
    best = pick_best_pair(pairs)

    if not best:
        return JSONResponse(
            status_code=404,
            content={
                "error": "no_pair_found",
                "detail": "Check chain and token address."
            }
        )

    liq = best.get("liquidity") or {}
    vol = best.get("volume") or {}
    pc = best.get("priceChange") or {}

    now_liq_usd = safe_float(liq.get("usd"))
    now_vol_24h = safe_float(vol.get("h24"))
    now_vol_15m = safe_float(vol.get("m15"))
    now_price_ch_15m = safe_float(pc.get("m15"))
    now_price_usd = safe_float(best.get("priceUsd"))

    raw_metrics = {
        "price_usd": now_price_usd,
        "liquidity_usd": now_liq_usd,
        "volume_24h_usd": now_vol_24h,
        "volume_15m_usd": now_vol_15m,
        "fdv_usd": safe_float(best.get("fdv")),
        "pair_address": best.get("pairAddress"),
        "dex_id": best.get("dexId"),
        "pair_url": best.get("url"),
    }

    history = load_history()

    current_snap = {
        "ts": now_ts,
        "chain": chain,
        "token": address,
        "raw_metrics": {
            "price_usd": now_price_usd,
            "liquidity_usd": now_liq_usd,
            "volume_24h_usd": now_vol_24h,
            "volume_15m_usd": now_vol_15m,
            "price_change_15m_pct": now_price_ch_15m,
        },
    }

    snap_5m = find_snapshot_approx(history, seconds_ago=5 * 60, chain_id=chain, token_address=address)
    snap_15m = find_snapshot_approx(history, seconds_ago=15 * 60, chain_id=chain, token_address=address)

    liq_change_5m = None
    liq_change_15m = None
    price_change_5m = None
    price_change_15m_from_history = None

    if snap_5m:
        prev_liq_5m = safe_float(snap_5m.get("raw_metrics", {}).get("liquidity_usd"))
        prev_price_5m = safe_float(snap_5m.get("raw_metrics", {}).get("price_usd"))
        liq_change_5m = pct_change(now_liq_usd, prev_liq_5m)
        price_change_5m = pct_change(now_price_usd, prev_price_5m)

    if snap_15m:
        prev_liq_15m = safe_float(snap_15m.get("raw_metrics", {}).get("liquidity_usd"))
        prev_price_15m = safe_float(snap_15m.get("raw_metrics", {}).get("price_usd"))
        liq_change_15m = pct_change(now_liq_usd, prev_liq_15m)
        price_change_15m_from_history = pct_change(now_price_usd, prev_price_15m)

    volume_spike = None
    if now_vol_15m is not None and now_vol_24h and now_vol_24h > 0:
        avg_15m = now_vol_24h / 96.0
        if avg_15m > 0:
            volume_spike = now_vol_15m / avg_15m

    behavior = {
        "liquidity_change_5m_pct": liq_change_5m,
        "liquidity_change_15m_pct": liq_change_15m,
        "price_change_5m_pct": price_change_5m,
        "price_change_15m_pct_from_history": price_change_15m_from_history,
        "price_change_15m_pct_from_source": now_price_ch_15m,
        "volume_spike_15m_vs_24h_avg": volume_spike,
    }

    latency_ms = int((time.time() - start_time) * 1000)

    payload = {
    "service": "Project L0 API",
    "version": "0.4",
    "chain": chain,
    "token": address,
    "as_of": datetime.now(timezone.utc).isoformat(),
    "raw_metrics": raw_metrics,
    "behavior": behavior,

    # 👇 여기 추가
    "agent_ready": True,
    "data_quality": "raw_liquidity_feed",
    "use_case": "token_analysis_for_ai_agents",

    "meta": {
        ...
    }
}

    history.append(current_snap)
    save_history(history)

    # 3) 캐시에 저장
    TOKEN_CACHE[cache_key] = {
        "stored_at": now_ts,
        "payload": payload
    }

    return payload