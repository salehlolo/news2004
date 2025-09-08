
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
    s["FEE_RATE"] = _f("FEE_RATE", 0.0006)
    s["REWARD_RISK_RATIO"] = _f("REWARD_RISK_RATIO", 5.0)
    s["MARGIN_PER_TRADE_USDT"] = _f("MARGIN_PER_TRADE_USDT", 90.0)

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

    return s

def sign_request(secret_key: str, timestamp: str, method: str, request_path: str, body: str) -> str:
    message = f"{timestamp}{method}{request_path}{body}"
    mac = hmac.new(secret_key.encode("utf-8"), msg=message.encode("utf-8"), digestmod="sha256")
    return base64.b64encode(mac.digest()).decode()

def okx_request(settings: Dict[str, any], method: str, path: str, params: Optional[Dict]=None, body: Optional[Dict]=None, private: bool=False) -> Dict:
    url = settings["BASE_URL"] + path
    headers = {'Content-Type': 'application/json', 'x-simulated-trading': '1'}
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

def get_instrument_specs(settings: Dict[str, any], inst_id: str) -> Tuple[float,float]:
    r = okx_request(settings, "GET", "/api/v5/public/instruments", params={'instType':'SWAP','instId':inst_id})
    if r.get("code")!="0" or not r.get("data"):
        return (1.0, 1.0)
    info = r["data"][0]
    lot = info.get("lotSz") or info.get("lotSize") or info.get("minSz")
    ct = info.get("ctVal")
    try:
        lot_f = float(lot) if lot is not None else 1.0
    except Exception:
        lot_f = 1.0
    try:
        ct_f = float(ct) if ct is not None else 1.0
    except Exception:
        ct_f = 1.0
    return (lot_f, ct_f)

def prefetch_instrument_specs(settings: Dict[str, any], inst_list: List[str]) -> Dict[str, Tuple[float,float]]:
    spec = {i:(1.0,1.0) for i in inst_list}
    try:
        r = okx_request(settings, "GET", "/api/v5/public/instruments", params={'instType':'SWAP'})
        if r.get("code")!="0":
            return spec
        for info in r.get("data",[]):
            iid = info.get("instId")
            if iid in spec:
                lot = info.get("lotSz") or info.get("lotSize") or info.get("minSz")
                ct = info.get("ctVal")
                try: lot_f = float(lot) if lot is not None else 1.0
                except: lot_f = 1.0
                try: ct_f = float(ct) if ct is not None else 1.0
                except: ct_f = 1.0
                spec[iid] = (lot_f, ct_f)
        return spec
    except Exception:
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

