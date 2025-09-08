
# -*- coding: utf-8 -*-
# okx_futures_bot_FIXED_VWAP_ONLY.py
# Strategy: VWAP Price Channel ONLY (Anchored VWAP at last swing High/Low)
# - Removes all other entry filters/tools (zones, inducement, BOS/FVG, MTF votes, confluence counts, etc.)
# - Entry: continuation breakout across "opposite" anchored VWAP with current trend
# - Stop: trailing at the active anchored VWAP
# - Take Profit: RR * initial stop distance
# - Simulated trading ON by default (x-simulated-trading header)
#
# NOTE: Reads the same .env keys as your original bot. Keep your secrets safe.
#
# Author: adapted for Saleh (VWAP-only)
# Confluence filter inspired by AlgoAlpha's HMA/pivot order-block approach

import os
import time
import hmac
import base64
import json
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, Tuple, List

import requests
import pandas as pd
import numpy as np
import math

def log(message: str) -> None:
    print(message, flush=True)

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def iso_utc_ms() -> str:
    return now_utc().isoformat(timespec="milliseconds").replace("+00:00", "Z")

def fmt_signed(x: float) -> str:
    return f"+{x:.4f}" if x >= 0 else f"-{abs(x):.4f}"

try:
    from dotenv import load_dotenv  # type: ignore
except Exception:
    load_dotenv = None

