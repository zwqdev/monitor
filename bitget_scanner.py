#!/usr/bin/env python3
"""Bitget scanner scaffolding and persistence helpers."""

import copy
import json
import os
import sys
from datetime import datetime, timezone, timedelta

import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TRADES_FILE = os.path.join(SCRIPT_DIR, "bitget_trades.json")
STATE_FILE = os.path.join(SCRIPT_DIR, "bitget_scanner_state.json")
LOG_FILE = os.path.join(SCRIPT_DIR, "bitget_scanner.log")

INITIAL_BALANCE = 100.0
TZ_UTC8 = timezone(timedelta(hours=8))
BITGET_BASE = "https://api.bitget.com"
PRODUCT_TYPE = "USDT-FUTURES"
MAX_OPEN_POSITIONS = 3
POSITION_PCT = 30
LEVERAGE = 3
COOLDOWN_HOURS = 4
MIN_VOLUME_M = 10
EXCLUDE_SYMBOLS = {"BTCUSDT", "ETHUSDT", "USDCUSDT", "FDUSDUSDT", "BTCSTUSDT"}
RUNTIME_ENV = None

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def now_str():
    return datetime.now(TZ_UTC8).strftime("%Y-%m-%d %H:%M:%S")


def log(message):
    line = f"[{now_str()}] {message}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as handle:
        handle.write(f"{line}\n")


def load_json(path, default):
    if not os.path.exists(path):
        return copy.deepcopy(default)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        log(f"加载JSON失败 {path}: {exc}")
        return copy.deepcopy(default)


def save_json(path, payload):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def load_trades():
    return load_json(
        TRADES_FILE,
        {"initial_balance": INITIAL_BALANCE, "trades": []},
    )


def save_trades(data):
    save_json(TRADES_FILE, data)


def load_state():
    return load_json(
        STATE_FILE,
        {"last_opens": {}, "signals_seen": {}},
    )


def save_state(state):
    save_json(STATE_FILE, state)


def get_balance(data):
    balance = data.get("initial_balance", INITIAL_BALANCE)
    for trade in data.get("trades", []):
        if trade.get("status") == "closed":
            balance += trade.get("pnl_usd", 0)
    return balance



def next_id(data):
    trades = data.get("trades", [])
    max_id = 0
    for trade in trades:
        try:
            max_id = max(max_id, int(trade.get("id", 0)))
        except (TypeError, ValueError, AttributeError):
            continue
    return f"{max_id + 1:03d}"


def load_env():
    env = {}
    env_paths = [os.path.join(SCRIPT_DIR, ".env"), os.path.join(os.getcwd(), ".env")]

    for path in env_paths:
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    env[key.strip()] = value.strip()
        except OSError as exc:
            log(f"加载环境变量文件失败 {path}: {exc}")

    merged = dict(env)
    merged.update(os.environ)
    return merged


def env_flag(env, key, default=False):
    value = env.get(key)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def notify(text):
    env = RUNTIME_ENV if RUNTIME_ENV is not None else load_env()
    if not env_flag(env, "ENABLE_NOTIFICATIONS"):
        return
    log(f"通知已启用，占位模式，尚未配置外部通知提供方: {text}")


