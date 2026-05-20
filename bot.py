#!/usr/bin/env python3
"""
AI Crypto Hunter v4
─────────────────────────────────────────────
Ключові покращення vs v3:
  • Пріоритетна черга: CRITICAL → обходить всі ліміти
  • Retry + exponential backoff на всіх API
  • Rate limit handler (CoinGecko 429 → пауза)
  • Helius API замість мертвого Solscan
  • Фільтр застарілих даних (> 2г = skip)
  • Ротація стилів Gemini (5 голосів)
  • Health check — Telegram алерт якщо бот мовчить > 6г
  • Тижневий дайджест (субота 10:00 UTC)
  • "Портфель китів" — що накопичують великі гравці
"""

import os, io, json, time, logging, hashlib, heapq, threading, re
import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

def md_to_html(text: str) -> str:
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
    text = re.sub(r'`(.+?)`', r'<code>\1</code>', text)
    return text
# ══════════════════════════════════════════════════════════════════════════════
#  КОНФІГ
# ══════════════════════════════════════════════════════════════════════════════

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHANNEL = os.getenv("TELEGRAM_CHANNEL")
GEMINI_API_KEY   = os.getenv("GEMINI_API_KEY")
ETHERSCAN_KEY    = os.getenv("ETHERSCAN_KEY", "")
HELIUS_KEY       = os.getenv("HELIUS_KEY", "")          # helius.dev — безкоштовно

# ── Пороги CRITICAL (обходять всі ліміти) ─────────────────────────────────
CRITICAL_WHALE_USD   = 2_000_000   # кит > $2M
CRITICAL_NEWS_WORDS  = [           # слова в заголовку = CRITICAL новина
    "hack", "exploit", "hacked", "drained", "stolen", "rug",
    "sec charges", "sec lawsuit", "etf approved", "etf rejected",
    "halving", "fed rate", "blackrock", "fidelity files",
    "exchange down", "exchange offline", "suspended withdrawals",
    "злом", "хакер", "заморозили", "зупинили виведення",
]

# ── Пороги HIGH (чекають 30 хв замість повного кулдауну) ──────────────────
HIGH_WHALE_USD   = 500_000
HIGH_TOKEN_GAIN  = 80              # +80% за 1г
HIGH_TOKEN_VOL   = 2_000_000

# ── Пороги NORMAL ─────────────────────────────────────────────────────────
TOKEN_MIN_VOLUME    = 500_000
TOKEN_MIN_LIQUIDITY = 100_000
TOKEN_MIN_GAIN      = 30
TOKEN_MAX_AGE_DAYS  = 5
WHALE_ETH_MIN_USD   = 500_000
WHALE_SOL_MIN_USD   = 200_000

# ── Кулдауни ──────────────────────────────────────────────────────────────
COOLDOWN = {
    "token":   3 * 3600,
    "whale":   1 * 3600,
    "news":    4 * 3600,
    "weekly":  7 * 24 * 3600,
    "high":    1800,             # HIGH події — 30 хв
    "critical_daily_max": 3,    # CRITICAL макс. 3 рази/день
    "critical_repeat_h":  2,    # CRITICAL не повторюється 2г
}
MAX_POSTS_DAY = 15              # загальний ліміт (CRITICAL не рахується)
DATA_MAX_AGE_S = 7200           # дані старші 2г ігноруємо

STATE_FILE = "state.json"

# ── Retry ──────────────────────────────────────────────────────────────────
RETRY_ATTEMPTS  = 4
RETRY_BASE_WAIT = 2             # секунд, подвоюється кожен раз

# ══════════════════════════════════════════════════════════════════════════════
#  ВІДОМІ АДРЕСИ ETH
# ══════════════════════════════════════════════════════════════════════════════

KNOWN_ETH = {
    "0xd8da6bf26964af9d7eed9e03e53415d37aa96045": "Vitalik Buterin",
    "0x47ac0fb4f2d84898e4d9e7b4dab3c24507a6d503": "Binance Cold",
    "0xbe0eb53f46cd790cd13851d5eff43d12404d33e8": "Binance Hot",
    "0x28c6c06298d514db089934071355e5743bf21d60": "Binance 14",
    "0xf977814e90da44bfa03b6295a0616a897441acec": "Binance 8",
    "0x21a31ee1afc51d94c2efccaa2092ad1028285549": "Binance 15",
    "0x71660c4005ba85c37ccec55d0c4493e66fe775d3": "Coinbase",
    "0xa9d1e08c7793af67e9d92fe308d5697fb81d3e43": "Coinbase 2",
    "0x77696bb39917c91a0c3908d577d5e322095425ca": "Coinbase 3",
    "0x267be1c1d684f78cb4f6a176c4911b741e4ffdc0": "Kraken",
    "0x0a869d79a7052c7f1b55a8ebabbea3420f0d1e13": "Kraken 2",
    "0x6cc5f688a315f3dc28a7781717a9a798a59fda7b": "OKX",
    "0x236f9f97e0e62388479bf9e5ba4889e46b0273c3": "OKX 2",
    "0xf89d7b9c864f589bbf53a82105107622b35eaa4": "Bybit Hot",
    "0x742d35cc6634c0532925a3b844bc454e4438f44e": "Bitfinex",
    "0x876eabf441b2ee5b5b0554fd502a8e0600950cfa": "Bitfinex 2",
    "0x756d64dc5edb56740fc617628dc832ddbcfd373c": "Jump Trading",
    "0x53d284357ec70ce289d6d64134dfac8e511c8a3d": "Cumberland DRW",
    "0x00000000ae347930bd1e7b0f35588b92280f9e75": "Wintermute",
    "0x55fe002aeff02f77364de339a1292923a15844b8": "Circle/USDC Treasury",
    "0x5754284f345afc66a98fbb0a0afe71e0f007b949": "Tether Treasury",
    "0x1a9c8182c09f50c8318d769245bea52c32be35bc": "Uniswap Foundation",
    "0xde0b295669a9fd93d5f28d9ec85e40f4cb697bae": "Ethereum Foundation",
}

KNOWN_SOL = {
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM": "Binance SOL",
    "5tzFkiKscXHK5ZXCGbXZxdw7gE9pjuZKN9WGPGfZXzDm": "FTX Cold (old)",
    "H6ARHf6YXhGYeQfUzQNGk6rDNnLBQKrenN712K4AQJEG": "Solana Foundation",
    "GThUX1Atko4tqhN2NaiTazWSeFWMuiUvfFnyJyUghFMJ":  "Jump Crypto SOL",
}

# ══════════════════════════════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
#  ПРІОРИТЕТИ
# ══════════════════════════════════════════════════════════════════════════════

PRIO_CRITICAL = 0
PRIO_HIGH     = 1
PRIO_NORMAL   = 2

class Event:
    """Подія в черзі з пріоритетом"""
    __slots__ = ("priority", "ts", "kind", "data")

    def __init__(self, priority: int, kind: str, data: dict):
        self.priority = priority
        self.ts       = time.time()
        self.kind     = kind     # "token" | "whale" | "news" | "weekly"
        self.data     = data

    def __lt__(self, other):
        if self.priority != other.priority:
            return self.priority < other.priority
        return self.ts < other.ts      # FIFO в межах одного пріоритету