# ---------------------------
# Settings & Utilities
# ---------------------------
def load_settings() -> Dict[str, any]:
    # Load .env if available
    if load_dotenv is not None:
        load_dotenv()
    else:
        env_path = os.path.join(os.getcwd(), '.env')
        if os.path.exists(env_path):
            with open(env_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line=line.strip()
                    if not line or line.startswith('#') or '=' not in line:
                        continue
                    k,v = line.split('=',1)
                    os.environ.setdefault(k.strip(), v.strip())

    required = [
        "OKX_API_KEY",
        ("OKX_SECRET_KEY","OKX_API_SECRET"),
        ("OKX_PASSPHRASE","OKX_API_PASSPHRASE"),
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    ]
    s: Dict[str, any] = {}
    for item in required:
        if isinstance(item, tuple):
            p,f = item
            val = os.getenv(p) or os.getenv(f)
            if not val:
                raise EnvironmentError(f"Missing required environment variable: {p} (or {f})")
            s[p] = val
        else:
            val = os.getenv(item)
            if not val:
                raise EnvironmentError(f"Missing required environment variable: {item}")
            s[item] = val

    # Instruments
    instrument_list = os.getenv("INSTRUMENT_LIST")
    single = os.getenv("INSTRUMENT_ID")
    if instrument_list:
        lst = [x.strip() for x in instrument_list.split(",") if x.strip()]
        s["INSTRUMENT_LIST"] = lst if lst else []
    elif single:
        s["INSTRUMENT_LIST"] = [single.strip()]
    else:
        s["INSTRUMENT_LIST"] = ["BTC-USDT-SWAP","ETH-USDT-SWAP","SOL-USDT-SWAP"]

    s["TIMEFRAME"] = os.getenv("TIMEFRAME","30m")
    s["BASE_URL"] = os.getenv("OKX_BASE_URL","https://www.okx.com")

    # Fees and fixed margin per trade
    def _f(name, default):
        val = os.getenv(name)
        try:
            return float(val) if val is not None else default
        except Exception:
            return default
    def _i(name, default):
        val = os.getenv(name)
        try:
            return int(val) if val is not None else default
        except Exception:
            return default
    def _b(name, default=False):
        val = os.getenv(name)
        if val is None:
            return default
        return str(val).lower() in ("1","true","on","yes")

    s["FEE_RATE"] = _f("FEE_RATE", 0.0006)
    s["REWARD_RISK_RATIO"] = _f("REWARD_RISK_RATIO", 5.0)
    s["MARGIN_PER_TRADE_USDT"] = _f("MARGIN_PER_TRADE_USDT", 90.0)
    s["LEVERAGE"] = _i("LEVERAGE", 10)
    s["AUTO_ADJUST_MARGIN"] = _b("AUTO_ADJUST_MARGIN", True)
    s["MIN_MARGIN_PER_TRADE_USDT"] = _f("MIN_MARGIN_PER_TRADE_USDT", 15.0)
    s["ENVIRONMENT"] = os.getenv("ENVIRONMENT","demo")

    # Files
    s["DATA_DIR"] = os.getenv("DATA_DIR",".")
    s["TRADE_HISTORY_FILE"] = os.getenv("TRADE_HISTORY_FILE","trade_history.csv")
    s["STATE_FILE"] = os.getenv("STATE_FILE","bot_state.json")

    # Reporting
    s["VERBOSE"] = int(os.getenv("VERBOSE","1") or "1")

    # Confluence filter settings (Trend + Order Block + Volume)
    def _i(name, default):
        val = os.getenv(name)
        try:
            return int(val) if val is not None else default
        except Exception:
            return default
    def _b(name, default=False):
        val = os.getenv(name)
        if val is None:
            return default
        return str(val).lower() in ("1","true","yes","on")

    s["UNIFIED_FILTER_ENABLED"] = _b("UNIFIED_FILTER_ENABLED", False)
    s["OB_TREND_MA_TYPE"] = os.getenv("OB_TREND_MA_TYPE", "HMA")
    s["OB_TREND_MA_LEN"] = _i("OB_TREND_MA_LEN", 55)
    s["OB_PIVOT_LEFT"] = _i("OB_PIVOT_LEFT", 3)
    s["OB_PIVOT_RIGHT"] = _i("OB_PIVOT_RIGHT", 3)
    s["OB_LOOKBACK"] = _i("OB_LOOKBACK", 300)
    s["OB_ZONE_EXTEND_BARS"] = _i("OB_ZONE_EXTEND_BARS", 500)
    s["OB_INVALIDATION_MODE"] = os.getenv("OB_INVALIDATION_MODE", "CLOSE_THROUGH")
    s["OB_MIN_ZONE_SIZE_MULT"] = _f("OB_MIN_ZONE_SIZE_MULT", 0.25)
    s["OB_ATR_LEN"] = _i("OB_ATR_LEN", 14)
    s["OB_MTF_ENABLED"] = _b("OB_MTF_ENABLED", False)
    s["OB_MTF_TIMEFRAME"] = os.getenv("OB_MTF_TIMEFRAME", "1h")
    s["OB_ENTRY_TOLERANCE_PCT"] = _f("OB_ENTRY_TOLERANCE_PCT", 0.15)
    s["OB_VOLUME_LOOKBACK"] = _i("OB_VOLUME_LOOKBACK", 20)
    s["OB_VOLUME_MULT"] = _f("OB_VOLUME_MULT", 1.2)

    s["DC_MODE"] = _b("DC_MODE", False)
    s["DC_LEN"] = _i("DC_LEN", 20)

    # Top40 universe and dynamic TP/SL settings
    s["USE_TOP_USDT"] = _b("USE_TOP_USDT", True)
    s["TOP_USDT_MODE"] = os.getenv("TOP_USDT_MODE", "override")
    s["TOP_USDT_COUNT"] = _i("TOP_USDT_COUNT", 40)
    s["TOP_USDT_SORT"] = os.getenv("TOP_USDT_SORT", "volCcy24h")
    s["TOP_USDT_MIN_VOL"] = _f("TOP_USDT_MIN_VOL", 1000000)
    s["ALLOW_QUANTO"] = _b("ALLOW_QUANTO", False)
    s["UNIVERSE_MIN_COUNT"] = _i("UNIVERSE_MIN_COUNT", 8)
    s["UNIVERSE_ENABLE_FALLBACK"] = _b("UNIVERSE_ENABLE_FALLBACK", True)

    s["VWAP_STOP_ATR_MULT"] = _f("VWAP_STOP_ATR_MULT", 0.5)
    s["MIN_STOP_ATR"] = _f("MIN_STOP_ATR", 0.4)
    s["MIN_HOLD_BARS"] = _i("MIN_HOLD_BARS", 1)
    s["BE_TRIGGER_R"] = _f("BE_TRIGGER_R", 1.0)
    s["PARTIAL_TP_ENABLED"] = _b("PARTIAL_TP_ENABLED", True)
    s["PARTIAL_TP_PCT"] = _f("PARTIAL_TP_PCT", 0.5)
    s["PARTIAL_TP_R"] = _f("PARTIAL_TP_R", 1.0)
    s["COOLDOWN_SEC"] = _i("COOLDOWN_SEC", 120)

    return s

def sign_request(secret_key: str, timestamp: str, method: str, request_path: str, body: str) -> str:
    message = f"{timestamp}{method}{request_path}{body}"
    mac = hmac.new(secret_key.encode("utf-8"), msg=message.encode("utf-8"), digestmod="sha256")
    return base64.b64encode(mac.digest()).decode()

def okx_request(settings: Dict[str, any], method: str, path: str, params: Optional[Dict]=None, body: Optional[Dict]=None, private: bool=False) -> Dict:
    url = settings["BASE_URL"] + path
    headers = {'Content-Type': 'application/json'}
    env = str(settings.get("ENVIRONMENT", "demo")).lower()
    headers['x-simulated-trading'] = '1' if env == 'demo' else '0'
    if private:
        ts = iso_utc_ms()
        body_str = json.dumps(body) if body else ''
        req_path = path + (f"?{requests.compat.urlencode(params)}" if params else '')
        sign = sign_request(settings["OKX_SECRET_KEY"], ts, method, req_path, body_str)
        headers.update({
            'OK-ACCESS-KEY': settings['OKX_API_KEY'],
            'OK-ACCESS-SIGN': sign,
            'OK-ACCESS-TIMESTAMP': ts,
            'OK-ACCESS-PASSPHRASE': settings['OKX_PASSPHRASE'],
        })
    try:
        if method.upper()=="GET":
            resp = requests.get(url, headers=headers, params=params, timeout=15)
        else:
            resp = requests.post(url, headers=headers, params=params, data=json.dumps(body), timeout=15)
    except Exception as e:
        raise ConnectionError(f"HTTP {method} {url} failed: {e}")
    if not resp.ok:
        raise RuntimeError(f"HTTP {resp.status_code} {url}: {resp.text}")
    try:
        return resp.json()
    except Exception:
        raise ValueError(f"Bad JSON from OKX: {resp.text}")

def send_telegram(settings: Dict[str, any], message: str) -> None:
    url = f"https://api.telegram.org/bot{settings['TELEGRAM_BOT_TOKEN']}/sendMessage"
    payload = {'chat_id': settings['TELEGRAM_CHAT_ID'], 'text': message, 'parse_mode': 'HTML'}
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        log(f"[Telegram] send failed: {e}")


def scan_okx(settings: Dict[str, any]) -> None:
    """Simple connectivity check to ensure the script runs."""
    try:
        r = okx_request(settings, "GET", "/api/v5/public/time")
        ts = r.get("data", [{}])[0].get("ts", "?")
        log(f"[SCAN] OKX server time: {ts}")
    except Exception as e:
        log(f"[SCAN_ERROR] {e}")
        send_telegram(settings, f"⚠️ فشل الفحص: {e}")

# ---------------------------
# Exchange Helpers
# ---------------------------
def get_account_balance(settings: Dict[str, any], currency: str='USDT') -> float:
    r = okx_request(settings, "GET", "/api/v5/account/balance", private=True)
    if r.get("code")!="0":
        raise RuntimeError(f"balance error: {r}")
    total = 0.0
    for d in r["data"][0].get("details", []):
        if d.get("ccy")==currency:
            total += float(d.get("availBal",0))
    return total

def get_last_price(settings: Dict[str, any], inst_id: str) -> float:
    r = okx_request(settings, "GET", "/api/v5/market/ticker", params={'instId': inst_id})
    if r.get("code")!="0":
        raise RuntimeError(f"ticker error: {r}")
    return float(r["data"][0]["last"])

def _precision_from_str(num_str: str) -> int:
    if num_str is None:
        return 0
    if isinstance(num_str, (int, float)):
        num_str = str(num_str)
    if '.' in num_str:
        return len(num_str.split('.')[1].rstrip('0'))
    return 0

def calc_atr(df: pd.DataFrame, length: int=14) -> pd.Series:
    high = df['high']
    low = df['low']
    close = df['close']
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(length).mean()

def get_order_fill(settings: Dict[str, any], inst_id: str, ord_id: str) -> Optional[Dict[str, float]]:
    r = okx_request(settings, "GET", "/api/v5/trade/fills", params={'instId':inst_id,'ordId':ord_id}, private=True)
    if r.get('code') != '0' or not r.get('data'):
        return None
    d = r['data'][0]
    try:
        return {
            'fillPx': float(d.get('fillPx', 0)),
            'fillSz': float(d.get('fillSz', 0)),
            'fee': float(d.get('fee', 0))
        }
    except Exception:
        return None

def get_instrument_specs(settings: Dict[str, any], inst_id: str) -> Optional[Dict[str, float]]:
    r = okx_request(settings, "GET", "/api/v5/public/instruments", params={'instType':'SWAP','instId':inst_id})
    if r.get("code")!="0" or not r.get("data"):
        return None
    info = r["data"][0]
    try:
        ct = float(info["ctVal"])
        lot = float(info["lotSz"])
        min_sz = float(info["minSz"])
        prec = _precision_from_str(info["lotSz"])
        return {"ctVal": ct, "lotSz": lot, "minSz": min_sz, "szPrec": prec}
    except Exception:
        log(f"[SPEC_ERROR] {inst_id} -> {info}")
        return None

def prefetch_instrument_specs(settings: Dict[str, any], inst_list: List[str]) -> Dict[str, Dict[str, float]]:
    spec: Dict[str, Dict[str, float]] = {}
    try:
        r = okx_request(settings, "GET", "/api/v5/public/instruments", params={'instType':'SWAP'})
        if r.get("code")!="0":
            return spec
        for info in r.get("data",[]):
            iid = info.get("instId")
            if iid in inst_list:
                sp = get_instrument_specs(settings, iid)
                if sp:
                    spec[iid] = sp
        return spec
    except Exception as e:
        log(f"[SPEC_FETCH_ERROR] {e}")
        return spec

def filter_valid_instruments(settings: Dict[str, any], inst_list: List[str]) -> List[str]:
    """Remove instruments that are not recognised by OKX to avoid 51001 errors."""
    try:
        r = okx_request(settings, "GET", "/api/v5/public/instruments", params={'instType': 'SWAP'})
        if r.get("code") != "0":
            return inst_list
        valid = {d.get("instId") for d in r.get("data", [])}
        out = []
        for inst in inst_list:
            if inst in valid:
                out.append(inst)
            else:
                log(f"[WARN] تجاهل الزوج غير المعروف {inst}")
        return out or inst_list
    except Exception as e:
        log(f"[WARN] فشل التحقق من الأزواج: {e}")
        return inst_list

def build_top_usdt_universe(settings: Dict[str, any]) -> Tuple[List[str], Dict[str, Dict[str, float]]]:
    if not settings.get('USE_TOP_USDT', False):
        insts = filter_valid_instruments(settings, settings['INSTRUMENT_LIST'])
        specs = prefetch_instrument_specs(settings, insts)
        return insts, specs
    log(f"[UNIVERSE] env={settings.get('ENVIRONMENT')} mode={settings.get('TOP_USDT_MODE')}")
    try:
        r = okx_request(settings, 'GET', '/api/v5/public/instruments', params={'instType':'SWAP'})
        if r.get('code') != '0':
            raise RuntimeError(str(r))
        allowed: List[str] = []
        meta: Dict[str, Dict[str, float]] = {}
        for info in r.get('data', []):
            if info.get('settleCcy') != 'USDT':
                continue
            if not settings.get('ALLOW_QUANTO', False) and info.get('ctType') == 'inverse':
                continue
            state = (info.get('state') or '').lower()
            if state in ('suspend', 'delisted', 'offline'):
                continue
            iid = info.get('instId')
            sp = {
                'ctVal': float(info.get('ctVal',1)),
                'lotSz': float(info.get('lotSz',1)),
                'minSz': float(info.get('minSz',1)),
                'szPrec': _precision_from_str(info.get('lotSz','1'))
            }
            allowed.append(iid)
            meta[iid] = sp
        log(f"[UNIVERSE] prefilter_total={len(allowed)}")

        tick = okx_request(settings, 'GET', '/api/v5/market/tickers', params={'instType':'SWAP'})
        vols: Dict[str, float] = {}
        for d in tick.get('data', []):
            val = d.get(settings['TOP_USDT_SORT'])
            try:
                v = float(val) if val not in (None, '') else 0.0
            except Exception:
                v = 0.0
            if v == 0.0:
                v24 = d.get('vol24h')
                last = d.get('last')
                try:
                    v = float(v24 or 0) * float(last or 0)
                except Exception:
                    v = 0.0
            vols[d['instId']] = v
        missing = len([iid for iid in allowed if iid not in vols])
        log(f"[UNIVERSE] joined_tickers={len(vols)} missing={missing}")
        zeros = sum(1 for iid in allowed if vols.get(iid,0)==0)
        log(f"[UNIVERSE] zeros_metric={zeros} nonzeros_metric={len(allowed)-zeros}")

        pairs = []
        min_vol = float(settings.get('TOP_USDT_MIN_VOL',0))
        env = str(settings.get('ENVIRONMENT','')).lower()
        for iid in allowed:
            if iid not in vols:
                continue  # missing ticker data often indicates demo-inaccessible instrument (51001)
            vol = vols.get(iid,0.0)
            if vol >= min_vol or (vol==0.0 and env=='demo' and min_vol==0):
                pairs.append((iid, vol))
        log(f"[UNIVERSE] after_filters={len(pairs)} taking_top={settings['TOP_USDT_COUNT']}")
        pairs.sort(key=lambda x: x[1], reverse=True)
        top = [p[0] for p in pairs[:settings['TOP_USDT_COUNT']]]

        if (not top) or (len(top) < settings.get('UNIVERSE_MIN_COUNT', 8)):
            manual = filter_valid_instruments(settings, settings['INSTRUMENT_LIST'])
            if settings.get('TOP_USDT_MODE','override') == 'merge':
                merged = list(dict.fromkeys(top + manual))
                top = merged
            if (not top) and settings.get('UNIVERSE_ENABLE_FALLBACK', True):
                default = ['BTC-USDT-SWAP','ETH-USDT-SWAP','SOL-USDT-SWAP','XRP-USDT-SWAP',
                           'BNB-USDT-SWAP','DOGE-USDT-SWAP','TRX-USDT-SWAP','TON-USDT-SWAP']
                default = filter_valid_instruments(settings, default)
                top = default[:max(settings.get('UNIVERSE_MIN_COUNT',8), len(default))]
            log(f"[UNIVERSE][FALLBACK] using {len(top)} instruments")

        examples = ', '.join([f"{iid}={vols.get(iid,0):.2f}" for iid in top[:3]])
        log(f"[UNIVERSE] selected {len(top)} instruments")
        if examples:
            log(f"[UNIVERSE] examples: {examples}")

        specs: Dict[str, Dict[str, float]] = {}
        missing: List[str] = []
        for iid in top:
            if iid in meta:
                specs[iid] = meta[iid]
            else:
                missing.append(iid)
        if missing:
            specs.update(prefetch_instrument_specs(settings, missing))
        return top, specs
    except Exception as e:
        log(f"[UNIVERSE_ERROR] {e}; fallback to manual list")
        insts = filter_valid_instruments(settings, settings['INSTRUMENT_LIST'])
        specs = prefetch_instrument_specs(settings, insts)
        return insts, specs

# ---------------------------
# Data & Signals (VWAP ONLY)
# ---------------------------
def parse_timeframes(tf_str: str) -> List[str]:
    out = []
    for part in tf_str.split(","):
        p = part.strip()
        if p: out.append(p)
    return out or ["30m"]

def _tf_to_minutes(tf: str) -> int:
    tf = tf.strip().lower()
    if tf.endswith('m'):
        return int(tf[:-1])
    if tf.endswith('h'):
        return int(tf[:-1]) * 60
    if tf.endswith('d'):
        return int(tf[:-1]) * 60 * 24
    try:
        return int(tf)
    except Exception:
        return 30

def get_candles_tf(settings: Dict[str, any], inst_id: str, timeframe: str, limit: int=300) -> pd.DataFrame:
    params = {'instId': inst_id, 'bar': timeframe, 'limit': limit}
    data = okx_request(settings, "GET", "/api/v5/market/candles", params=params)
    if data.get("code")!="0":
        raise RuntimeError(f"Failed to fetch candles: {data}")
    rows=[]
    for e in data.get("data",[]):
        ts=int(e[0]); dt=datetime.fromtimestamp(ts/1000, tz=timezone.utc)
        rows.append({'time':dt,'open':float(e[1]),'high':float(e[2]),'low':float(e[3]),'close':float(e[4]),'volume':float(e[5])})
    df=pd.DataFrame(rows).sort_values("time").reset_index(drop=True)
    return df

def _pivot_indices(highs: pd.Series, lows: pd.Series) -> Tuple[List[int], List[int]]:
    sh=[]; sl=[]
    for i in range(1, len(highs)-1):
        if highs.iloc[i]>highs.iloc[i-1] and highs.iloc[i]>highs.iloc[i+1]: sh.append(i)
        if lows.iloc[i]<lows.iloc[i-1] and lows.iloc[i]<lows.iloc[i+1]: sl.append(i)
    return sh, sl

def anchored_vwap_series(df: pd.DataFrame, start_idx: int) -> pd.Series:
    # Standard anchored VWAP using HLC3
    tp = (df['high'] + df['low'] + df['close']) / 3.0
    vol = df['volume']
    tv = tp * vol
    cum_tv = tv.cumsum()
    cum_v = vol.cumsum()
    base_tv = cum_tv.iloc[start_idx-1] if start_idx>0 else 0.0
    base_v = cum_v.iloc[start_idx-1] if start_idx>0 else 0.0
    vwap = (cum_tv - base_tv) / (cum_v - base_v)
    # Mask values before the anchor
    vwap_masked = vwap.copy()
    if start_idx>0:
        vwap_masked.iloc[:start_idx] = float('nan')
    return vwap_masked

def vwap_signal(df: pd.DataFrame, settings: Dict[str, any]) -> Dict[str, any]:
    highs=df['high']; lows=df['low']; closes=df['close']
    sh, sl = _pivot_indices(highs, lows)
    if len(sh)<2 or len(sl)<2:
        return {'trend': None, 'a_hi_idx': None, 'a_lo_idx': None,
                'vwap_high': None, 'vwap_low': None, 'signal': None}

    last_h, prev_h = sh[-1], sh[-2]
    last_l, prev_l = sl[-1], sl[-2]

    trend=None
    if highs.iloc[last_h]>highs.iloc[prev_h] and lows.iloc[last_l]>lows.iloc[prev_l]:
        trend='up'
    elif highs.iloc[last_h]<highs.iloc[prev_h] and lows.iloc[last_l]<lows.iloc[prev_l]:
        trend='down'

    dc_mode = bool(settings.get('DC_MODE'))
    dc_len = int(settings.get('DC_LEN',20))
    if dc_mode and len(df) >= dc_len:
        a_hi_idx = len(highs) - dc_len + int(np.argmax(highs.tail(dc_len).values))
        a_lo_idx = len(lows) - dc_len + int(np.argmin(lows.tail(dc_len).values))
    else:
        a_hi_idx = last_h
        a_lo_idx = last_l

    vwap_hi = anchored_vwap_series(df, a_hi_idx)
    vwap_lo = anchored_vwap_series(df, a_lo_idx)
    log(f"[VWAP] mode={'donchian' if dc_mode else 'pivots'} a_hi_idx={a_hi_idx} a_lo_idx={a_lo_idx}")

    sig=None
    if trend=='up':
        if len(closes)>=2 and not pd.isna(vwap_hi.iloc[-1]) and not pd.isna(vwap_hi.iloc[-2]):
            if closes.iloc[-2] <= vwap_hi.iloc[-2] and closes.iloc[-1] > vwap_hi.iloc[-1]:
                sig='buy'
    elif trend=='down':
        if len(closes)>=2 and not pd.isna(vwap_lo.iloc[-1]) and not pd.isna(vwap_lo.iloc[-2]):
            if closes.iloc[-2] >= vwap_lo.iloc[-2] and closes.iloc[-1] < vwap_lo.iloc[-1]:
                sig='sell'

    return {
        'trend': trend,
        'a_hi_idx': a_hi_idx,
        'a_lo_idx': a_lo_idx,
        'vwap_high': vwap_hi,
        'vwap_low': vwap_lo,
        'signal': sig
    }

# ---------------------------
# Order Block Filter
# ---------------------------
class ConfluenceFilter:
    """Trend + Order Block + Volume filter sitting atop VWAP logic."""

    def __init__(self, settings: Dict[str, any]):
        self.s = settings
        # Unified filter enable switch
        self.enabled = bool(settings.get("UNIFIED_FILTER_ENABLED"))
        self.ma_type = str(settings.get("OB_TREND_MA_TYPE", "HMA")).upper()
        self.ma_len = int(settings.get("OB_TREND_MA_LEN", 55))
        self.pivot_left = int(settings.get("OB_PIVOT_LEFT", 3))
        self.pivot_right = int(settings.get("OB_PIVOT_RIGHT", 3))
        self.lookback = int(settings.get("OB_LOOKBACK", 300))
        self.zone_extend = int(settings.get("OB_ZONE_EXTEND_BARS", 500))
        self.min_zone_mult = float(settings.get("OB_MIN_ZONE_SIZE_MULT", 0.25))
        self.atr_len = int(settings.get("OB_ATR_LEN", 14))
        self.mtf_enabled = bool(settings.get("OB_MTF_ENABLED"))
        self.mtf_tf = settings.get("OB_MTF_TIMEFRAME", "1h")
        self.entry_tol_pct = float(settings.get("OB_ENTRY_TOLERANCE_PCT", 0.15)) / 100.0
        self.vol_lookback = int(settings.get("OB_VOLUME_LOOKBACK", 20))
        self.vol_mult = float(settings.get("OB_VOLUME_MULT", 1.2))
        self.zones: Dict[str, List[Dict]] = {}

    # --- helpers ---
    def _wma(self, series: pd.Series, length: int) -> pd.Series:
        weights = np.arange(1, length + 1)
        return series.rolling(length).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)

    def _ma(self, series: pd.Series) -> pd.Series:
        t = self.ma_type
        l = self.ma_len
        if t == "EMA":
            return series.ewm(span=l, adjust=False).mean()
        if t == "SMA":
            return series.rolling(l).mean()
        # default HMA
        half = l // 2
        sqrt_l = int(math.sqrt(l)) or 1
        return self._wma(2 * self._wma(series, half) - self._wma(series, l), sqrt_l)

    def _atr(self, df: pd.DataFrame) -> pd.Series:
        high = df['high']; low = df['low']; close = df['close']
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs()
        ], axis=1).max(axis=1)
        return tr.rolling(self.atr_len).mean()

    def _pivot_highs(self, series: pd.Series) -> List[int]:
        left = self.pivot_left; right = self.pivot_right
        ph = []
        for i in range(left, len(series) - right):
            window = series.iloc[i - left:i + right + 1]
            if series.iloc[i] == window.max():
                ph.append(i)
        return ph

    def _pivot_lows(self, series: pd.Series) -> List[int]:
        left = self.pivot_left; right = self.pivot_right
        pl = []
        for i in range(left, len(series) - right):
            window = series.iloc[i - left:i + right + 1]
            if series.iloc[i] == window.min():
                pl.append(i)
        return pl

    def _update_zones(self, inst_key: str, df: pd.DataFrame) -> Tuple[bool, bool]:
        zones = self.zones.setdefault(inst_key, [])
        close = df['close']; open_ = df['open']; high = df['high']; low = df['low']
        atr = self._atr(df)

        # remove invalid or expired zones
        last_close = close.iloc[-1]
        for z in zones[:]:
            age = len(df) - z['start_idx']
            if age > self.zone_extend:
                zones.remove(z); continue
            if z['dir'] == 'bull' and last_close < z['low']:
                zones.remove(z); continue
            if z['dir'] == 'bear' and last_close > z['high']:
                zones.remove(z); continue

        lookback = min(self.lookback, len(df))
        subset = df.tail(lookback)
        offset = len(df) - len(subset)
        ph = self._pivot_highs(subset['high'])
        pl = self._pivot_lows(subset['low'])

        ma = self._ma(close)
        trend_up = ma.iloc[-1] > ma.iloc[-2] if len(ma) > 1 else False
        trend_down = ma.iloc[-1] < ma.iloc[-2] if len(ma) > 1 else False

        for idx in pl:
            i = idx + offset
            if not trend_up:
                continue
            j = i + 1
            if j >= len(df):
                continue
            rng = abs(close.iloc[j] - open_.iloc[j])
            atr_v = atr.iloc[j]
            if close.iloc[j] > high.iloc[i] and rng > atr_v:
                ob_idx = j - 1
                if ob_idx < 0 or close.iloc[ob_idx] >= open_.iloc[ob_idx]:
                    continue
                lower = min(open_.iloc[ob_idx], close.iloc[ob_idx])
                upper = max(open_.iloc[ob_idx], close.iloc[ob_idx])
                if (upper - lower) < self.min_zone_mult * atr.iloc[ob_idx]:
                    continue
                zones.append({'dir': 'bull', 'low': lower, 'high': upper, 'start_idx': ob_idx})

        for idx in ph:
            i = idx + offset
            if not trend_down:
                continue
            j = i + 1
            if j >= len(df):
                continue
            rng = abs(close.iloc[j] - open_.iloc[j])
            atr_v = atr.iloc[j]
            if close.iloc[j] < low.iloc[i] and rng > atr_v:
                ob_idx = j - 1
                if ob_idx < 0 or close.iloc[ob_idx] <= open_.iloc[ob_idx]:
                    continue
                lower = min(open_.iloc[ob_idx], close.iloc[ob_idx])
                upper = max(open_.iloc[ob_idx], close.iloc[ob_idx])
                if (upper - lower) < self.min_zone_mult * atr.iloc[ob_idx]:
                    continue
                zones.append({'dir': 'bear', 'low': lower, 'high': upper, 'start_idx': ob_idx})

        self.zones[inst_key] = zones
        return trend_up, trend_down

    def _allows_df(self, inst_key: str, df: pd.DataFrame, direction: str) -> Tuple[bool, str]:
        trend_up, trend_down = self._update_zones(inst_key, df)
        price = df['close'].iloc[-1]
        zones = self.zones.get(inst_key, [])
        tol_ratio = self.entry_tol_pct
        if direction == 'buy':
            if not trend_up:
                return False, 'trend_mismatch'
            for z in zones:
                if z['dir'] != 'bull':
                    continue
                tol = (z['high'] - z['low']) * tol_ratio
                if price >= z['low'] - tol and price <= z['high'] + tol:
                    break
            else:
                return False, 'not_in_OB_zone dir=bull'
        else:
            if not trend_down:
                return False, 'trend_mismatch'
            for z in zones:
                if z['dir'] != 'bear':
                    continue
                tol = (z['high'] - z['low']) * tol_ratio
                if price <= z['high'] + tol and price >= z['low'] - tol:
                    break
            else:
                return False, 'not_in_OB_zone dir=bear'

        avg_vol = df['volume'].tail(self.vol_lookback).mean()
        last_vol = df['volume'].iloc[-1]
        if last_vol < avg_vol * self.vol_mult:
            return False, f"vol {last_vol:.0f} < {self.vol_mult}*avg({avg_vol:.0f})"
        return True, 'ok'

    def allows(self, inst: str, df: pd.DataFrame, direction: str) -> bool:
        if not self.enabled:
            return True
        allowed, reason = self._allows_df(inst, df, direction)
        if not allowed:
            log(f"[FILTER] {inst} reject: {reason}")
            return False
        htf_flag = ""
        if self.mtf_enabled:
            try:
                df_htf = get_candles_tf(self.s, inst_id=inst, timeframe=self.mtf_tf, limit=300)
            except Exception:
                log(f"[FILTER] {inst} reject: htf_fetch_fail")
                return False
            ok, _ = self._allows_df(inst + "|HTF", df_htf, direction)
            if not ok:
                log(f"[FILTER] {inst} reject: htf_mismatch")
                return False
            htf_flag = "+HTF"
        log(f"[FILTER] {inst} allow dir={'buy' if direction=='buy' else 'sell'} (trend+OB+vol{htf_flag})")
        return True