def http_get(path, params=None, timeout=10):
    try:
        response = requests.get(f"{BITGET_BASE}{path}", params=params, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        log(f"HTTP错误 {path}: {exc}")
        return None

    if isinstance(payload, dict):
        code = payload.get("code")
        if code is not None and str(code) != "00000":
            log(f"Bitget返回错误 {path}: code={code}, msg={payload.get('msg')}")
            return None
    return payload


def normalize_percent(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if -1 <= parsed <= 1:
        return parsed * 100
    return parsed


def normalize_symbol(raw_symbol):
    if raw_symbol is None:
        return None
    symbol = str(raw_symbol).strip().upper()
    if not symbol:
        return None
    for suffix in ("_UMCBL", "-UMCBL"):
        if symbol.endswith(suffix):
            symbol = symbol[: -len(suffix)]
            break
    for sep in ("-", "_", "/"):
        symbol = symbol.replace(sep, "")
    return symbol or None


def to_bitget_symbol(symbol):
    return normalize_symbol(symbol)


def get_all_tickers():
    payload = http_get(
        "/api/v2/mix/market/tickers",
        params={"productType": PRODUCT_TYPE},
    )
    if not isinstance(payload, dict):
        return []

    data = payload.get("data")
    if not isinstance(data, list):
        return []

    tickers = []
    for row in data:
        if not isinstance(row, dict):
            continue

        symbol = normalize_symbol(row.get("symbol") or row.get("instId"))
        if not symbol:
            continue

        try:
            last_price = float(row.get("lastPr") or row.get("last") or row.get("lastPrice"))
            change_ratio = normalize_percent(
                row.get("changeUtc24h") or row.get("chgUtc") or row.get("priceChangePercent")
            )
            quote_volume = float(row.get("usdtVolume") or row.get("quoteVolume") or row.get("volumeUsd24h"))
        except (TypeError, ValueError):
            continue
        if change_ratio is None:
            continue
        tickers.append(
            {
                "symbol": symbol,
                "lastPrice": last_price,
                "priceChangePercent": change_ratio,
                "quoteVolume": quote_volume,
            }
        )
    return tickers


def get_funding_rates(tickers=None):
    tickers = tickers if isinstance(tickers, list) else get_all_tickers()
    funding_rates = {}
    for ticker in tickers:
        symbol = ticker.get("symbol")
        bitget_symbol = to_bitget_symbol(symbol)
        if not bitget_symbol:
            continue

        payload = http_get(
            "/api/v2/mix/market/current-fund-rate",
            params={"symbol": bitget_symbol, "productType": PRODUCT_TYPE},
        )
        if not isinstance(payload, dict):
            continue

        data = payload.get("data")
        rows = data if isinstance(data, list) else []
        row = rows[0] if rows else None
        if not isinstance(row, dict):
            continue

        try:
            rate = float(row.get("fundingRate") or row.get("rate")) * 100
        except (TypeError, ValueError):
            continue
        funding_rates[symbol] = rate
    return funding_rates


def get_funding_history(symbol, limit=8):
    bitget_symbol = to_bitget_symbol(symbol)
    if not bitget_symbol:
        return []

    payload = http_get(
        "/api/v2/mix/market/history-fund-rate",
        params={"symbol": bitget_symbol, "productType": PRODUCT_TYPE, "pageSize": limit},
    )
    if not isinstance(payload, dict):
        return []

    data = payload.get("data")
    if not isinstance(data, list):
        return []

    parsed = []
    for row in data:
        if not isinstance(row, dict):
            continue
        try:
            rate = float(row.get("fundingRate") or row.get("rate")) * 100
        except (TypeError, ValueError):
            continue
        parsed.append(rate)
    return list(reversed(parsed))


def get_open_interest(symbol):
    bitget_symbol = to_bitget_symbol(symbol)
    if not bitget_symbol:
        return None

    payload = http_get(
        "/api/v2/mix/market/open-interest",
        params={"symbol": bitget_symbol, "productType": PRODUCT_TYPE},
    )
    if not isinstance(payload, dict):
        return None

    data = payload.get("data")
    if not isinstance(data, dict):
        return None

    open_interest_list = data.get("openInterestList")
    if isinstance(open_interest_list, list) and open_interest_list:
        row = open_interest_list[0]
        if isinstance(row, dict):
            try:
                return float(row.get("size"))
            except (TypeError, ValueError):
                return None

    for key in ("openInterest", "size", "amount"):
        value = data.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


def get_klines(symbol, interval="1H", limit=6):
    bitget_symbol = to_bitget_symbol(symbol)
    if not bitget_symbol:
        return []

    payload = http_get(
        "/api/v2/mix/market/candles",
        params={
            "symbol": bitget_symbol,
            "productType": PRODUCT_TYPE,
            "granularity": interval,
            "limit": limit,
        },
    )
    if not isinstance(payload, dict):
        return []

    data = payload.get("data")
    if not isinstance(data, list):
        return []

    rows = []
    for row in data:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        try:
            rows.append(
                {
                    "timestamp": int(row[0]),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                }
            )
        except (TypeError, ValueError, IndexError):
            continue
    rows.sort(key=lambda row: row["timestamp"])
    return rows


def build_signal(signal_type, direction, strength, reason, sl_pct, tp_pct):
    return {
        "type": signal_type,
        "direction": direction,
        "strength": strength,
        "reason": reason,
        "sl_pct": sl_pct,
        "tp_pct": tp_pct,
    }


def get_funding_history_cached(symbol, funding_history_map, limit=8):
    if symbol not in funding_history_map:
        funding_history_map[symbol] = get_funding_history(symbol, limit=limit)
    return funding_history_map[symbol]


def detect_extreme_negative_funding(symbol, funding_rate, funding_map):
    try:
        current_rate = float(funding_rate)
    except (TypeError, ValueError):
        return None

    if current_rate >= -0.08 or not isinstance(funding_map, dict):
        return None

    history = funding_map.get(symbol)
    if not isinstance(history, list) or len(history) < 8:
        return None

    try:
        recent_history = [float(rate) for rate in history[-8:]]
    except (TypeError, ValueError):
        return None

    negative_count = sum(1 for rate in recent_history if rate < -0.03)
    if negative_count < 4:
        return None

    avg_rate = sum(recent_history) / len(recent_history)
    strength = "S" if avg_rate < -0.15 else "A" if avg_rate < -0.10 else "B"
    return build_signal(
        "extreme_neg_funding",
        "long",
        strength,
        f"费率极端深负 avg:{avg_rate:.4f}% 连续{negative_count}/8期为负 逼空概率高",
        0.08,
        0.12,
    )


def detect_extreme_positive_funding(symbol, funding_rate, funding_map):
    try:
        current_rate = float(funding_rate)
    except (TypeError, ValueError):
        return None

    if current_rate <= 0.10 or not isinstance(funding_map, dict):
        return None

    history = funding_map.get(symbol)
    if not isinstance(history, list) or len(history) < 8:
        return None

    try:
        recent_history = [float(rate) for rate in history[-8:]]
    except (TypeError, ValueError):
        return None

    positive_count = sum(1 for rate in recent_history if rate > 0.05)
    if positive_count < 4:
        return None

    avg_rate = sum(recent_history) / len(recent_history)
    strength = "S" if avg_rate > 0.20 else "A" if avg_rate > 0.12 else "B"
    return build_signal(
        "extreme_pos_funding",
        "short",
        strength,
        f"费率极端正 avg:{avg_rate:.4f}% 连续{positive_count}/8期高正 多头过度拥挤",
        0.10,
        0.15,
    )


def detect_crash_bounce(ticker):
    if not isinstance(ticker, dict):
        return None

    symbol = ticker.get("symbol")
    try:
        change_pct = float(ticker.get("priceChangePercent"))
    except (TypeError, ValueError):
        return None

    if not symbol or change_pct >= -25:
        return None

    klines = get_klines(symbol, interval="1H", limit=6)
    if len(klines) < 3:
        return None

    try:
        recent_closes = [float(row["close"]) for row in klines[-3:]]
    except (TypeError, ValueError, KeyError):
        return None

    if len(recent_closes) >= 2 and recent_closes[-1] >= recent_closes[-2]:
        return build_signal(
            "crash_bounce",
            "long",
            "B",
            f"24h暴跌{change_pct:.1f}%后企稳 超跌反弹",
            0.10,
            0.15,
        )
    return None


def detect_pump_short(ticker):
    if not isinstance(ticker, dict):
        return None

    symbol = ticker.get("symbol")
    try:
        change_pct = float(ticker.get("priceChangePercent"))
    except (TypeError, ValueError):
        return None

    if not symbol or change_pct <= 40:
        return None

    klines = get_klines(symbol, interval="1H", limit=6)
    if len(klines) < 1:
        return None

    try:
        highs = [float(row["high"]) for row in klines]
        closes = [float(row["close"]) for row in klines]
    except (TypeError, ValueError, KeyError):
        return None

    if not highs or not closes:
        return None

    current = closes[-1]
    peak = max(highs)
    if peak <= 0:
        return None

    pullback = (peak - current) / peak * 100
    if pullback < 10:
        return None

    strength = "A" if change_pct > 80 else "B"
    return build_signal(
        "pump_short",
        "short",
        strength,
        f"24h暴涨{change_pct:.1f}%后回落{pullback:.1f}% 历史回调概率>85%",
        0.15,
        0.20,
    )


def check_environment(symbol, signal):
    analysis = {
        "btc_env": "",
        "sentiment": "",
        "oi_check": "",
        "volume_check": "",
        "verdict": "",
    }

    if not isinstance(signal, dict):
        return False, analysis

    direction = signal.get("direction")
    strength = signal.get("strength")
    score = 0

    tickers = get_all_tickers()
    ticker_map = {
        ticker.get("symbol"): ticker for ticker in tickers if isinstance(ticker, dict) and ticker.get("symbol")
    }

    btc_ticker = ticker_map.get("BTCUSDT")
    if isinstance(btc_ticker, dict):
        try:
            btc_change = float(btc_ticker.get("priceChangePercent"))
            if direction == "long":
                if btc_change > -2:
                    score += 1
                    analysis["btc_env"] = f"BTC {btc_change:+.1f}% 环境正常 +1"
                elif btc_change < -5:
                    score -= 1
                    analysis["btc_env"] = f"BTC {btc_change:+.1f}% 暴跌中做多危险 -1"
                else:
                    analysis["btc_env"] = f"BTC {btc_change:+.1f}% 偏弱 0"
            elif direction == "short":
                if btc_change < 2:
                    score += 1
                    analysis["btc_env"] = f"BTC {btc_change:+.1f}% 环境正常 +1"
                elif btc_change > 5:
                    score -= 1
                    analysis["btc_env"] = f"BTC {btc_change:+.1f}% 暴涨中做空危险 -1"
                else:
                    analysis["btc_env"] = f"BTC {btc_change:+.1f}% 偏强 0"
        except (TypeError, ValueError):
            pass

    try:
        response = requests.get("https://api.alternative.me/fng/", params={"limit": 1}, timeout=10)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        log(f"获取恐慌贪婪指数失败: {exc}")
    else:
        rows = payload.get("data") if isinstance(payload, dict) else None
        row = rows[0] if isinstance(rows, list) and rows else None
        if isinstance(row, dict):
            try:
                fgi_value = int(row.get("value"))
                if direction == "long":
                    if fgi_value <= 25:
                        score += 1
                        analysis["sentiment"] = f"FGI={fgi_value}极度恐惧 逆向做多 +1"
                    elif fgi_value >= 75:
                        score -= 1
                        analysis["sentiment"] = f"FGI={fgi_value}极度贪婪 做多风险 -1"
                    else:
                        analysis["sentiment"] = f"FGI={fgi_value}中性 0"
                elif direction == "short":
                    if fgi_value >= 75:
                        score += 1
                        analysis["sentiment"] = f"FGI={fgi_value}极度贪婪 逆向做空 +1"
                    elif fgi_value <= 25:
                        score -= 1
                        analysis["sentiment"] = f"FGI={fgi_value}极度恐惧 做空风险 -1"
                    else:
                        analysis["sentiment"] = f"FGI={fgi_value}中性 0"
            except (TypeError, ValueError):
                pass

    symbol_ticker = ticker_map.get(normalize_symbol(symbol))
    current_price = None
    if isinstance(symbol_ticker, dict):
        try:
            current_price = float(symbol_ticker.get("lastPrice"))
        except (TypeError, ValueError):
            current_price = None

    oi_value = get_open_interest(symbol)
    if oi_value is not None:
        if oi_value > 5_000_000:
            score += 1
            analysis["oi_check"] = f"OI={oi_value / 1e6:.1f}M 有关注度 +1"
        else:
            analysis["oi_check"] = f"OI={oi_value / 1e6:.1f}M 关注度低 0"

    if isinstance(symbol_ticker, dict):
        try:
            volume_usd = float(symbol_ticker.get("quoteVolume"))
            if volume_usd > 50_000_000:
                score += 1
                analysis["volume_check"] = f"24h量={volume_usd / 1e6:.0f}M 活跃 +1"
            elif volume_usd > 20_000_000:
                analysis["volume_check"] = f"24h量={volume_usd / 1e6:.0f}M 一般 0"
            else:
                score -= 1
                analysis["volume_check"] = f"24h量={volume_usd / 1e6:.0f}M 冷清 -1"
        except (TypeError, ValueError):
            pass

    if strength == "S":
        score += 2
    elif strength == "A":
        score += 1

    analysis["verdict"] = f"综合得分:{score}/7"
    return score >= 3, analysis


def execute_open(data, state, symbol, price, signal):
    passed, analysis = check_environment(symbol, signal)
    if not passed:
        env_summary = " | ".join(value for value in analysis.values() if value)
        log(f"综合检查未通过 {symbol}: {env_summary}")
        return None

    try:
        entry_price = float(price)
    except (TypeError, ValueError):
        log(f"开仓拒绝 {symbol}: 无效价格 {price}")
        return None
    if entry_price <= 0:
        log(f"开仓拒绝 {symbol}: 价格必须大于0")
        return None

    direction = signal.get("direction")
    if direction not in {"long", "short"}:
        log(f"开仓拒绝 {symbol}: 无效方向 {direction}")
        return None

    try:
        sl_pct = float(signal.get("sl_pct"))
        tp_pct = float(signal.get("tp_pct"))
    except (TypeError, ValueError):
        log(f"开仓拒绝 {symbol}: 止盈止损配置无效")
        return None
    if sl_pct > 1:
        sl_pct /= 100
    if tp_pct > 1:
        tp_pct /= 100
    if not (0 < sl_pct < 1 and 0 < tp_pct < 1):
        log(f"开仓拒绝 {symbol}: 止盈止损配置越界 sl={sl_pct} tp={tp_pct}")
        return None

    balance = get_balance(data)
    position_usd = balance * POSITION_PCT / 100
    notional_usd = position_usd * LEVERAGE

    if direction == "long":
        stop_loss = entry_price * (1 - sl_pct)
        take_profit = entry_price * (1 + tp_pct)
    else:
        stop_loss = entry_price * (1 + sl_pct)
        take_profit = entry_price * (1 - tp_pct)

    trade = {
        "id": next_id(data),
        "symbol": normalize_symbol(symbol),
        "direction": direction,
        "leverage": LEVERAGE,
        "position_pct": POSITION_PCT,
        "position_usd": round(position_usd, 4),
        "notional_usd": round(notional_usd, 4),
        "entry_price": entry_price,
        "signal_type": signal.get("type"),
        "signal_strength": signal.get("strength"),
        "signal_sl_pct": sl_pct,
        "signal_tp_pct": tp_pct,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "entry_time": now_str(),
        "exit_price": None,
        "exit_time": None,
        "exit_reason": None,
        "pnl_pct": None,
        "pnl_usd": None,
        "status": "open",
        "pre_analysis": {
            "btc_env": analysis.get("btc_env", ""),
            "sentiment": analysis.get("sentiment", ""),
            "oi": analysis.get("oi_check", ""),
            "volume": analysis.get("volume_check", ""),
            "key_reason": f"[{signal.get('strength')}级] {signal.get('reason', '')}",
            "risk": f"{analysis.get('verdict', '')} 策略:{signal.get('type', '')}",
        },
        "post_review": None,
    }
    data.setdefault("trades", []).append(trade)
    state.setdefault("last_opens", {})[trade["symbol"]] = trade["entry_time"]
    save_trades(data)
    save_state(state)

    log(
        f"开仓 {trade['id']} {trade['symbol']} {direction} strength={signal.get('strength')} "
        f"entry={entry_price:.6f} sl={stop_loss:.6f} tp={take_profit:.6f}"
    )
    notify(
        f"开仓 {trade['symbol']} {direction} {signal.get('type')} strength={signal.get('strength')} "
        f"entry={entry_price:.6f} sl={stop_loss:.6f} tp={take_profit:.6f}"
    )
    return trade


def scan():
    data = load_trades()
    state = load_state()

    open_trades = [
        trade for trade in data.get("trades", []) if isinstance(trade, dict) and trade.get("status") == "open"
    ]
    open_positions = {
        normalize_symbol(trade.get("symbol"))
        for trade in open_trades
        if normalize_symbol(trade.get("symbol"))
    }
    if len(open_trades) >= MAX_OPEN_POSITIONS:
        log(f"当前开仓数已达上限 {len(open_trades)}/{MAX_OPEN_POSITIONS}，跳过扫描")
        return

    tickers = get_all_tickers()
    if not tickers:
        log("获取ticker失败或为空，跳过扫描")
        return

    funding_rates = get_funding_rates(tickers)
    if not funding_rates:
        log("获取资金费率失败或为空，跳过扫描")
        return

    ticker_map = {
        ticker.get("symbol"): ticker for ticker in tickers if isinstance(ticker, dict) and ticker.get("symbol")
    }
    funding_history_map = {}
    now = datetime.now(TZ_UTC8)
    cooldown_delta = timedelta(hours=COOLDOWN_HOURS)
    candidates = []

    for symbol, ticker in ticker_map.items():
        normalized_symbol = normalize_symbol(symbol)
        if not normalized_symbol or not normalized_symbol.endswith("USDT"):
            continue
        if normalized_symbol in EXCLUDE_SYMBOLS:
            continue
        if normalized_symbol in open_positions:
            continue

        try:
            quote_volume = float(ticker.get("quoteVolume"))
        except (TypeError, ValueError):
            continue
        if quote_volume <= MIN_VOLUME_M * 1e6:
            continue

        last_open_raw = state.get("last_opens", {}).get(normalized_symbol)
        if last_open_raw:
            try:
                last_open_time = datetime.strptime(last_open_raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ_UTC8)
            except ValueError:
                log(f"忽略无效冷却时间 {normalized_symbol}: {last_open_raw}")
            else:
                if now - last_open_time < cooldown_delta:
                    continue

        candidates.append((normalized_symbol, ticker, quote_volume))

    signals = []
    strength_rank = {"S": 0, "A": 1, "B": 2}
    for symbol, ticker, quote_volume in candidates:
        funding_rate = funding_rates.get(symbol)

        detectors = (
            lambda: detect_extreme_negative_funding(
                symbol,
                funding_rate,
                {symbol: get_funding_history_cached(symbol, funding_history_map, limit=8)},
            ),
            lambda: detect_extreme_positive_funding(
                symbol,
                funding_rate,
                {symbol: get_funding_history_cached(symbol, funding_history_map, limit=8)},
            ),
            lambda: detect_crash_bounce(ticker),
            lambda: detect_pump_short(ticker),
        )

        for detector in detectors:
            try:
                signal = detector()
            except Exception as exc:
                log(f"信号检测异常 {symbol}: {exc}")
                continue
            if not isinstance(signal, dict):
                continue

            enriched = dict(signal)
            try:
                enriched["price"] = float(ticker.get("lastPrice"))
            except (TypeError, ValueError):
                log(f"信号价格无效，跳过 {symbol}")
                continue
            enriched["symbol"] = symbol
            enriched["volume_m"] = quote_volume / 1e6
            signals.append(enriched)

    if not signals:
        log("本轮未发现可执行信号")
        return

    signals.sort(key=lambda item: (strength_rank.get(item.get("strength"), 99), -item.get("volume_m", 0)))
    best_signal = signals[0]
    if best_signal.get("strength") == "B":
        log(
            f"最佳信号为B级，按策略跳过 {best_signal.get('symbol')} {best_signal.get('type')} "
            f"reason={best_signal.get('reason', '')}"
        )
        return

    opened_trade = execute_open(data, state, best_signal.get("symbol"), best_signal.get("price"), best_signal)
    if opened_trade is None:
        log(f"最佳信号未能开仓 {best_signal.get('symbol')} {best_signal.get('type')}")


def main():
    global RUNTIME_ENV
    RUNTIME_ENV = load_env()
    if env_flag(RUNTIME_ENV, "ENABLE_NOTIFICATIONS"):
        log("通知已启用，占位模式，尚未配置外部通知提供方")
    else:
        log("通知未启用，运行本地扫描模式")
    scan()


if __name__ == "__main__":
    main()