class PriorityQueue:
    def __init__(self):
        self._heap = []
        self._lock = threading.Lock()

    def push(self, event: Event):
        with self._lock:
            heapq.heappush(self._heap, event)

    def pop(self) -> Event | None:
        with self._lock:
            return heapq.heappop(self._heap) if self._heap else None

    def __len__(self):
        return len(self._heap)


# ══════════════════════════════════════════════════════════════════════════════
#  STATE
# ══════════════════════════════════════════════════════════════════════════════

DEFAULT_STATE = {
    "seen_tokens":        [],
    "seen_news":          [],
    "seen_whale_txs":     [],
    "last_token_post":    0,
    "last_whale_post":    0,
    "last_news_post":     0,
    "last_weekly_post":   0,
    "last_high_post":     0,
    "last_critical_post": 0,
    "critical_today":     0,
    "critical_today_date":"",
    "posts_today":        0,
    "posts_today_date":   "",
    "last_post_ts":       0,          # для health check
    "whale_portfolio":    {},         # addr -> {symbol, total_usd, count}
}

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                s = json.load(f)
            for k, v in DEFAULT_STATE.items():
                if k not in s:
                    s[k] = v
            return s
        except Exception:
            pass
    return DEFAULT_STATE.copy()

def save_state(s: dict):
    s["seen_tokens"]    = s["seen_tokens"][-500:]
    s["seen_news"]      = s["seen_news"][-200:]
    s["seen_whale_txs"] = s["seen_whale_txs"][-500:]
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2)

def _reset_daily(s: dict):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if s["posts_today_date"] != today:
        s["posts_today"]    = 0
        s["posts_today_date"] = today
    if s["critical_today_date"] != today:
        s["critical_today"] = 0
        s["critical_today_date"] = today

def can_post_normal(s: dict, kind: str) -> tuple[bool, str]:
    """Перевіряє чи можна публікувати NORMAL/HIGH подію"""
    _reset_daily(s)
    if s["posts_today"] >= MAX_POSTS_DAY:
        return False, f"daily limit {MAX_POSTS_DAY}"
    key_map = {"token": "last_token_post", "whale": "last_whale_post",
                "news": "last_news_post",  "weekly": "last_weekly_post",
                "high": "last_high_post"}
    key = key_map.get(kind, "last_token_post")
    cd  = COOLDOWN.get(kind, COOLDOWN["token"])
    elapsed = time.time() - s[key]
    if elapsed < cd:
        return False, f"cooldown {kind}: {(cd-elapsed)/60:.0f}m left"
    return True, "ok"

def can_post_critical(s: dict) -> tuple[bool, str]:
    """CRITICAL: обходить добовий ліміт, але має власні обмеження"""
    _reset_daily(s)
    if s["critical_today"] >= COOLDOWN["critical_daily_max"]:
        return False, f"critical daily max ({COOLDOWN['critical_daily_max']}) reached"
    elapsed = time.time() - s["last_critical_post"]
    if elapsed < COOLDOWN["critical_repeat_h"] * 3600:
        return False, f"critical repeat: {(COOLDOWN['critical_repeat_h']*3600-elapsed)/60:.0f}m left"
    return True, "ok"

def mark_posted(s: dict, kind: str, is_critical=False, is_high=False):
    now = time.time()
    s["last_post_ts"] = now
    if is_critical:
        s["last_critical_post"] = now
        s["critical_today"]     = s.get("critical_today", 0) + 1
    else:
        s["posts_today"] += 1
        key_map = {"token": "last_token_post", "whale": "last_whale_post",
                   "news": "last_news_post",   "weekly": "last_weekly_post"}
        if is_high:
            s["last_high_post"] = now
        else:
            k = key_map.get(kind)
            if k:
                s[k] = now


# ══════════════════════════════════════════════════════════════════════════════
#  HTTP з RETRY + RATE LIMIT
# ══════════════════════════════════════════════════════════════════════════════

_rate_limit_until: dict[str, float] = {}  # host -> timestamp

def http_get(url: str, params: dict = None, headers: dict = None,
             timeout: int = 12) -> requests.Response | None:
    """
    GET з exponential backoff.
    Якщо 429 — чекаємо Retry-After або 60 секунд.
    """
    import urllib.parse
    host = urllib.parse.urlparse(url).netloc

    # Якщо хост в rate-limit паузі — чекаємо
    rl_until = _rate_limit_until.get(host, 0)
    if time.time() < rl_until:
        wait = rl_until - time.time()
        log.info(f"Rate limit pause for {host}: {wait:.0f}s")
        time.sleep(wait)

    wait = RETRY_BASE_WAIT
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            r = requests.get(url, params=params, headers=headers,
                             timeout=timeout)
            if r.status_code == 429:
                retry_after = int(r.headers.get("Retry-After", 60))
                log.warning(f"429 from {host}, waiting {retry_after}s")
                _rate_limit_until[host] = time.time() + retry_after
                time.sleep(retry_after)
                continue
            if r.status_code == 200:
                return r
            log.warning(f"HTTP {r.status_code} from {url} (attempt {attempt})")
        except requests.RequestException as e:
            log.warning(f"Request error {url} attempt {attempt}: {e}")

        if attempt < RETRY_ATTEMPTS:
            time.sleep(wait)
            wait *= 2   # exponential backoff: 2, 4, 8 сек

    log.error(f"All {RETRY_ATTEMPTS} attempts failed: {url}")
    return None


def http_post(url: str, json_body: dict, headers: dict = None,
              timeout: int = 30) -> requests.Response | None:
    wait = RETRY_BASE_WAIT
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            r = requests.post(url, json=json_body, headers=headers, timeout=timeout)
            if r.status_code in (200, 201):
                return r
            if r.status_code == 429:
                time.sleep(60)
                continue
            log.warning(f"POST {r.status_code} attempt {attempt}: {url}")
        except requests.RequestException as e:
            log.warning(f"POST error attempt {attempt}: {e}")
        if attempt < RETRY_ATTEMPTS:
            time.sleep(wait)
            wait *= 2
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  ЦІНИ (кеш)
# ══════════════════════════════════════════════════════════════════════════════

_price_cache: dict[str, dict] = {}

def _get_price(coin_id: str) -> float:
    c = _price_cache.get(coin_id, {})
    if time.time() - c.get("ts", 0) < 300:
        return c["price"]
    try:
        r = http_get("https://api.coingecko.com/api/v3/simple/price",
                     params={"ids": coin_id, "vs_currencies": "usd"})
        if r:
            price = r.json()[coin_id]["usd"]
            _price_cache[coin_id] = {"price": price, "ts": time.time()}
            return price
    except Exception:
        pass
    return c.get("price", 0)

def get_eth_price() -> float:
    if ETHERSCAN_KEY:
        try:
            r = http_get("https://api.etherscan.io/api",
                         params={"module": "stats", "action": "ethprice",
                                 "apikey": ETHERSCAN_KEY})
            if r:
                return float(r.json()["result"]["ethusd"])
        except Exception:
            pass
    return _get_price("ethereum")

def get_sol_price() -> float:
    return _get_price("solana") or 150.0


# ══════════════════════════════════════════════════════════════════════════════
#  ДЖЕРЕЛА — ТОКЕНИ
# ══════════════════════════════════════════════════════════════════════════════

def _classify_token(token: dict) -> int:
    """Визначає пріоритет токену"""
    if token.get("price_chg_1h", 0) >= HIGH_TOKEN_GAIN and \
       token.get("volume_24h", 0) >= HIGH_TOKEN_VOL:
        return PRIO_HIGH
    return PRIO_NORMAL