# ---------------------------
# Position Management
# ---------------------------
class PositionManager:
    def __init__(self, settings: Dict[str, any], side: str, entry_price: float,
                 qty: float, ct_val: float, stop_price: float, tp_price: float,
                 atr: float, entry_time: str, entry_bar: int):
        self.s = settings
        self.side = side
        self.entry_price = entry_price
        self.qty = qty
        self.ct_val = ct_val
        self.stop_price = stop_price
        self.tp_price = tp_price
        self.initial_stop = abs(entry_price - stop_price)
        self.atr = atr
        self.entry_time = entry_time
        self.entry_bar = entry_bar
        self.entry_ordId = ''
        self.fee_entry = 0.0
        self.realized = 0.0
        self.fees = 0.0
        self.partial_done = False
        self.be_moved = False

    def update_stop(self, vwap_hi: float, vwap_lo: float, atr_now: float):
        mult = self.s['VWAP_STOP_ATR_MULT']
        if self.side == 'long':
            self.stop_price = vwap_lo - mult * atr_now
        else:
            self.stop_price = vwap_hi + mult * atr_now
        self.atr = atr_now

    def r_multiple(self, price: float) -> float:
        return abs(price - self.entry_price) / self.initial_stop if self.initial_stop>0 else 0.0

# ---------------------------
# Trading
# ---------------------------
def place_order(settings: Dict[str, any], side: str, size: str, inst_id: str,
                leverage: int=10, td_mode: str='cross', ord_type: str='market') -> Dict:
    body={'instId':inst_id,'tdMode':td_mode,'side':side,'ordType':ord_type,'sz':size,'lever':str(leverage)}
    r = okx_request(settings, "POST", "/api/v5/trade/order", body=body, private=True)
    if r.get("code")!="0":
        raise RuntimeError(f"Order failed: {r}")
    try:
        d=r.get("data",[])[0]
        if d.get("sCode") and d.get("sCode")!="0":
            raise RuntimeError(f"Order sub-error: {r}")
    except Exception:
        pass
    return r

