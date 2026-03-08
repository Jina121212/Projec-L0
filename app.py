import json
import time
from pathlib import Path
from datetime import datetime, timezone

import requests
import streamlit as st

CACHE_TTL_SECONDS = 60
HISTORY_MAX = 120
STATE_FILE = Path("state_history.json")


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def fetch_pairs(chain_id: str, token_address: str):
    url = f"https://api.dexscreener.com/tokens/v1/{chain_id}/{token_address}"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return r.json()


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


def main():
    st.title("Project L0 Dashboard")
    st.caption("L0 = liquidity-related raw data + short-term deltas + meta. No score, no label, no recommendation.")

    st.subheader("Input")

    chain_id = st.selectbox(
        "Chain",
        ["base", "ethereum", "solana"],
        index=0
    )

    token_address = st.text_input(
        "Token Address",
        value="0x833589fCD6eDb6E08f4c7C32D4f71b54bDa02913"
    )

    run_query = st.button("Fetch Data")

    if not run_query:
        st.info("체인과 토큰 주소를 입력한 뒤 Fetch Data를 누르세요.")
        st.stop()

    if not token_address.strip():
        st.warning("토큰 주소를 입력하세요.")
        st.stop()

    token_address = token_address.strip()

    start_time = time.time()

    try:
        data = fetch_pairs(chain_id, token_address)
    except Exception as e:
        st.error(f"DexScreener fetch failed: {e}")
        st.stop()

    pairs = data if isinstance(data, list) else data.get("pairs", [])
    best = pick_best_pair(pairs)

    if not best:
        st.warning("No pair found. Check chain and token address.")
        st.stop()

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
        "ts": time.time(),
        "chain": chain_id,
        "token": token_address,
        "raw_metrics": {
            "price_usd": now_price_usd,
            "liquidity_usd": now_liq_usd,
            "volume_24h_usd": now_vol_24h,
            "volume_15m_usd": now_vol_15m,
            "price_change_15m_pct": now_price_ch_15m,
        },
    }

    snap_5m = find_snapshot_approx(history, seconds_ago=5 * 60, chain_id=chain_id, token_address=token_address)
    snap_15m = find_snapshot_approx(history, seconds_ago=15 * 60, chain_id=chain_id, token_address=token_address)

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
        "chain": chain_id,
        "token": token_address,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "raw_metrics": raw_metrics,
        "behavior": behavior,
        "meta": {
            "source": "dexscreener",
            "cache_ttl_seconds": CACHE_TTL_SECONDS,
            "cache_age_seconds": 0,
            "latency_ms": latency_ms,
            "history_max": HISTORY_MAX,
        },
    }

    st.subheader("Quick View")
    st.metric("Price (USD)", f"{now_price_usd:,.8f}" if now_price_usd is not None else "N/A")
    st.metric("Liquidity (USD)", f"{now_liq_usd:,.0f}" if now_liq_usd is not None else "N/A")
    st.metric("24h Volume (USD)", f"{now_vol_24h:,.0f}" if now_vol_24h is not None else "N/A")

    st.subheader("Behavior")
    st.write(behavior)

    st.subheader("Service Output (JSON)")
    st.json(payload)

    history.append(current_snap)
    save_history(history)


if __name__ == "__main__":
    main()