def fetch_dexscreener() -> list[dict]:
    results = []
    SEARCH_QUERIES = ["meme", "pepe", "dog", "cat", "ai", "trump"]
    chains = ["solana", "base", "ethereum", "bsc"]
    for chain in chains:
      for query in SEARCH_QUERIES:
        r = http_get(
            f"https://api.dexscreener.com/latest/dex/search",
            params={"q": query, "chainId": chain}
        )
        if not r:
            continue
        for p in (r.json().get("pairs") or []):
            try:
                liq = float(p.get("liquidity", {}).get("usd", 0) or 0)
                vol = float(p.get("volume", {}).get("h24", 0) or 0)
                chg = float(p.get("priceChange", {}).get("h24", 0) or 0)
                created = p.get("pairCreatedAt")
                if created and (time.time() - created/1000)/86400 > TOKEN_MAX_AGE_DAYS:
                    continue
                # Перевіряємо актуальність даних
                if created and (time.time() - created/1000) < 60:
                    continue  # щойно створена — ще не стабільна
                if liq < TOKEN_MIN_LIQUIDITY or vol < TOKEN_MIN_VOLUME or chg < TOKEN_MIN_GAIN:
                    continue
                buys  = p.get("txns", {}).get("h24", {}).get("buys", 0)
                sells = p.get("txns", {}).get("h24", {}).get("sells", 0)
                results.append({
                    "source":        "DexScreener",
                    "name":          p.get("baseToken", {}).get("name", "???"),
                    "symbol":        p.get("baseToken", {}).get("symbol", "???"),
                    "address":       p.get("baseToken", {}).get("address", ""),
                    "chain":         chain.upper(),
                    "price_usd":     float(p.get("priceUsd", 0) or 0),
                    "price_chg_24h": chg,
                    "price_chg_1h":  float(p.get("priceChange", {}).get("h1", 0) or 0),
                    "price_chg_6h":  float(p.get("priceChange", {}).get("h6", 0) or 0),
                    "volume_24h":    vol,
                    "liquidity":     liq,
                    "market_cap":    float(p.get("marketCap") or p.get("fdv") or 0),
                    "txns_buys":     buys,
                    "txns_sells":    sells,
                    "buy_pressure":  round(buys / max(sells, 1), 2),
                    "dex_url":       p.get("url", ""),
                    "pair_addr":     p.get("pairAddress", ""),
                    "fetched_ts":    time.time(),
                })
            except (ValueError, TypeError):
                continue
    results.sort(key=lambda x: x["volume_24h"], reverse=True)
    return results

def fetch_coingecko_gainers() -> list[dict]:
    r = http_get(
        "https://api.coingecko.com/api/v3/coins/markets",
        params={"vs_currency": "usd", "order": "price_change_percentage_24h_desc",
                "per_page": 15, "page": 1, "price_change_percentage": "24h",
                "market_cap_max": 300_000_000}
    )
    if not r:
        return []
    out = []
    for c in r.json():
        chg = c.get("price_change_percentage_24h") or 0
        vol = c.get("total_volume") or 0
        if chg < TOKEN_MIN_GAIN or vol < TOKEN_MIN_VOLUME:
            continue
        out.append({
            "source": "CoinGecko", "name": c.get("name","???"),
            "symbol": c.get("symbol","???").upper(),
            "address": c.get("id",""), "chain": "MULTI",
            "price_usd": c.get("current_price",0),
            "price_chg_24h": chg, "price_chg_1h": 0, "price_chg_6h": 0,
            "volume_24h": vol, "liquidity": 0,
            "market_cap": c.get("market_cap",0),
            "txns_buys": 0, "txns_sells": 0, "buy_pressure": 0,
            "dex_url": f"https://www.coingecko.com/en/coins/{c.get('id','')}",
            "pair_addr": "", "fetched_ts": time.time(),
        })
    return out[:4]


# ══════════════════════════════════════════════════════════════════════════════
#  ДЖЕРЕЛА — КИТИ ETH (Etherscan)
# ══════════════════════════════════════════════════════════════════════════════

def _etherscan(params: dict) -> list:
    if not ETHERSCAN_KEY:
        return []
    params["apikey"] = ETHERSCAN_KEY
    r = http_get("https://api.etherscan.io/api", params=params)
    if not r:
        return []
    data = r.json()
    if data.get("status") == "1" and isinstance(data.get("result"), list):
        return data["result"]
    return []

def _eth_label(addr: str) -> str:
    return KNOWN_ETH.get(addr.lower(), addr[:6] + "…" + addr[-4:])

def _classify_whale(value_usd: float) -> int:
    if value_usd >= CRITICAL_WHALE_USD:
        return PRIO_CRITICAL
    if value_usd >= HIGH_WHALE_USD:
        return PRIO_HIGH
    return PRIO_NORMAL