def load_trade_history(path: str):
    import csv
    cum = 0.0; tot=win=loss=0; stats={}
    if not os.path.isfile(path): return (cum,tot,win,loss,stats)
    try:
        with open(path,"r",newline="",encoding="utf-8") as f:
            rd = csv.DictReader(f)
            for row in rd:
                try:
                    pnl=float(row.get("pnl_net", row.get("pnl",0)))
                except: pnl=0.0
                inst=row.get("instrument","")
                cum+=pnl; tot+=1; win+=1 if pnl>=0 else 0; loss+=1 if pnl<0 else 0
                if inst:
                    st = stats.setdefault(inst,{'pnl':0.0,'trades':0})
                    st['pnl']+=pnl; st['trades']+=1
    except Exception:
        pass
    return (cum,tot,win,loss,stats)

def append_trade_record(path: str, record: Dict) -> None:
    import csv
    exists = os.path.isfile(path)
    with open(path,"a",newline="",encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=list(record.keys()))
        if not exists: wr.writeheader()
        wr.writerow(record)

def save_bot_state(path: str, state: Dict) -> None:
    with open(path,"w",encoding="utf-8") as f:
        json.dump(state,f)

def load_bot_state(path: str) -> Optional[Dict]:
    if not os.path.isfile(path): return None
    try:
        with open(path,"r",encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

# ---------------------------
# Main Loop
# ---------------------------
def run_bot_vwap_only():
    s = load_settings()
    data_dir = s["DATA_DIR"]
    history_file = s["TRADE_HISTORY_FILE"]
    state_file = s["STATE_FILE"]

    instruments, specs = build_top_usdt_universe(s)
    tf_list = sorted(parse_timeframes(s["TIMEFRAME"]), key=_tf_to_minutes)
    tf_main = tf_list[-1]

    cum, tot, win, loss, inst_stats = load_trade_history(history_file)

    st = load_bot_state(state_file) or {}
    current_instrument = st.get("current_instrument")
    position_side = st.get("position_side")
    entry_price = st.get("entry_price")
    entry_size = st.get("entry_size")
    tp_price = st.get("tp_price")
    entry_ct_val = st.get("entry_ct_val")
    entry_time = st.get("entry_time")
    stop_price = st.get("stop_price")
    entry_bar = st.get("entry_bar", 0)
    entry_ordId = st.get("entry_ordId", "")
    fee_entry = st.get("fee_entry", 0.0)
    entry_fill_px = st.get("entry_fill_px")
    exec_qty = st.get("exec_qty")
    initial_stop_dist = st.get("initial_stop_dist", 0.0)
    pm = None
    if current_instrument and position_side and entry_price and entry_size:
        pm = PositionManager(s, position_side, float(entry_price), float(entry_size), float(entry_ct_val or 1), float(stop_price or entry_price), float(tp_price or entry_price), 0.0, entry_time, int(entry_bar))
        pm.entry_ordId = entry_ordId
        pm.fee_entry = float(fee_entry)
        pm.fees = pm.fee_entry

    cooldowns: Dict[str, float] = {}
    leverage_set: set = set()

    start_msg = "🚀 تم تشغيل بوت OKX (استراتيجية VWAP Price Channel فقط)\n" +                 f"الإطار الزمني: {tf_main}\n" +                 f"عدد الأزواج: {len(instruments)}"
    log(start_msg); send_telegram(s, start_msg)
    log(f"[START] VWAP mode={'donchian' if s['DC_MODE'] else 'pivots'} | OB Filter={'ON' if s['UNIFIED_FILTER_ENABLED'] else 'OFF'}")
    scan_okx(s)

    last_report = now_utc()
    hour_trades=0; hour_profit=0.0; hour_wins=0; hour_losses=0

    rr = float(s["REWARD_RISK_RATIO"])
    margin_per_trade = float(s.get("MARGIN_PER_TRADE_USDT", 90.0))
    confluence_filter = ConfluenceFilter(s)

    while True:
        try:
            try:
                bal = get_account_balance(s, 'USDT')
            except Exception:
                bal = 0.0

            if position_side is None:
                opened=False
                for inst in instruments:
                    if inst in cooldowns and time.time() - cooldowns[inst] < s['COOLDOWN_SEC']:
                        continue
                    # Fetch data
                    try:
                        df = get_candles_tf(s, inst_id=inst, timeframe=tf_main, limit=300)
                    except Exception as ex:
                        log(f"[SKIP] فشل الحصول على البيانات للزوج {inst} ({tf_main}): {ex}")
                        continue

                    sig = vwap_signal(df, s)
                    trend = sig['trend']
                    vwap_hi = sig['vwap_high']
                    vwap_lo = sig['vwap_low']
                    direction = sig['signal']
                    if direction is None or trend is None:
                        continue

                    if not confluence_filter.allows(inst, df, direction):
                        continue

                    price = df['close'].iloc[-1]
                    spec = specs.get(inst)
                    if not spec:
                        continue
                    ct = spec['ctVal']; lot = spec['lotSz']; min_sz = spec['minSz']; prec = spec['szPrec']

                    atr_now = calc_atr(df, s['OB_ATR_LEN']).iloc[-1]
                    if direction=='buy' and trend=='up':
                        active_stop = vwap_lo.iloc[-1] - s['VWAP_STOP_ATR_MULT']*atr_now
                        stop_dist = price - active_stop
                    elif direction=='sell' and trend=='down':
                        active_stop = vwap_hi.iloc[-1] + s['VWAP_STOP_ATR_MULT']*atr_now
                        stop_dist = active_stop - price
                    else:
                        continue

                    if pd.isna(active_stop) or stop_dist <= 0:
                        continue
                    if stop_dist < s['MIN_STOP_ATR']*atr_now:
                        continue

                    # Position sizing by fixed margin with precheck
                    notional_target = margin_per_trade * s["LEVERAGE"]
                    contracts_raw = notional_target / (price * ct)
                    qty = math.floor(contracts_raw / lot) * lot
                    if qty < min_sz:
                        if s["AUTO_ADJUST_MARGIN"]:
                            qty = min_sz
                        else:
                            continue
                    notional = qty * price * ct
                    fee_buffer = notional * s["FEE_RATE"] * 2
                    required_margin = notional / s["LEVERAGE"] + fee_buffer
                    log(f"[SIZING] price={price:.4f} ctVal={ct} lotSz={lot} minSz={min_sz} contracts_raw={contracts_raw:.4f} qty_final={qty}")
                    if bal < required_margin:
                        log(f"[PRECHECK_FAIL] avail={bal:.2f} required={required_margin:.2f} notional={notional:.2f} qty={qty} leverage={s['LEVERAGE']}")
                        continue
                    size_str = f"{qty:.{prec}f}".rstrip('0').rstrip('.')
                    margin = notional / s["LEVERAGE"]

                    side = 'buy' if direction=='buy' else 'sell'
                    try:
                        response = place_order(s, side=side, size=size_str, inst_id=inst, leverage=s['LEVERAGE'], td_mode='cross', ord_type='market')
                    except Exception as ex:
                        log(f"[ORDER_ERROR] {ex}")
                        continue

                    try:
                        od = response.get('data', [])[0]
                        entry_ordId = od.get('ordId', '')
                        log(f"[ORDER_RESPONSE] code={response.get('code')} sCode={od.get('sCode')} sMsg={od.get('sMsg')} ordId={entry_ordId}")
                    except Exception:
                        entry_ordId = ''
                        log(f"[ORDER_RESPONSE] {response}")

                    fill=None
                    for _ in range(3):
                        fill = get_order_fill(s, inst, entry_ordId)
                        if fill: break
                        time.sleep(1)
                    if fill:
                        entry_fill_px = fill['fillPx']
                        exec_qty = fill['fillSz']
                        fee_entry = abs(fill.get('fee', 0.0))
                    else:
                        entry_fill_px = price
                        exec_qty = qty
                        fee_entry = s['FEE_RATE'] * entry_fill_px * exec_qty * ct

                    current_instrument = inst
                    position_side = 'long' if direction=='buy' else 'short'
                    entry_price = entry_fill_px
                    entry_size = size_str
                    entry_ct_val = ct
                    entry_time = now_utc().isoformat()
                    notional = exec_qty * entry_fill_px * ct
                    margin = notional / s['LEVERAGE']

                    tp_price = (entry_price + stop_dist*rr) if position_side=='long' else (entry_price - stop_dist*rr)

                    save_bot_state(state_file, {
                        'current_instrument': current_instrument,
                        'position_side': position_side,
                        'entry_price': entry_price,
                        'entry_size': entry_size,
                        'tp_price': tp_price,
                        'entry_ct_val': entry_ct_val,
                        'entry_time': entry_time,
                        'stop_price': active_stop,
                        'entry_bar': len(df),
                        'entry_ordId': entry_ordId,
                        'entry_fill_px': entry_price,
                        'exec_qty': exec_qty,
                        'fee_entry': fee_entry,
                        'initial_stop_dist': stop_dist
                    })

                    try:
                        balance_after = get_account_balance(s,'USDT')
                    except Exception:
                        balance_after = bal

                    entry_dir = 'شراء' if position_side=='long' else 'بيع'
                    send_telegram(s,
                        ("📈" if position_side=='long' else "📉") +
                        f" دخول صفقة {entry_dir} على <b>{inst}</b>\n"
                        f"TF: {tf_main}\n"
                        f"الكمية: {exec_qty} | سعر الدخول (fill): {entry_price:.4f}\n"
                        f"الهامش: {margin:.2f} | النوتيونال: {notional:.2f} | الرافعة: {s['LEVERAGE']}x\n"
                        f"SL (مبدئي/فعّال): {active_stop:.4f} | TP: {tp_price:.4f}\n"
                        f"ordId: {entry_ordId}"
                    )
                    log(f"[ENTRY] {entry_dir} {inst}: qty {exec_qty}, px {entry_price:.4f}")
                    opened=True
                    break

                if not opened:
                    if int(s.get("VERBOSE",1))>0:
                        log("[SKIP] لا توجد إشارة VWAP لهذه الدورة")

            else:
                # Manage open position using dynamic stop at active anchored VWAP
                try:
                    df = get_candles_tf(s, inst_id=current_instrument, timeframe=tf_main, limit=300)
                except Exception as ex:
                    log(f"[ERROR] فشل جلب البيانات للزوج {current_instrument}: {ex}")
                    time.sleep(60); continue

                sig = vwap_signal(df, s)
                vwap_hi = sig['vwap_high']
                vwap_lo = sig['vwap_low']

                atr_now = calc_atr(df, s['OB_ATR_LEN']).iloc[-1]
                atr_prev = calc_atr(df, s['OB_ATR_LEN']).iloc[-2]
                mult = s['VWAP_STOP_ATR_MULT']
                current_price = df['close'].iloc[-1]
                close_prev = df['close'].iloc[-2]
                gap = abs(current_price - close_prev)

                if position_side=='long':
                    active_stop = vwap_lo.iloc[-1] - mult*atr_now
                    stop_prev = vwap_lo.iloc[-2] - mult*atr_prev
                    tp_hit = current_price >= tp_price if tp_price is not None else False
                    sl_confirm = (current_price < active_stop) and (close_prev >= stop_prev)
                    gap_exit = gap > atr_now and current_price < active_stop
                else:
                    active_stop = vwap_hi.iloc[-1] + mult*atr_now
                    stop_prev = vwap_hi.iloc[-2] + mult*atr_prev
                    tp_hit = current_price <= tp_price if tp_price is not None else False
                    sl_confirm = (current_price > active_stop) and (close_prev <= stop_prev)
                    gap_exit = gap > atr_now and current_price > active_stop

                hold_bars = len(df) - int(entry_bar or 0)
                if hold_bars < s['MIN_HOLD_BARS'] and not gap_exit:
                    sl_confirm = False

                sl_hit = gap_exit or sl_confirm

                log(f"[PM] price={current_price:.4f} atr={atr_now:.4f} stop={active_stop:.4f} tp={tp_price:.4f}")

                if tp_hit or sl_hit:
                    closing_side = 'sell' if position_side=='long' else 'buy'
                    qty_close = float(exec_qty or entry_size)
                    prec = specs.get(current_instrument, {}).get('szPrec', 0)
                    size_close = f"{qty_close:.{prec}f}".rstrip('0').rstrip('.')
                    try:
                        response = place_order(s, side=closing_side, size=size_close, inst_id=current_instrument, leverage=10, td_mode='cross', ord_type='market')
                    except Exception as ex:
                        log(f"[ORDER_CLOSE_ERROR] {ex}")
                        time.sleep(60); continue

                    try:
                        od = response.get('data', [])[0]
                        exit_ordId = od.get('ordId', '')
                    except Exception:
                        exit_ordId = ''

                    fill=None
                    for _ in range(3):
                        fill = get_order_fill(s, current_instrument, exit_ordId)
                        if fill: break
                        time.sleep(1)
                    if fill:
                        exit_fill_px = fill['fillPx']
                        fee_exit = abs(fill.get('fee', 0.0))
                    else:
                        exit_fill_px = current_price
                        fee_exit = s['FEE_RATE'] * exit_fill_px * qty_close * (entry_ct_val or 1.0)

                    ct = entry_ct_val if entry_ct_val and entry_ct_val>0 else 1.0
                    gross = (exit_fill_px - entry_price)*qty_close*ct if position_side=='long' else (entry_price - exit_fill_px)*qty_close*ct
                    fees = fee_entry + fee_exit
                    pnl_net = gross - fees
                    cum += pnl_net; tot += 1
                    if pnl_net>=0: win += 1
                    else: loss += 1
                    hour_trades += 1; hour_profit += pnl_net
                    if pnl_net>=0: hour_wins += 1
                    else: hour_losses += 1

                    exit_time = now_utc().isoformat()
                    hold_sec = int((now_utc() - datetime.fromisoformat(entry_time)).total_seconds()) if entry_time else 0
                    r_realized = abs((exit_fill_px - entry_price) / initial_stop_dist) if initial_stop_dist>0 else 0.0
                    reason = "TP" if tp_hit else ("SL-gap" if gap_exit else "SL-confirm")

                    rec = {
                        'timestamp_entry': entry_time,
                        'timestamp_exit': exit_time,
                        'tf': tf_main,
                        'instrument': current_instrument,
                        'side': position_side,
                        'entry_ordId': entry_ordId,
                        'exit_ordId': exit_ordId,
                        'entry_fill_px': entry_price,
                        'exit_fill_px': exit_fill_px,
                        'exec_qty': qty_close,
                        'ct_val': entry_ct_val,
                        'initial_stop': initial_stop_dist,
                        'exit_reason': reason,
                        'gross_pnl': gross,
                        'fees': fees,
                        'pnl_net': pnl_net,
                        'r_realized': r_realized,
                        'hold_bars': hold_bars,
                        'hold_time_sec': hold_sec,
                        'notional_entry': entry_price * qty_close * (entry_ct_val or 1.0),
                        'notional_exit': exit_fill_px * qty_close * (entry_ct_val or 1.0),
                        'leverage': s['LEVERAGE'],
                    }
                    append_trade_record(history_file, rec)
                    try:
                        if os.path.isfile(state_file): os.remove(state_file)
                    except Exception: pass

                    send_telegram(s,
                        f"✅ إغلاق {('شراء' if position_side=='long' else 'بيع')} <b>{current_instrument}</b> — {reason}\n"
                        f"سعر الخروج (fill): {exit_fill_px:.4f}\n"
                        f"SL/TP عند الخروج: SL={active_stop:.4f} | TP={tp_price:.4f}\n"
                        f"PnL: {fmt_signed(pnl_net)} USDT  |  G:{fmt_signed(gross)}  F:{fees:.4f}\n"
                        f"R-realized: {r_realized:.2f}R  |  مدة الاحتفاظ: {hold_bars} بار / {hold_sec}s\n"
                        f"ordId(entry): {entry_ordId} | ordId(exit): {exit_ordId}\n"
                        f"الإجمالي: {cum:.4f} USDT"
                    )
                    log(f"[EXIT] {('شراء' if position_side=='long' else 'بيع')} {current_instrument}: px {exit_fill_px:.4f}, PnL {pnl_net:.4f}")

                    current_instrument=None; position_side=None
                    entry_price=entry_size=tp_price=entry_ct_val=entry_time=None

            # Hourly report
            now = now_utc()
            if now - last_report >= timedelta(hours=1):
                report_time = now.strftime('%Y-%m-%d %H:%M:%S')
                msg = (f"🕒 تقرير ساعي عند {report_time} UTC\n"
                       f"نتائج الساعة الماضية:\n"
                       f"عدد الصفقات: {hour_trades}\n"
                       f"الربح/الخسارة: {hour_profit:.4f} USDT (رابحة: {hour_wins}, خاسرة: {hour_losses})\n"
                       f"منذ تشغيل السكربت:\n"
                       f"إجمالي الصفقات المنفذة: {tot}\n"
                       f"الصفقات الرابحة: {win}\n"
                       f"الصفقات الخاسرة: {loss}\n"
                       f"الإجمالي الكلي: {cum:.4f} USDT")
                log(f"[REPORT] {msg}")
                send_telegram(s, msg)
                hour_trades=0; hour_profit=0.0; hour_wins=0; hour_losses=0
                last_report = now

            time.sleep(60)
        except Exception as e:
            log(f"[ERROR] {e}")
            send_telegram(s, f"⚠️ حدث خطأ: {e}")
            time.sleep(60)

if __name__=="__main__":
    try:
        run_bot_vwap_only()
    except KeyboardInterrupt:
        log("Bot stopped by user")