def vwap_signal(df: pd.DataFrame) -> Dict[str, any]:
    highs=df['high']; lows=df['low']; closes=df['close']
    sh, sl = _pivot_indices(highs, lows)
    if len(sh)<2 or len(sl)<2:
        return {'trend': None, 'a_hi_idx': None, 'a_lo_idx': None,
                'vwap_high': None, 'vwap_low': None, 'signal': None}

    last_h, prev_h = sh[-1], sh[-2]
    last_l, prev_l = sl[-1], sl[-2]

    # Simple trend test (higher highs & higher lows vs lower highs & lower lows)
    trend=None
    if highs.iloc[last_h]>highs.iloc[prev_h] and lows.iloc[last_l]>lows.iloc[prev_l]:
        trend='up'
    elif highs.iloc[last_h]<highs.iloc[prev_h] and lows.iloc[last_l]<lows.iloc[prev_l]:
        trend='down'

    # Anchors
    a_hi_idx = last_h
    a_lo_idx = last_l

    vwap_hi = anchored_vwap_series(df, a_hi_idx)  # anchored at last swing high
    vwap_lo = anchored_vwap_series(df, a_lo_idx)  # anchored at last swing low

    # Entry logic (continuation breakout of the "opposite" VWAP)
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

    def _allows_df(self, inst_key: str, df: pd.DataFrame, direction: str) -> bool:
        trend_up, trend_down = self._update_zones(inst_key, df)
        price = df['close'].iloc[-1]
        zones = self.zones.get(inst_key, [])
        tol_ratio = self.entry_tol_pct
        if direction == 'buy':
            if not trend_up:
                return False
            for z in zones:
                if z['dir'] != 'bull':
                    continue
                tol = (z['high'] - z['low']) * tol_ratio
                if price >= z['low'] - tol and price <= z['high'] + tol:
                    return True
            return False
        else:
            if not trend_down:
                return False
            for z in zones:
                if z['dir'] != 'bear':
                    continue
                tol = (z['high'] - z['low']) * tol_ratio
                if price <= z['high'] + tol and price >= z['low'] - tol:
                    return True
            return False

    def allows(self, inst: str, df: pd.DataFrame, direction: str) -> bool:
        if not self.enabled:
            return True
        if not self._allows_df(inst, df, direction):
            return False
        # Always apply volume/ liquidity filter
        avg_vol = df['volume'].tail(self.vol_lookback).mean()
        if df['volume'].iloc[-1] < avg_vol * self.vol_mult:
            return False
        if self.mtf_enabled:
            try:
                df_htf = get_candles_tf(self.s, inst_id=inst, timeframe=self.mtf_tf, limit=300)
            except Exception:
                return False
            if not self._allows_df(inst + "|HTF", df_htf, direction):
                return False
        return True

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
                    pnl=float(row.get("pnl",0))
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

    instruments = filter_valid_instruments(s, s["INSTRUMENT_LIST"])
    tf_list = sorted(parse_timeframes(s["TIMEFRAME"]), key=_tf_to_minutes)
    # Use a single main TF (highest for stability)
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

    start_msg = "🚀 تم تشغيل بوت OKX (استراتيجية VWAP Price Channel فقط)\n" +                 f"الإطار الزمني: {tf_main}\n" +                 f"عدد الأزواج: {len(instruments)}"
    log(start_msg); send_telegram(s, start_msg)
    scan_okx(s)

    last_report = now_utc()
    hour_trades=0; hour_profit=0.0; hour_wins=0; hour_losses=0

    specs = prefetch_instrument_specs(s, instruments)
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
                    # Fetch data
                    try:
                        df = get_candles_tf(s, inst_id=inst, timeframe=tf_main, limit=300)
                    except Exception as ex:
                        log(f"[SKIP] فشل الحصول على البيانات للزوج {inst} ({tf_main}): {ex}")
                        continue

                    sig = vwap_signal(df)
                    trend = sig['trend']
                    vwap_hi = sig['vwap_high']
                    vwap_lo = sig['vwap_low']
                    direction = sig['signal']
                    if direction is None or trend is None:
                        continue

                    if not confluence_filter.allows(inst, df, direction):
                        continue

                    price = df['close'].iloc[-1]
                    lot, ct = specs.get(inst,(1.0,1.0))

                    # Determine active stop (trailing at active anchored VWAP) & initial stop distance
                    if direction=='buy' and trend=='up':
                        active_stop = vwap_lo.iloc[-1]  # active VWAP is from last swing low
                    elif direction=='sell' and trend=='down':
                        active_stop = vwap_hi.iloc[-1]  # active VWAP is from last swing high
                    else:
                        # Ignore mixed cases (shouldn't happen with logic above)
                        continue

                    if pd.isna(active_stop):
                        continue

                    stop_dist = abs(price - active_stop)
                    if stop_dist<=0:
                        continue

                    # Position sizing by fixed margin
                    lev = 10
                    notional_target = margin_per_trade * lev
                    lot = lot if lot and lot>0 else 1.0
                    contracts_raw = notional_target / (price * ct)
                    units_int = max(1, int(contracts_raw / lot))
                    contracts = units_int * lot
                    size_str = f"{contracts:.8f}".rstrip('0').rstrip('.')
                    notional = price * contracts * ct
                    margin = notional / lev

                    # Place entry
                    side = 'buy' if direction=='buy' else 'sell'
                    try:
                        response = place_order(s, side=side, size=size_str, inst_id=inst, leverage=lev, td_mode='cross', ord_type='market')
                    except Exception as ex:
                        log(f"[ORDER_ERROR] {ex}")
                        continue

                    current_instrument = inst
                    position_side = 'long' if direction=='buy' else 'short'
                    entry_price = price; entry_size = size_str
                    entry_ct_val = ct; entry_time = now_utc().isoformat()

                    # Fixed TP based on initial stop distance
                    tp_price = (entry_price + stop_dist*rr) if position_side=='long' else (entry_price - stop_dist*rr)

                    save_bot_state(state_file, {
                        'current_instrument': current_instrument,
                        'position_side': position_side,
                        'entry_price': entry_price,
                        'entry_size': entry_size,
                        'tp_price': tp_price,
                        'entry_ct_val': entry_ct_val,
                        'entry_time': entry_time
                    })

                    try:
                        od=response.get('data',[])[0]
                        log(f"[ORDER_RESPONSE] code={response.get('code')} sCode={od.get('sCode')} sMsg={od.get('sMsg')} ordId={od.get('ordId')}")
                    except Exception:
                        log(f"[ORDER_RESPONSE] {response}")
                    try:
                        balance_after = get_account_balance(s,'USDT')
                    except Exception:
                        balance_after = bal

                    entry_dir = 'شراء' if position_side=='long' else 'بيع'
                    send_telegram(s,
                        ( "📈" if position_side=='long' else "📉" ) +
                        f" دخول صفقة {entry_dir} على <b>{inst}</b>\n"
                        f"TF: {tf_main}\n"
                        f"القيمة الإسمية: {notional:.2f} | الهامش: {margin:.2f} | الرصيد: {balance_after:.2f}\n"
                        f"الكمية: {entry_size} | سعر الدخول: {entry_price:.4f}\n"
                        f"⏫ TP: {tp_price:.4f} | ⏬ وقف متغير: Anchored VWAP ({'low' if position_side=='long' else 'high'})"
                    )
                    log(f"[ENTRY] {entry_dir} {inst}: qty {entry_size}, px {entry_price:.4f}")
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

                sig = vwap_signal(df)
                vwap_hi = sig['vwap_high']
                vwap_lo = sig['vwap_low']

                current_price = df['close'].iloc[-1]
                # Dynamic stop recalculated each loop
                if position_side=='long':
                    active_stop = vwap_lo.iloc[-1]
                    tp_hit = current_price >= tp_price if tp_price is not None else False
                    sl_hit = (not pd.isna(active_stop)) and (current_price <= active_stop)
                else:
                    active_stop = vwap_hi.iloc[-1]
                    tp_hit = current_price <= tp_price if tp_price is not None else False
                    sl_hit = (not pd.isna(active_stop)) and (current_price >= active_stop)

                if tp_hit or sl_hit:
                    px = current_price
                    closing_side = 'sell' if position_side=='long' else 'buy'
                    try:
                        response = place_order(s, side=closing_side, size=entry_size, inst_id=current_instrument, leverage=10, td_mode='cross', ord_type='market')
                    except Exception as ex:
                        log(f"[ORDER_CLOSE_ERROR] {ex}")
                        time.sleep(60); continue

                    ct = entry_ct_val if entry_ct_val and entry_ct_val>0 else 1.0
                    qty = float(entry_size)
                    gross = (px - entry_price)*qty*ct if position_side=='long' else (entry_price - px)*qty*ct
                    fee_rate = float(s.get('FEE_RATE',0.0006))
                    entry_notional = entry_price*qty*ct; exit_notional = px*qty*ct
                    fees = fee_rate*(entry_notional + exit_notional)
                    pnl = gross - fees
                    cum += pnl; tot += 1
                    if pnl>=0: win += 1
                    else: loss += 1
                    hour_trades += 1; hour_profit += pnl
                    if pnl>=0: hour_wins += 1
                    else: hour_losses += 1

                    exit_time = now_utc().isoformat()
                    reason = "تحقيق الهدف" if tp_hit else "كسر Anchored VWAP (وقف متحرك)"
                    rec = {
                        'timestamp_entry': entry_time, 'timestamp_exit': exit_time,
                        'instrument': current_instrument, 'side': position_side,
                        'entry_price': entry_price, 'exit_price': px,
                        'contracts': entry_size, 'ct_val': entry_ct_val,
                        'gross_pnl': gross, 'fees': fees, 'pnl': pnl
                    }
                    append_trade_record(history_file, rec)
                    try:
                        if os.path.isfile(state_file): os.remove(state_file)
                    except Exception: pass

                    profit_text = "ربح" if pnl>=0 else "خسارة"
                    send_telegram(s,
                        f"✅ إغلاق صفقة {('شراء' if position_side=='long' else 'بيع')} على <b>{current_instrument}</b>\n"
                        f"سبب الخروج: {reason}\n"
                        f"سعر الخروج: {px:.4f} USDT\n"
                        f"نتيجة الصفقة: {profit_text} {abs(pnl):.4f} USDT\n"
                        f"الإجمالي حتى الآن: {cum:.4f} USDT")
                    log(f"[EXIT] {('شراء' if position_side=='long' else 'بيع')} {current_instrument}: px {px:.4f}, PnL {pnl:.4f}")

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