def fetch_eth_whales() -> list[dict]:
    if not ETHERSCAN_KEY:
        return []

    eth_price = get_eth_price()
    whales    = []

    # Поточний блок
    try:
        r = http_get("https://api.etherscan.io/api",
                     params={"module": "proxy", "action": "eth_blockNumber",
                             "apikey": ETHERSCAN_KEY})
        current_block = int(r.json().get("result", "0x0"), 16) if r else 0
        from_block    = max(0, current_block - 300)
    except Exception:
        from_block = 0

    # Internal ETH transfers
    txs = _etherscan({"module": "account", "action": "txlistinternal",
                      "startblock": from_block, "sort": "desc", "offset": 50})
    for tx in txs:
        try:
            val_eth = int(tx.get("value", 0)) / 1e18
            val_usd = val_eth * eth_price
            if val_usd < WHALE_ETH_MIN_USD:
                continue
            fa, ta = tx.get("from",""), tx.get("to","")
            whales.append({
                "chain": "ETH", "tx_hash": tx.get("hash",""),
                "from_addr": fa, "to_addr": ta,
                "from_label": _eth_label(fa), "to_label": _eth_label(ta),
                "value_usd": val_usd, "native_amount": round(val_eth, 3),
                "symbol": "ETH", "move_type": "ETH transfer",
                "explorer_url": f"https://etherscan.io/tx/{tx.get('hash','')}",
                "fetched_ts": time.time(),
            })
        except (ValueError, TypeError):
            continue

    # Стейблкоїни USDT / USDC / DAI
    for contract, symbol, decimals in [
        ("0xdac17f958d2ee523a2206206994597c13d831ec7", "USDT",  6),
        ("0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "USDC",  6),
        ("0x6b175474e89094c44da98b954eedeac495271d0f", "DAI",  18),
    ]:
        txs = _etherscan({"module": "account", "action": "tokentx",
                          "contractaddress": contract,
                          "page": 1, "offset": 30, "sort": "desc"})
        for tx in txs:
            try:
                val_usd = int(tx.get("value", 0)) / (10 ** decimals)
                if val_usd < WHALE_ETH_MIN_USD:
                    continue
                h = tx.get("hash","")
                if any(w["tx_hash"] == h for w in whales):
                    continue
                fa, ta = tx.get("from",""), tx.get("to","")
                whales.append({
                    "chain": "ETH", "tx_hash": h,
                    "from_addr": fa, "to_addr": ta,
                    "from_label": _eth_label(fa), "to_label": _eth_label(ta),
                    "value_usd": val_usd, "native_amount": round(val_usd, 0),
                    "symbol": symbol, "move_type": f"{symbol} transfer",
                    "explorer_url": f"https://etherscan.io/tx/{h}",
                    "fetched_ts": time.time(),
                })
            except (ValueError, TypeError):
                continue

    # WETH
    txs = _etherscan({"module": "account", "action": "tokentx",
                      "contractaddress": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
                      "page": 1, "offset": 20, "sort": "desc"})
    for tx in txs:
        try:
            val_eth = int(tx.get("value",0)) / 1e18
            val_usd = val_eth * eth_price
            if val_usd < WHALE_ETH_MIN_USD:
                continue
            h = tx.get("hash","")
            if any(w["tx_hash"] == h for w in whales):
                continue
            fa, ta = tx.get("from",""), tx.get("to","")
            whales.append({
                "chain": "ETH", "tx_hash": h,
                "from_addr": fa, "to_addr": ta,
                "from_label": _eth_label(fa), "to_label": _eth_label(ta),
                "value_usd": val_usd, "native_amount": round(val_eth, 3),
                "symbol": "WETH", "move_type": "WETH transfer",
                "explorer_url": f"https://etherscan.io/tx/{h}",
                "fetched_ts": time.time(),
            })
        except (ValueError, TypeError):
            continue

    whales.sort(key=lambda x: x["value_usd"], reverse=True)
    return whales


# ══════════════════════════════════════════════════════════════════════════════
#  ДЖЕРЕЛА — КИТИ SOL (Helius)
# ══════════════════════════════════════════════════════════════════════════════

def fetch_sol_whales() -> list[dict]:
    """
    Helius API — великі SOL та SPL-токен переміщення.
    Безкоштовний план: 100k кредитів/місяць.
    Реєстрація: helius.dev
    """
    if not HELIUS_KEY:
        log.debug("Helius key not set — skipping SOL whales")
        return []

    sol_price = get_sol_price()
    whales    = []
    base_url  = f"https://api.helius.xyz/v0"

    # Великі SOL транзакції через Helius Enhanced Transactions API
    try:
        r = http_post(
            f"{base_url}/transactions/?api-key={HELIUS_KEY}",
            json_body={
                "query": {
                    "types": ["TRANSFER"],
                    "nativeTransfers": {"minimumAmount": int(WHALE_SOL_MIN_USD / sol_price * 1e9)}
                },
                "options": {"limit": 20}
            }
        )
        if r:
            for tx in r.json():
                try:
                    for transfer in tx.get("nativeTransfers", []):
                        lamports  = transfer.get("amount", 0)
                        sol_amt   = lamports / 1e9
                        usd_val   = sol_amt * sol_price
                        if usd_val < WHALE_SOL_MIN_USD:
                            continue
                        fa  = transfer.get("fromUserAccount", "")
                        ta  = transfer.get("toUserAccount", "")
                        sig = tx.get("signature", "")
                        fl  = KNOWN_SOL.get(fa, fa[:6]+"…"+fa[-4:] if fa else "???")
                        tl  = KNOWN_SOL.get(ta, ta[:6]+"…"+ta[-4:] if ta else "???")
                        whales.append({
                            "chain": "SOL", "tx_hash": sig,
                            "from_addr": fa, "to_addr": ta,
                            "from_label": fl, "to_label": tl,
                            "value_usd": usd_val,
                            "native_amount": round(sol_amt, 2),
                            "symbol": "SOL", "move_type": "SOL transfer",
                            "explorer_url": f"https://solscan.io/tx/{sig}",
                            "fetched_ts": time.time(),
                        })
                except (ValueError, TypeError, KeyError):
                    continue
    except Exception as e:
        log.warning(f"Helius native transfers: {e}")

    # SPL-токен великі переміщення (мемкоїни)
    try:
        r = http_post(
            f"{base_url}/transactions/?api-key={HELIUS_KEY}",
            json_body={
                "query": {"types": ["TRANSFER"]},
                "options": {"limit": 30}
            }
        )
        if r:
            for tx in r.json():
                for transfer in tx.get("tokenTransfers", []):
                    try:
                        amount    = float(transfer.get("tokenAmount", 0))
                        symbol    = transfer.get("symbol", "???")
                        mint      = transfer.get("mint", "")
                        fa        = transfer.get("fromUserAccount", "")
                        ta        = transfer.get("toUserAccount", "")
                        sig       = tx.get("signature", "")

                        # Перевіряємо чи це вже є в списку
                        if any(w["tx_hash"] == sig for w in whales):
                            continue
                        # Для SPL без ціни — фільтр по мінімальній кількості
                        if amount < 500_000:
                            continue

                        fl = KNOWN_SOL.get(fa, fa[:6]+"…"+fa[-4:] if fa else "???")
                        tl = KNOWN_SOL.get(ta, ta[:6]+"…"+ta[-4:] if ta else "???")
                        whales.append({
                            "chain": "SOL", "tx_hash": sig,
                            "from_addr": fa, "to_addr": ta,
                            "from_label": fl, "to_label": tl,
                            "value_usd": 0,
                            "native_amount": amount,
                            "symbol": symbol or mint[:8],
                            "move_type": "SPL token transfer",
                            "explorer_url": f"https://solscan.io/tx/{sig}",
                            "fetched_ts": time.time(),
                        })
                    except (ValueError, TypeError):
                        continue
    except Exception as e:
        log.warning(f"Helius SPL transfers: {e}")

    whales.sort(key=lambda x: x["value_usd"], reverse=True)
    log.info(f"Helius: {len(whales)} SOL whale events")
    return whales


# ══════════════════════════════════════════════════════════════════════════════
#  ПОРТФЕЛЬ КИТІВ (накопичувальна статистика)
# ══════════════════════════════════════════════════════════════════════════════

def update_whale_portfolio(state: dict, whale: dict):
    """Зберігаємо статистику по китах для тижневого звіту"""
    addr = whale.get("from_addr") or whale.get("to_addr", "")
    if not addr or whale["value_usd"] < HIGH_WHALE_USD:
        return
    p = state.setdefault("whale_portfolio", {})
    if addr not in p:
        p[addr] = {"label": whale.get("from_label", addr[:8]),
                   "total_usd": 0, "count": 0, "symbols": {}}
    p[addr]["total_usd"] += whale["value_usd"]
    p[addr]["count"]     += 1
    sym = whale.get("symbol", "???")
    p[addr]["symbols"][sym] = p[addr]["symbols"].get(sym, 0) + whale["value_usd"]


# ══════════════════════════════════════════════════════════════════════════════
#  ДЖЕРЕЛА — НОВИНИ
# ══════════════════════════════════════════════════════════════════════════════

def _is_critical_news(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in CRITICAL_NEWS_WORDS)

def _parse_rss(url: str, name: str, limit: int = 6) -> list[dict]:
    import xml.etree.ElementTree as ET
    r = http_get(url, headers={"User-Agent": "Mozilla/5.0"})
    if not r:
        return []
    try:
        root  = ET.fromstring(r.content)
        items = root.findall(".//item")
        out   = []
        for item in items[:limit]:
            title = (item.findtext("title") or "").strip()
            link  = (item.findtext("link") or "").strip()
            if title and link:
                out.append({"title": title, "url": link, "source": name,
                            "critical": _is_critical_news(title),
                            "fetched_ts": time.time()})
        return out
    except Exception as e:
        log.debug(f"RSS {name}: {e}")
        return []

def fetch_news() -> list[dict]:
    RSS = [
        ("https://cointelegraph.com/rss",               "CoinTelegraph"),
        ("https://coindesk.com/arc/outboundfeeds/rss/", "CoinDesk"),
        ("https://decrypt.co/feed",                     "Decrypt"),
        ("https://thedefiant.io/feed",                  "The Defiant"),
        ("https://cryptobriefing.com/feed/",            "Crypto Briefing"),
    ]
    news = []
    for url, name in RSS:
        news.extend(_parse_rss(url, name, limit=5))

    # CoinGecko news як доповнення
    r = http_get("https://api.coingecko.com/api/v3/news")
    if r:
        try:
            for item in r.json().get("data", [])[:8]:
                title = (item.get("title") or "").strip()
                if title:
                    news.append({"title": title, "url": item.get("url",""),
                                 "source": "CoinGecko",
                                 "critical": _is_critical_news(title),
                                 "fetched_ts": time.time()})
        except Exception:
            pass

    # Дедуплікація
    seen, unique = set(), []
    for n in news:
        k = n["title"][:60].lower()
        if k not in seen:
            seen.add(k)
            unique.append(n)

    # Критичні вперед
    unique.sort(key=lambda x: x["critical"], reverse=True)
    return unique[:8]

def fetch_market() -> dict:
    r = http_get("https://api.coingecko.com/api/v3/global")
    if not r:
        return {}
    d = r.json().get("data", {})
    return {
        "total_market_cap": d.get("total_market_cap", {}).get("usd", 0),
        "total_volume":     d.get("total_volume", {}).get("usd", 0),
        "btc_dominance":    d.get("market_cap_percentage", {}).get("btc", 0),
        "eth_dominance":    d.get("market_cap_percentage", {}).get("eth", 0),
        "market_cap_chg":   d.get("market_cap_change_percentage_24h_usd", 0),
        "fetched_ts":       time.time(),
    }

def fetch_fear_greed() -> dict:
    r = http_get("https://api.alternative.me/fng/?limit=1")
    if not r:
        return {"value": 50, "label": "Neutral"}
    d = r.json().get("data", [{}])[0]
    return {"value": int(d.get("value", 50)),
            "label": d.get("value_classification", "Neutral")}


# ══════════════════════════════════════════════════════════════════════════════
#  ГРАФІКИ
# ══════════════════════════════════════════════════════════════════════════════

CS = {"bg": "#111111", "grid": "#1E1E1E", "text": "#AAAAAA",
      "up": "#00E676", "down": "#FF1744", "accent": "#FFD700"}

def _price_history(token: dict) -> list[tuple]:
    # DexScreener
    if token.get("pair_addr") and token["chain"] != "MULTI":
        chain_map = {"SOLANA":"solana","BASE":"base","ETHEREUM":"ethereum","BSC":"bsc"}
        c  = chain_map.get(token["chain"], token["chain"].lower())
        r  = http_get(f"https://api.dexscreener.com/latest/dex/pairs/{c}/{token['pair_addr']}")
        if r:
            p = (r.json().get("pairs") or [None])[0]
            if p:
                now   = datetime.now(timezone.utc)
                price = float(p.get("priceUsd", 0) or 0)
                c1h   = float(p.get("priceChange", {}).get("h1", 0) or 0)
                c6h   = float(p.get("priceChange", {}).get("h6", 0) or 0)
                c24h  = float(p.get("priceChange", {}).get("h24", 0) or 0)
                if price > 0:
                    return [
                        (now - timedelta(hours=24), price/(1+c24h/100) if c24h else price),
                        (now - timedelta(hours=6),  price/(1+c6h/100)  if c6h  else price),
                        (now - timedelta(hours=1),  price/(1+c1h/100)  if c1h  else price),
                        (now, price),
                    ]
    # CoinGecko
    r = http_get(
        f"https://api.coingecko.com/api/v3/coins/{token['address']}/market_chart",
        params={"vs_currency": "usd", "days": 1}
    )
    if r:
        pts = r.json().get("prices", [])
        return [(datetime.fromtimestamp(ts/1000, tz=timezone.utc), p)
                for ts, p in pts[::6]]
    return []

def make_token_chart(token: dict, history: list[tuple]) -> bytes | None:
    if len(history) < 2:
        return None
    try:
        dates, prices = [h[0] for h in history], [h[1] for h in history]
        is_up  = prices[-1] >= prices[0]
        color  = CS["up"] if is_up else CS["down"]
        bg     = CS["bg"]

        fig, ax = plt.subplots(figsize=(8, 3.8), facecolor=bg)
        ax.set_facecolor(bg)
        ax.fill_between(dates, prices, min(prices)*0.98, alpha=0.12, color=color)
        ax.plot(dates, prices, color=color, linewidth=2.2, zorder=5)
        ax.scatter([dates[-1]], [prices[-1]], color=color, s=55, zorder=10,
                   edgecolors="white", linewidths=0.5)
        ax.annotate(
            f"${prices[-1]:.6f}" if prices[-1] < 0.01 else f"${prices[-1]:,.4f}",
            xy=(dates[-1], prices[-1]), xytext=(-60, 12), textcoords="offset points",
            color="white", fontsize=9, fontweight="bold",
            bbox=dict(boxstyle="round,pad=0.3", facecolor="#222222",
                      edgecolor=color, linewidth=1),
        )
        for sp in ax.spines.values(): sp.set_visible(False)
        ax.tick_params(colors=CS["text"], labelsize=7.5)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        ax.yaxis.set_major_formatter(
            plt.FuncFormatter(lambda x, _: f"${x:.6f}" if x < 0.01 else f"${x:,.4f}"))
        ax.grid(axis="y", color=CS["grid"], linewidth=0.6, linestyle="--")
        sign = "+" if is_up else ""
        chg1, chg24 = token.get("price_chg_1h", 0), token.get("price_chg_24h", 0)
        ax.set_title(
            f"${token['symbol']}  •  {sign}{chg24:.1f}% (24h)  "
            f"{'▲' if chg1>=0 else '▼'}{abs(chg1):.1f}% (1h)  "
            f"Vol: ${token['volume_24h']:,.0f}",
            color="white", fontsize=10, fontweight="bold", pad=8)
        fig.text(0.99, 0.02, "AI Crypto Hunter", color="#2A2A2A",
                 fontsize=7.5, ha="right", va="bottom", fontstyle="italic")
        plt.tight_layout(pad=1.2)
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=140, bbox_inches="tight",
                    facecolor=bg, edgecolor="none")
        plt.close(fig)
        buf.seek(0)
        return buf.read()
    except Exception as e:
        log.error(f"Token chart: {e}")
        return None

def make_market_chart(market: dict, fg: dict) -> bytes | None:
    try:
        bg = CS["bg"]
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(9, 3.5), facecolor=bg)
        ax1.set_facecolor(bg); ax2.set_facecolor(bg)
        val = fg["value"]
        gc  = ("#FF1744" if val<=20 else "#FF6D00" if val<=40 else
               "#FFD700" if val<=60 else "#76FF03" if val<=80 else "#00E676")
        ax1.pie([val, 100-val], colors=[gc, "#1E1E1E"],
                wedgeprops={"width":0.38}, startangle=90, counterclock=False)
        ax1.text(0,  0.05, str(val),    ha="center", va="center",
                 color="white", fontsize=30, fontweight="bold")
        ax1.text(0, -0.28, fg["label"], ha="center", va="center",
                 color=gc, fontsize=9, fontweight="bold")
        ax1.set_title("Fear & Greed", color=CS["text"], fontsize=9, pad=4)
        for sp in ax1.spines.values(): sp.set_visible(False)

        btc_d = market.get("btc_dominance", 0)
        eth_d = market.get("eth_dominance", 0)
        alt_d = max(0, 100 - btc_d - eth_d)
        bars  = ax2.bar(["BTC","ETH","ALTs"], [btc_d, eth_d, alt_d],
                        color=["#F7931A","#627EEA","#00E676"], width=0.45, zorder=3)
        for bar, v in zip(bars, [btc_d, eth_d, alt_d]):
            ax2.text(bar.get_x()+bar.get_width()/2, bar.get_height()+0.4,
                     f"{v:.1f}%", ha="center", color="white",
                     fontsize=9, fontweight="bold")
        ax2.set_ylim(0, max(btc_d, eth_d, alt_d)*1.22)
        ax2.set_title("Market Dominance", color=CS["text"], fontsize=9, pad=4)
        for sp in ax2.spines.values(): sp.set_visible(False)
        ax2.tick_params(colors=CS["text"], labelsize=8); ax2.set_yticks([])
        ax2.grid(axis="y", color=CS["grid"], linewidth=0.5, linestyle="--", zorder=0)

        plt.tight_layout(pad=1.5)
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=140, bbox_inches="tight",
                    facecolor=bg, edgecolor="none")
        plt.close(fig)
        buf.seek(0)
        return buf.read()
    except Exception as e:
        log.error(f"Market chart: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  GEMINI — 5 СТИЛІВ РОТАЦІЇ
# ══════════════════════════════════════════════════════════════════════════════

STYLES = [
    # 0 — Холодний аналітик (базовий)
    """Ти — автор крипто-каналу. Стиль: холодний, цифри вперед, коротко.
Пишеш на суміші рос/укр/крипто-сленгу. Без "Привіт!", без дисклеймерів.
Довжина: 150–220 слів. HTML теги: <b>, <i>, <code>.""",

    # 1 — Детектив ринку
    """Ти — крипто-детектив який розслідує рухи капіталу на блокчейні.
Кожен пост — це розслідування. Є підозрювані (адреси), є докази (цифри), є висновок.
Стиль: напружений, як детективна розповідь. Без пафосу.
Довжина: 160–230 слів. HTML теги: <b>, <i>, <code>.""",

    # 2 — Циніk-трейдер
    """Ти — досвідчений трейдер з 10 роками в крипті. Все вже бачив.
Пишеш з іронією, але без образ. Знаєш коли памп, знаєш коли хайп.
Не кричиш. Говориш як людина яка не здивується нічому.
Довжина: 140–200 слів. HTML теги: <b>, <i>.""",

    # 3 — Алерт-режим (для HIGH/CRITICAL)
    """Ти — система моніторингу яка видає алерт про важливу подію.
Стиль: чіткий, без зайвих слів, максимально інформативний.
Факти → що це означає → що робити (спостерігати / не панікувати / etc).
Довжина: 120–180 слів. HTML теги: <b>, <code>.""",

    # 4 — Оповідач (для тижневих підсумків)
    """Ти — автор тижневої колонки про крипту.
Стиль: розповідний, але без зайвої лірики. Як досвідчений журналіст.
Підсумовуєш тиждень так, щоб читач зрозумів загальну картину.
Довжина: 250–350 слів. HTML теги: <b>, <i>.""",
]

_style_counter = 0

def _pick_style(override: int | None = None) -> str:
    global _style_counter
    if override is not None:
        return STYLES[override]
    s = STYLES[_style_counter % (len(STYLES) - 1)]  # не чіпаємо стиль 4 (weekly)
    _style_counter += 1
    return s

def gemini(prompt: str, style_idx: int | None = None) -> str | None:
    url  = (f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}")
    body = {
        "system_instruction": {"parts": [{"text": _pick_style(style_idx)}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.88, "maxOutputTokens": 700},
    }
    r = http_post(url, body)
    if not r:
        return None
    try:
        parts = r.json()["candidates"][0]["content"]["parts"]
        return md_to_html("".join(p.get("text","") for p in parts).strip())
    except (KeyError, IndexError) as e:
        log.error(f"Gemini parse: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  ГЕНЕРАЦІЯ ПОСТІВ
# ══════════════════════════════════════════════════════════════════════════════

def gen_token_post(token: dict, priority: int) -> str | None:
    style = 3 if priority == PRIO_HIGH else None
    bp    = token.get("buy_pressure", 0)
    prompt = f"""Напиши пост про мемкоїн.

{token['name']} (${token['symbol']}) | {token['chain']} | {token['source']}
Ціна: ${token['price_usd']}
Динаміка: +{token['price_chg_24h']:.1f}% (24h) / +{token['price_chg_6h']:.1f}% (6h) / +{token['price_chg_1h']:.1f}% (1h)
Об'єм: ${token['volume_24h']:,.0f} | Ліквідність: ${token['liquidity']:,.0f}
MCap: ${token['market_cap']:,.0f} | Buy pressure: {bp:.1f}x
Транзакцій: {token.get('txns_buys',0)+token.get('txns_sells',0):,}
→ {token['dex_url']}

Зверни увагу на співвідношення об'єму до mcap і buy pressure.
{'⚡ УВАГА: ріст +'+str(round(token.get("price_chg_1h",0),1))+'% за 1г — це HIGH PRIORITY сигнал.' if priority == PRIO_HIGH else ''}
Чи це органічний рух чи виглядає як маніпуляція?"""
    return gemini(prompt, style)


def gen_whale_post(whale: dict, priority: int) -> str | None:
    style  = 3 if priority in (PRIO_CRITICAL, PRIO_HIGH) else None
    val    = whale.get("value_usd", 0)
    native = whale.get("native_amount", 0)
    sym    = whale.get("symbol", "???")
    val_str = f"${val:,.0f} ({native:,} {sym})" if val > 0 else f"{native:,} {sym}"

    fl, tl = whale.get("from_label","???"), whale.get("to_label","???")
    EXCHANGES = ["binance","coinbase","kraken","okx","bybit","huobi","bitfinex"]
    if any(x in tl.lower() for x in EXCHANGES):
        ctx = "Кошти йдуть НА біржу — потенційний sell pressure."
    elif any(x in fl.lower() for x in EXCHANGES):
        ctx = "Кошти виходять З біржі — можливе накопичення або cold wallet."
    else:
        ctx = "Переміщення між приватними гаманцями — OTC, cold storage, або підготовка до угоди."

    critical_note = ""
    if priority == PRIO_CRITICAL:
        critical_note = f"\n🔴 CRITICAL: сума ${val:,.0f} перевищує $2M — це рух великого гравця."

    prompt = f"""Напиши пост про переміщення крипти (whale move).{critical_note}

Мережа: {whale['chain']} | Тип: {whale.get('move_type','transfer')}
Сума: {val_str}
Від: {fl} → Куди: {tl}
Контекст: {ctx}
Транзакція: {whale['explorer_url']}

Дай інтерпретацію — не переказуй дані механічно.
Що це може означати для ринку?"""
    return gemini(prompt, style)


def gen_news_post(news: list[dict], market: dict, fg: dict,
                  is_critical=False) -> str | None:
    style = 3 if is_critical else None
    block = "\n".join(f"• {n['title']} ({n['source']})" for n in news[:5])
    mcap  = market.get("total_market_cap", 0)
    vol   = market.get("total_volume", 0)
    chg   = market.get("market_cap_chg", 0)
    btc   = market.get("btc_dominance", 0)
    now   = datetime.now(timezone.utc).strftime("%d.%m %H:%M UTC")

    crit_note = "\n⚠️ ТЕРМІНОВА НОВИНА — публікується поза чергою." if is_critical else ""

    prompt = f"""Напиши дайджест ринку.{crit_note}

Час: {now}
MCap: ${mcap/1e9:.1f}B ({'+' if chg>=0 else ''}{chg:.1f}%)
Volume 24h: ${vol/1e9:.1f}B | BTC Dom: {btc:.1f}% | F&G: {fg['value']}/100 {fg['label']}

Новини:
{block}

Структура: загальна оцінка (1 речення) → що важливо з новин → що це означає для альтів/мемів."""
    return gemini(prompt, style)


def gen_weekly_digest(state: dict, market: dict, fg: dict) -> str | None:
    """Тижневий дайджест + топ китів"""
    portfolio = state.get("whale_portfolio", {})
    top_whales = sorted(portfolio.items(),
                        key=lambda x: x[1].get("total_usd", 0), reverse=True)[:5]

    whale_block = ""
    for addr, info in top_whales:
        syms = ", ".join(f"{s}: ${v:,.0f}" for s, v in
                         sorted(info["symbols"].items(), key=lambda x:-x[1])[:3])
        whale_block += f"• {info['label']}: ${info['total_usd']:,.0f} ({info['count']} txns) — {syms}\n"

    if not whale_block:
        whale_block = "— даних за тиждень недостатньо —"

    mcap = market.get("total_market_cap", 0)
    btc  = market.get("btc_dominance", 0)
    week = datetime.now(timezone.utc).strftime("тиждень %d.%m")

    prompt = f"""Напиши тижневий підсумок крипто-ринку ({week}).

Ринок зараз:
MCap: ${mcap/1e9:.1f}B | BTC Dom: {btc:.1f}% | F&G: {fg['value']}/100 {fg['label']}

Топ рухи китів за тиждень:
{whale_block}

Структура посту:
1. Загальна картина тижня (2-3 речення)
2. Що робили кити (виходячи з даних вище)
3. На що звертати увагу наступного тижня
Це тижнева рубрика — пиши як досвідчений аналітик."""
    return gemini(prompt, style_idx=4)  # стиль "оповідач"


# ══════════════════════════════════════════════════════════════════════════════
#  ХЕШТЕГИ
# ══════════════════════════════════════════════════════════════════════════════

def build_hashtags(event: Event) -> str:
    tags = set()
    d    = event.data
    kind = event.kind

    chain = d.get("chain","").upper()
    if "SOL"      in chain: tags.add("#SOL")
    if "ETH"      in chain: tags.add("#ETH")
    if "BASE"     in chain: tags.add("#BASE")
    if "BSC"      in chain: tags.add("#BSC")
    if "MULTI"    in chain: tags.update(["#крипта","#альт"])

    if kind == "token":
        tags.add("#мемкоїн")
        sym = d.get("symbol","")
        if sym: tags.add(f"#{sym.upper()}")
        if d.get("price_chg_24h",0) > 50:   tags.add("#памп")
        if d.get("price_chg_1h",0)  > 30:   tags.add("#швидкийрух")
        if d.get("buy_pressure",0)   > 2.0:  tags.add("#buyzone")
        if d.get("volume_24h",0)     > 1_000_000: tags.add("#highvol")
        if event.priority == PRIO_HIGH:      tags.add("#сигнал")

    elif kind == "whale":
        tags.add("#кит")
        tags.add("#smartmoney")
        if "SOL" in chain:                    tags.add("#SOLкит")
        if "ETH" in chain:                    tags.add("#ETHкит")
        if d.get("value_usd",0) > 2_000_000: tags.add("#мегакит")
        if d.get("value_usd",0) > 500_000:   tags.add("#bigmove")
        move = d.get("move_type","")
        if "USDT" in move or "USDC" in move:  tags.add("#stablecoin")
        if event.priority == PRIO_CRITICAL:   tags.add("#🔴critical")

    elif kind in ("news", "weekly"):
        tags.update(["#ринок","#крипта"])
        if kind == "weekly":                  tags.add("#тижневийдайджест")
        else:                                 tags.add("#новини")
        if d.get("critical"):                 tags.add("#терміново")
        fg = d.get("fear_greed", 50)
        if fg < 30:                           tags.add("#страх")
        if fg > 70:                           tags.add("#жадність")

    return " ".join(sorted(tags))


# ══════════════════════════════════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════

def tg_text(text: str) -> bool:
    r = http_post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        {"chat_id": TELEGRAM_CHANNEL, "text": text,
         "parse_mode": "HTML", "disable_web_page_preview": True}
    )
    return r is not None and r.status_code == 200

def tg_photo(img: bytes, caption: str) -> bool:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
            data={"chat_id": TELEGRAM_CHANNEL, "parse_mode": "HTML",
                  "disable_web_page_preview": "true"},
            files={"photo": ("chart.png", img, "image/png")},
            params={"caption": caption[:1024]},
            timeout=20
        )
        return r.status_code == 200
    except Exception as e:
        log.error(f"TG photo: {e}")
        return False

def publish(text: str, hashtags: str, image: bytes | None = None) -> bool:
    full = f"{text}\n\n{hashtags}"
    ok   = tg_photo(image, full) if image else tg_text(full)
    if ok:
        log.info(f"✅ Published ({len(full)} chars)")
    return ok


# ══════════════════════════════════════════════════════════════════════════════
#  HEALTH CHECK
# ══════════════════════════════════════════════════════════════════════════════

HEALTH_SILENCE_H = 6   # якщо бот мовчить > 6г — алерт в Telegram

def health_check(state: dict):
    if not state.get("last_post_ts"):
        return
    silent_h = (time.time() - state["last_post_ts"]) / 3600
    if silent_h > HEALTH_SILENCE_H:
        msg = (f"⚠️ <b>Health Alert</b>\n"
               f"Бот мовчить вже {silent_h:.1f}г.\n"
               f"Можливі причини: API ліміти, помилки джерел, відсутність значимих подій.\n"
               f"Перевір логи.")
        tg_text(msg)
        log.warning(f"Health alert: {silent_h:.1f}h silence")


# ══════════════════════════════════════════════════════════════════════════════
#  ЗБІР ПОДІЙ → ЧЕРГА
# ══════════════════════════════════════════════════════════════════════════════

def collect_tokens(state: dict, queue: PriorityQueue):
    tokens = []
    tokens.extend(fetch_dexscreener())
    tokens.extend(fetch_coingecko_gainers())

    seen_sym = set()
    for t in tokens:
        uid = f"{t['chain']}:{t['address'] or t['symbol']}"
        if uid in state["seen_tokens"]:
            continue
        key = f"{t['chain']}:{t['symbol']}"
        if key in seen_sym:
            continue
        # Фільтр застарілих даних
        if time.time() - t.get("fetched_ts", 0) > DATA_MAX_AGE_S:
            continue
        seen_sym.add(key)
        prio = _classify_token(t)
        queue.push(Event(prio, "token", t))

    log.info(f"Tokens queued: {len(seen_sym)} new")


def collect_whales(state: dict, queue: PriorityQueue):
    whales = []
    whales.extend(fetch_eth_whales())
    whales.extend(fetch_sol_whales())

    added = 0
    for w in whales:
        tx_id = w.get("tx_hash","")
        if not tx_id or tx_id in state["seen_whale_txs"]:
            continue
        if w["value_usd"] < min(WHALE_ETH_MIN_USD, WHALE_SOL_MIN_USD) and w["value_usd"] > 0:
            continue
        # Фільтр застарілих
        if time.time() - w.get("fetched_ts", 0) > DATA_MAX_AGE_S:
            continue
        prio = _classify_whale(w["value_usd"])
        queue.push(Event(prio, "whale", w))
        update_whale_portfolio(state, w)
        added += 1

    log.info(f"Whales queued: {added} new")


def collect_news(state: dict, queue: PriorityQueue):
    news   = fetch_news()
    market = fetch_market()
    fg     = fetch_fear_greed()

    if not news:
        return

    top = news[0]
    news_hash = hashlib.md5(top["title"].encode()).hexdigest()[:12]
    if news_hash in state["seen_news"]:
        return

    prio = PRIO_CRITICAL if top["critical"] else PRIO_NORMAL
    queue.push(Event(prio, "news", {
        "news_list": news, "market": market, "fg": fg,
        "news_hash": news_hash, "critical": top["critical"],
        "fear_greed": fg["value"], "fetched_ts": time.time(),
    }))
    log.info(f"News queued: '{top['title'][:50]}' prio={'CRITICAL' if prio==0 else 'NORMAL'}")


def collect_weekly(state: dict, queue: PriorityQueue):
    now = datetime.now(timezone.utc)
    # Субота (weekday=5), між 09:00 і 11:00 UTC
    if now.weekday() != 5:
        return
    if not (9 <= now.hour < 11):
        return
    # Не більше одного разу на тиждень
    last = state.get("last_weekly_post", 0)
    if time.time() - last < 6 * 24 * 3600:
        return

    market = fetch_market()
    fg     = fetch_fear_greed()
    queue.push(Event(PRIO_NORMAL, "weekly", {
        "market": market, "fg": fg, "fetched_ts": time.time()
    }))
    log.info("Weekly digest queued")


# ══════════════════════════════════════════════════════════════════════════════
#  ОБРОБКА ЧЕРГИ → ПУБЛІКАЦІЯ
# ══════════════════════════════════════════════════════════════════════════════

def process_queue(state: dict, queue: PriorityQueue):
    if not queue:
        return

    event = queue.pop()
    if not event:
        return

    is_critical = event.priority == PRIO_CRITICAL
    is_high     = event.priority == PRIO_HIGH
    d           = event.data

    # ── Перевірка дозволу на публікацію ──────────────────────────────────────
    if is_critical:
        ok, reason = can_post_critical(state)
        if not ok:
            log.info(f"CRITICAL blocked: {reason}")
            return
    else:
        kind = "high" if is_high else event.kind
        ok, reason = can_post_normal(state, kind)
        if not ok:
            log.info(f"Blocked [{event.kind}|{'HIGH' if is_high else 'NORMAL'}]: {reason}")
            return

    prio_label = "🔴 CRITICAL" if is_critical else "🟡 HIGH" if is_high else "NORMAL"
    log.info(f"Publishing [{event.kind}] [{prio_label}]")

    # ── Генерація і публікація ────────────────────────────────────────────────
    text  = None
    image = None

    if event.kind == "token":
        text    = gen_token_post(d, event.priority)
        history = _price_history(d) if text else []
        image   = make_token_chart(d, history) if history else None

        uid = f"{d['chain']}:{d['address'] or d['symbol']}"
        if text and publish(text, build_hashtags(event), image):
            state["seen_tokens"].append(uid)
            mark_posted(state, "token", is_critical, is_high)
            save_state(state)

    elif event.kind == "whale":
        text = gen_whale_post(d, event.priority)
        if text and publish(text, build_hashtags(event)):
            state["seen_whale_txs"].append(d.get("tx_hash",""))
            mark_posted(state, "whale", is_critical, is_high)
            save_state(state)

    elif event.kind == "news":
        text = gen_news_post(d["news_list"], d["market"], d["fg"], is_critical)
            if text:
                now_str = datetime.now(timezone.utc).strftime("%d.%m.%y %h:%m utc")
                text = text.replace("час: 20.", f"час: {now_str}")
                text = text.replace("**", "")
        image = make_market_chart(d["market"], d["fg"]) if text else None
        if text and publish(text, build_hashtags(event), image):
            state["seen_news"].append(d["news_hash"])
            mark_posted(state, "news", is_critical, is_high)
            save_state(state)

    elif event.kind == "weekly":
        text = gen_weekly_digest(state, d["market"], d["fg"])
        if text and publish(text, build_hashtags(event)):
            mark_posted(state, "weekly")
            # Скидаємо портфель китів після тижневого дайджесту
            state["whale_portfolio"] = {}
            save_state(state)


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN LOOP
# ══════════════════════════════════════════════════════════════════════════════

def main():
    log.info("=" * 55)
    log.info("🚀 AI Crypto Hunter v4")
    log.info(f"   Etherscan:  {'✅' if ETHERSCAN_KEY else '❌'}")
    log.info(f"   Helius:     {'✅' if HELIUS_KEY else '❌ (set HELIUS_KEY for SOL whales)'}")
    log.info(f"   CRITICAL threshold: ${CRITICAL_WHALE_USD:,.0f} whale / keywords in news")
    log.info(f"   HIGH threshold:     ${HIGH_WHALE_USD:,.0f} whale / +{HIGH_TOKEN_GAIN}% in 1h")
    log.info(f"   Max posts/day:      {MAX_POSTS_DAY} (CRITICAL не рахується)")
    log.info("=" * 55)

    state = load_state()
    queue = PriorityQueue()

    # Черговість збору (кожен тік — один тип, щоб не бити всі API одночасно)
    collectors = [
        ("whales", lambda: collect_whales(state, queue)),
        ("tokens", lambda: collect_tokens(state, queue)),
        ("news",   lambda: collect_news(state, queue)),
        ("weekly", lambda: collect_weekly(state, queue)),
    ]
    col_idx          = 0
    last_collect_ts  = 0
    COLLECT_INTERVAL = 20 * 60   # збираємо дані кожні 20 хв
    PROCESS_INTERVAL = 60        # обробляємо чергу кожну хвилину
    last_health_ts   = 0

    while True:
        now = time.time()

        # Збір даних (по черзі, один тип за тік)
        if now - last_collect_ts >= COLLECT_INTERVAL:
            name, fn = collectors[col_idx % len(collectors)]
            log.info(f"[collect] {name}")
            try:
                fn()
            except Exception as e:
                log.error(f"Collector {name} error: {e}")
            col_idx        += 1
            last_collect_ts = now

        # Обробка черги
        log.info(f"[queue] size={len(queue)}")
        try:
            process_queue(state, queue)
        except Exception as e:
            log.error(f"Queue process error: {e}")

        # Health check раз на годину
        if now - last_health_ts >= 3600:
            health_check(state)
            last_health_ts = now

        time.sleep(PROCESS_INTERVAL)


if __name__ == "__main__":
    main()
