# ============================================================
# NIFTY PULSE AUTO — GitHub Actions Runner
# Python script that replaces Google Apps Script
# Fetches all data, calls Gemini AI, writes to Google Sheet
# Runs at 9:00 AM and 3:30 PM IST via GitHub Actions
# ============================================================

import os
import json
import time
import requests
import datetime
import pytz
import gspread
from google.oauth2.service_account import Credentials
from bs4 import BeautifulSoup

# ─────────────────────────────────────────────
# CONFIG — all secrets come from GitHub environment
# ─────────────────────────────────────────────
GEMINI_API_KEY    = os.environ.get("GEMINI_API_KEY", "")
GOOGLE_SHEET_ID   = os.environ.get("GOOGLE_SHEET_ID", "")
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON", "")
RUN_TYPE          = os.environ.get("RUN_TYPE", "MORNING")  # MORNING or AFTERNOON

# Model priority list — tries each in order until one works
GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash-lite",
]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", GEMINI_MODELS[0])
GEMINI_BASE  = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"

IST = pytz.timezone("Asia/Kolkata")

INDICES = {
    "NIFTY50":    {"yahoo": "^NSEI",               "display": "Nifty 50",          "opt": "NIFTY",
                   "yahoo_fallback": "NIFTY50.NS"},
    "BANKNIFTY":  {"yahoo": "^NSEBANK",            "display": "Bank Nifty",        "opt": "BANKNIFTY",
                   "yahoo_fallback": "BANKNIFTY.NS"},
    "MIDCAP50":   {"yahoo": "^NSEMDCP50",          "display": "Nifty Midcap 50",   "opt": "NIFTY",
                   "yahoo_fallback": "NIFTY_MID_SELECT.NS"},
    "SMALLCAP50": {"yahoo": "^NSEMDCP50",           "display": "Nifty Smallcap 50", "opt": "NIFTY",
                   "yahoo_fallback": "NIFTYSMALLCAP50.NS"},
}

NEWS_FEEDS = [
    {"url": "https://news.google.com/rss/search?q=nifty+sensex+NSE+BSE+stock+market&hl=en-IN&gl=IN&ceid=IN:en",        "label": "India Market"},
    {"url": "https://news.google.com/rss/search?q=RBI+monetary+policy+india+inflation+GDP&hl=en-IN&gl=IN&ceid=IN:en", "label": "India Macro"},
    {"url": "https://news.google.com/rss/search?q=US+federal+reserve+interest+rate+FOMC&hl=en-US&gl=US&ceid=US:en",   "label": "US Fed"},
    {"url": "https://news.google.com/rss/search?q=FII+FPI+foreign+investor+india+equity&hl=en-IN&gl=IN&ceid=IN:en",   "label": "FII Flows"},
    {"url": "https://news.google.com/rss/search?q=crude+oil+OPEC+commodities+gold+rupee&hl=en-IN&gl=IN&ceid=IN:en",   "label": "Commodities"},
]

POS_KW = [
    {"w": 2, "k": ["rate cut","repo cut","stimulus","buyback","FII buying","inflow","upgrade",
                   "outperform","beat estimates","record high","breakout","oversold","accumulate",
                   "strong GDP","surplus","trade deal","ceasefire","dovish","monsoon normal",
                   "GST collection","capex boost","PLI scheme"]},
    {"w": 1, "k": ["rally","surge","gain","rise","bullish","growth","recovery","rebound",
                   "positive","profit","boost","buying","invest","expand","strong"]},
]
NEG_KW = [
    {"w": 2, "k": ["rate hike","repo hike","FII selling","outflow","downgrade","underperform",
                   "miss estimates","recession","stagflation","default","trade war","tariff",
                   "sanctions","overbought","inflation surge","current account deficit",
                   "rupee fall","oil spike","geopolitical tension"]},
    {"w": 1, "k": ["fall","drop","decline","down","bearish","loss","weak","crash","sell",
                   "risk","uncertainty","war","tension","debt","concern","negative"]},
]

WEIGHTS = {"GIFT": 15, "US": 12, "ASIA": 8, "FII_FUT": 15,
           "PCR": 12, "VIX": 10, "TECH": 10, "NEWS": 12, "MOM": 6}
BULL_THR = 57
BEAR_THR = 43

# ─────────────────────────────────────────────
# GOOGLE SHEETS CONNECTION
# ─────────────────────────────────────────────
def connect_sheet():
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(GOOGLE_SHEET_ID)

def get_or_create_sheet(wb, name):
    try:
        return wb.worksheet(name)
    except:
        return wb.add_worksheet(title=name, rows=1000, cols=30)

# ─────────────────────────────────────────────
# DATA FETCHING
# ─────────────────────────────────────────────
YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com",
}
NSE_SESSION = requests.Session()

def init_nse_session():
    """Initialize NSE session with cookies — works from GitHub IPs"""
    try:
        NSE_SESSION.headers.update(NSE_HEADERS)
        r = NSE_SESSION.get("https://www.nseindia.com", timeout=10)
        time.sleep(1)
        return r.status_code == 200
    except Exception as e:
        print(f"NSE session init failed: {e}")
        return False

def fetch_yahoo(ticker, period="6mo", interval="1d"):
    """Fetch OHLCV data from Yahoo Finance"""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        params = {"range": period, "interval": interval, "includePrePost": "false"}
        r = requests.get(url, headers=YAHOO_HEADERS, params=params, timeout=15)
        data = r.json()
        result = data["chart"]["result"][0]
        quote = result["indicators"]["quote"][0]
        return {
            "timestamps": result.get("timestamp", []),
            "closes":  [x for x in (quote.get("close")  or []) if x is not None],
            "opens":   [x for x in (quote.get("open")   or []) if x is not None],
            "highs":   [x for x in (quote.get("high")   or []) if x is not None],
            "lows":    [x for x in (quote.get("low")    or []) if x is not None],
            "volumes": [x for x in (quote.get("volume") or []) if x is not None],
        }
    except Exception as e:
        print(f"Yahoo fetch error {ticker}: {e}")
        return None

def fetch_latest(ticker):
    """Get latest price and change % from Yahoo"""
    try:
        d = fetch_yahoo(ticker, "5d", "1d")
        if not d or len(d["closes"]) < 2:
            return None
        c = d["closes"]
        return {
            "price": c[-1],
            "prev": c[-2],
            "change_pct": ((c[-1] - c[-2]) / c[-2]) * 100
        }
    except Exception as e:
        print(f"Latest price error {ticker}: {e}")
        return None

def fetch_vix():
    """Fetch India VIX — try NSE first, fallback to Yahoo"""
    try:
        r = NSE_SESSION.get("https://www.nseindia.com/api/allIndices", timeout=10)
        if r.status_code == 200:
            data = r.json()
            vix = next((d for d in data["data"] if d["index"] == "INDIA VIX"), None)
            if vix:
                return {"vix": float(vix["last"]), "vix_change": float(vix["percentChange"])}
    except Exception as e:
        print(f"NSE VIX error: {e}")
    # Yahoo fallback
    v = fetch_latest("^INDIAVIX")
    return {"vix": v["price"], "vix_change": v["change_pct"]} if v else {"vix": 15.0, "vix_change": 0.0}

def fetch_fii_dii():
    """Fetch FII/DII cash flows from NSE"""
    try:
        r = NSE_SESSION.get("https://www.nseindia.com/api/fiidiiTradeReact", timeout=10)
        if r.status_code != 200:
            print(f"FII/DII HTTP error: {r.status_code}")
            return None
        data = r.json()
        fii = next((d for d in data if d.get("category","").startswith("FII")), None)
        dii = next((d for d in data if d.get("category","").startswith("DII")), None)

        def get_net(row):
            # NSE changes field names occasionally — try all known variants
            for field in ["netPurchasesSales","net_purchases_sales","netPurchSales","netPurchase","net"]:
                if row and field in row:
                    try: return float(row[field])
                    except: continue
            # Last resort: bought - sold
            if row:
                try:
                    bought = float(row.get("buyValue", row.get("bought", row.get("purchase", 0))))
                    sold   = float(row.get("sellValue", row.get("sold", row.get("sales", 0))))
                    return bought - sold
                except: pass
            return 0

        return {
            "fii_net": get_net(fii),
            "dii_net": get_net(dii),
            "date": fii.get("date","—") if fii else "—"
        }
    except Exception as e:
        print(f"FII/DII error: {e}")
        return None

def fetch_participant_oi():
    """Fetch FII futures positioning from NSE"""
    try:
        r = NSE_SESSION.get(
            "https://www.nseindia.com/api/participantStatsEquity?type=future",
            timeout=10
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not data.get("data"):
            return None
        fii = next((d for d in data["data"]
                    if "FII" in d.get("clientType","") or "FPI" in d.get("clientType","")), None)
        if not fii:
            return None
        return {
            "fii_net_futures": float(fii.get("futureOI_Long", 0)) - float(fii.get("futureOI_Short", 0)),
            "date": data.get("date", "—")
        }
    except Exception as e:
        print(f"Participant OI error: {e}")
        return None

def fetch_options(symbol):
    """Fetch options chain — PCR and OI velocity"""
    try:
        r = NSE_SESSION.get(
            f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}",
            timeout=15
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not data.get("records") or not data["records"].get("data"):
            return None

        put_oi = call_oi = put_chg = call_chg = 0
        for row in data["records"]["data"]:
            if row.get("PE"):
                put_oi  += row["PE"].get("openInterest", 0)
                put_chg += row["PE"].get("changeinOpenInterest", 0)
            if row.get("CE"):
                call_oi  += row["CE"].get("openInterest", 0)
                call_chg += row["CE"].get("changeinOpenInterest", 0)

        pcr = put_oi / call_oi if call_oi > 0 else 1.0
        oi_vel = (put_chg - call_chg) / (call_oi + put_oi) * 100 if (call_oi + put_oi) > 0 else 0

        return {"pcr": round(pcr, 3), "oi_velocity": round(oi_vel, 3)}
    except Exception as e:
        print(f"Options error {symbol}: {e}")
        return None

def fetch_news():
    """Fetch and score pre-9AM news from 5 feeds"""
    now_ist = datetime.datetime.now(IST)
    cutoff = now_ist.replace(hour=9, minute=0, second=0, microsecond=0)

    total_score = total_count = pos = neg = neu = 0
    feed_scores = {}
    headlines = []

    for feed in NEWS_FEEDS:
        try:
            r = requests.get(feed["url"], headers=YAHOO_HEADERS, timeout=10)
            if r.status_code != 200:
                feed_scores[feed["label"]] = {"score": 50, "count": 0}
                continue

            soup = BeautifulSoup(r.text, "xml")
            items = soup.find_all("item")[:10]
            f_score = f_count = 0

            for item in items:
                title = item.find("title")
                pub_date = item.find("pubDate")
                if not title:
                    continue

                hl = title.get_text().strip()

                # Strict pre-9AM cutoff
                if pub_date:
                    try:
                        pd = datetime.datetime.strptime(
                            pub_date.get_text().strip(),
                            "%a, %d %b %Y %H:%M:%S %Z"
                        ).replace(tzinfo=pytz.UTC).astimezone(IST)
                        if pd > cutoff:
                            continue
                    except:
                        pass

                # Score headline
                score = 0
                hl_lower = hl.lower()
                for g in POS_KW:
                    for k in g["k"]:
                        if k in hl_lower:
                            score += g["w"]
                for g in NEG_KW:
                    for k in g["k"]:
                        if k in hl_lower:
                            score -= g["w"]

                f_score  += score
                f_count  += 1
                total_count += 1
                total_score += score
                if score > 0: pos += 1
                elif score < 0: neg += 1
                else: neu += 1
                if len(headlines) < 15:
                    headlines.append(hl)

            vm = min(1.5, 0.5 + f_count / 10)
            norm = max(0, min(100, 50 + (f_score / f_count) * 15 * vm)) if f_count > 0 else 50
            feed_scores[feed["label"]] = {"score": norm, "count": f_count}

        except Exception as e:
            print(f"News error {feed['label']}: {e}")
            feed_scores[feed["label"]] = {"score": 50, "count": 0}

    vm = min(1.5, 0.5 + total_count / 20)
    combined = max(0, min(100, 50 + (total_score / total_count) * 15 * vm)) if total_count > 0 else 50

    return {
        "combined_score": combined,
        "total_headlines": total_count,
        "pos_count": pos,
        "neg_count": neg,
        "neu_count": neu,
        "feed_scores": feed_scores,
        "headlines": headlines,
    }

# ─────────────────────────────────────────────
# TECHNICAL INDICATORS
# ─────────────────────────────────────────────
def calc_ema(data, period):
    k = 2 / (period + 1)
    ema = [data[0]]
    for i in range(1, len(data)):
        ema.append(data[i] * k + ema[-1] * (1 - k))
    return ema

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50
    gains = losses = 0
    for i in range(1, period + 1):
        d = closes[i] - closes[i-1]
        if d > 0: gains += d
        else: losses -= d
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i-1]
        avg_gain = (avg_gain * (period - 1) + (d if d > 0 else 0)) / period
        avg_loss = (avg_loss * (period - 1) + (-d if d < 0 else 0)) / period
    return 100 - (100 / (1 + avg_gain / avg_loss)) if avg_loss != 0 else 100

def calc_atr(hist, period=14):
    if not hist or len(hist["closes"]) < period + 2:
        return None
    h, l, c = hist["highs"], hist["lows"], hist["closes"]
    n = min(len(h), len(l), len(c))
    if n < period + 2:
        return None
    tr_sum = sum(
        max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1]))
        for i in range(1, min(period + 1, n))
    )
    return tr_sum / period

def calc_bb(closes, period=20, std_mult=2):
    if len(closes) < period:
        return None
    recent = closes[-period:]
    sma = sum(recent) / period
    variance = sum((x - sma) ** 2 for x in recent) / period
    sd = variance ** 0.5
    upper = sma + std_mult * sd
    lower = sma - std_mult * sd
    latest = closes[-1]
    pct_b = (latest - lower) / (upper - lower) * 100 if upper != lower else 50
    width = (upper - lower) / sma * 100

    return {
        "upper": round(upper),
        "lower": round(lower),
        "middle": round(sma),
        "pct_b": round(pct_b, 1),
        "width": round(width, 2),
        "position": "UPPER_BAND" if pct_b > 80 else "LOWER_BAND" if pct_b < 20 else "UPPER_HALF" if pct_b > 50 else "LOWER_HALF",
        "squeeze": width < 1.5,
        "exhaustion": "UPPER_EXHAUSTION" if latest > upper else "LOWER_EXHAUSTION" if latest < lower else "NORMAL",
        "latest": round(latest),
    }

def detect_rzy(hist):
    """Detect Marci Silfrain Little RZY pattern"""
    if not hist or not hist["closes"] or len(hist["closes"]) < 20:
        return {"trend": "UNCLEAR", "pattern": "INSUFFICIENT_DATA"}

    closes = hist["closes"]
    highs  = hist["highs"]
    lows   = hist["lows"]
    n = min(len(closes), len(highs), len(lows))

    # Find swing highs and lows (3-bar window)
    swing_h = [(i, highs[i]) for i in range(2, n-2)
               if highs[i] > highs[i-1] and highs[i] > highs[i-2]
               and highs[i] > highs[i+1] and highs[i] > highs[i+2]]
    swing_l = [(i, lows[i]) for i in range(2, n-2)
               if lows[i] < lows[i-1] and lows[i] < lows[i-2]
               and lows[i] < lows[i+1] and lows[i] < lows[i+2]]

    if len(swing_h) < 2 or len(swing_l) < 2:
        return {"trend": "UNCLEAR", "pattern": "NO_SWINGS"}

    # Determine trend
    lh2, ll2 = swing_h[-2:], swing_l[-2:]
    trend = ("UPTREND"   if lh2[1][1] > lh2[0][1] and ll2[1][1] > ll2[0][1] else
             "DOWNTREND" if lh2[1][1] < lh2[0][1] and ll2[1][1] < ll2[0][1] else "SIDEWAYS")

    latest = closes[-1]

    # Find impulse move
    impulse_start = impulse_end = None
    impulse_pct = 0

    if trend == "UPTREND":
        last_low = swing_l[-1]
        next_high = next((h for h in swing_h if h[0] > last_low[0]), None)
        if next_high:
            impulse_pct = ((next_high[1] - last_low[1]) / last_low[1]) * 100
            if impulse_pct >= 0.5:
                impulse_start, impulse_end = last_low, next_high
    elif trend == "DOWNTREND":
        last_high = swing_h[-1]
        next_low = next((l for l in swing_l if l[0] > last_high[0]), None)
        if next_low:
            impulse_pct = ((last_high[1] - next_low[1]) / last_high[1]) * 100
            if impulse_pct >= 0.5:
                impulse_start, impulse_end = last_high, next_low

    if not impulse_start or not impulse_end:
        return {"trend": trend, "pattern": "NO_IMPULSE", "impulse_pct": 0}

    impulse_size = abs(impulse_end[1] - impulse_start[1])

    if trend == "UPTREND":
        post_lows = [lows[i] for i in range(impulse_end[0] + 1, n)]
        trendline = min(post_lows) if post_lows else impulse_end[1] * 0.995
        entry_zone = {"low": round(trendline * 0.999), "high": round(trendline * 1.002)}
        projected_target = round(latest + impulse_size)
    else:
        post_highs = [highs[i] for i in range(impulse_end[0] + 1, n)]
        trendline = max(post_highs) if post_highs else impulse_end[1] * 1.005
        entry_zone = {"low": round(trendline * 0.998), "high": round(trendline * 1.001)}
        projected_target = round(latest - impulse_size)

    return {
        "trend": trend,
        "pattern": "RZY_DETECTED",
        "impulse_start": round(impulse_start[1]),
        "impulse_end": round(impulse_end[1]),
        "impulse_pct": round(impulse_pct, 2),
        "trendline": round(trendline),
        "measured_move": round(impulse_size),
        "entry_zone": entry_zone,
        "projected_target": projected_target,
        "latest": round(latest),
    }

def get_technicals(hist):
    if not hist or not hist["closes"] or len(hist["closes"]) < 50:
        return None
    closes = hist["closes"]
    ema20 = calc_ema(closes, 20)
    ema50 = calc_ema(closes, 50)
    latest = closes[-1]
    return {
        "latest": latest,
        "ema20": ema20[-1],
        "ema50": ema50[-1],
        "rsi": calc_rsi(closes, 14),
        "adx": 20,
        "above_ema20": latest > ema20[-1],
        "above_ema50": latest > ema50[-1],
    }

# ─────────────────────────────────────────────
# MACRO SIGNAL SCORING (9 signals)
# ─────────────────────────────────────────────
def score_macro(gift, global_data, vix, fii, part_oi, opts, tech, news):
    w = WEIGHTS
    s = {}

    s["GIFT"] = max(0, min(100, 50 + gift["change_pct"] * 20)) if gift else 50

    us_avg = (global_data["sp500"] + global_data["dow"] + global_data["nasdaq"]) / 3 if global_data else 0
    s["US"] = max(0, min(100, 50 + us_avg * 16.67))

    asia_avg = (global_data["nikkei"] + global_data["hangseng"]) / 2 if global_data else 0
    s["ASIA"] = max(0, min(100, 50 + asia_avg * 16.67))

    if part_oi and part_oi.get("fii_net_futures") is not None:
        s["FII_FUT"] = max(0, min(100, 50 + part_oi["fii_net_futures"] / 1000))
    elif fii:
        v = 50 + max(-25, min(25, fii["fii_net"] / 200)) + max(-10, min(10, fii.get("dii_net", 0) / 400))
        s["FII_FUT"] = max(0, min(100, v))
    else:
        s["FII_FUT"] = 50

    if opts:
        pcr = opts["pcr"]
        v = (75 if pcr > 1.5 else 65 if pcr > 1.2 else 50 if pcr > 0.9 else 35 if pcr > 0.7 else 25)
        v -= opts.get("oi_velocity", 0) * 5
        s["PCR"] = max(0, min(100, v))
    else:
        s["PCR"] = 50

    if vix:
        vv = vix["vix"]
        v = (80 if vv < 12 else 65 if vv < 15 else 50 if vv < 18 else 30 if vv < 22 else 15)
        v += (10 if vix["vix_change"] < -2 else -10 if vix["vix_change"] > 2 else 0)
        s["VIX"] = max(0, min(100, v))
    else:
        s["VIX"] = 50

    if tech:
        v = 50
        v += 12 if tech["above_ema20"] else -12
        v += 8  if tech["above_ema50"] else -8
        rsi = tech["rsi"]
        v += 15 if rsi < 30 else 8 if rsi < 40 else -15 if rsi > 70 else -8 if rsi > 60 else 0
        s["TECH"] = max(0, min(100, v))
    else:
        s["TECH"] = 50

    s["NEWS"] = news["combined_score"] if news else 50

    if tech:
        adx = tech.get("adx", 20)
        s["MOM"] = (75 if adx > 30 and tech["above_ema20"] and tech["above_ema50"] else
                    25 if adx > 30 and not tech["above_ema20"] and not tech["above_ema50"] else 50)
    else:
        s["MOM"] = 50

    total = (s["GIFT"] * w["GIFT"] + s["US"] * w["US"] + s["ASIA"] * w["ASIA"] +
             s["FII_FUT"] * w["FII_FUT"] + s["PCR"] * w["PCR"] + s["VIX"] * w["VIX"] +
             s["TECH"] * w["TECH"] + s["NEWS"] * w["NEWS"] + s["MOM"] * w["MOM"]) / 100

    signal = "BULLISH" if total > BULL_THR else "BEARISH" if total < BEAR_THR else "NEUTRAL"
    conf = round(min(100, abs(total - 50) * 2.5))
    return {"total": round(total), "signal": signal, "confidence": conf, "scores": s}

# ─────────────────────────────────────────────
# GEMINI AI ANALYSIS
# ─────────────────────────────────────────────
def call_gemini(prompt):
    """Call Gemini API — tries multiple models in fallback order"""
    if not GEMINI_API_KEY:
        print("ERROR: GEMINI_API_KEY is empty")
        return None

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 4096}
    }

    models_to_try = [GEMINI_MODEL] + [m for m in GEMINI_MODELS if m != GEMINI_MODEL]

    for model in models_to_try:
        try:
            url = GEMINI_BASE.format(model=model, key=GEMINI_API_KEY)
            print(f"  Trying Gemini model: {model}")
            r = requests.post(url, json=payload, timeout=30)
            print(f"  Response status: {r.status_code}")

            if r.status_code == 200:
                data = r.json()
                if "candidates" in data and data["candidates"]:
                    text = data["candidates"][0]["content"]["parts"][0]["text"]
                    print(f"  Gemini OK with {model} ({len(text)} chars)")
                    return text
                print(f"  Unexpected response: {str(data)[:200]}")
            elif r.status_code in (404, 429):
                print(f"  {r.status_code} for {model} — trying next")
                continue
            else:
                print(f"  Error {r.status_code}: {r.text[:200]}")
                continue
        except Exception as e:
            print(f"  Exception with {model}: {e}")
            continue

    print("  All Gemini models failed — using macro fallback")
    return None

def build_prompt(display, macro, rzy, bb, vix, fii, opts, news, closes, atr_val):
    closes5 = [round(x) for x in closes[-5:]]
    latest  = closes5[-1]
    rzy_str = (
        f"Trend: {rzy['trend']}\nPattern: {rzy['pattern']}\n"
        f"Impulse: {rzy.get('impulse_start')} → {rzy.get('impulse_end')} ({rzy.get('impulse_pct')}%)\n"
        f"Trendline: {rzy.get('trendline')}\nEntry zone: {rzy.get('entry_zone')}\n"
        f"Projected target: {rzy.get('projected_target')}"
        if rzy and rzy.get("pattern") == "RZY_DETECTED"
        else f"Trend: {rzy.get('trend','UNCLEAR')} | No clear RZY pattern"
    )
    bb_str = (
        f"Upper: {bb['upper']} | Middle: {bb['middle']} | Lower: {bb['lower']}\n"
        f"%B: {bb['pct_b']}% | Position: {bb['position']}\n"
        f"Width: {bb['width']}% {'⚠ SQUEEZE' if bb['squeeze'] else 'normal'} | Exhaustion: {bb['exhaustion']}"
        if bb else "BB data unavailable"
    )

    return f"""You are an expert Indian stock market analyst combining quantitative macro signals with Marci Silfrain Little RZY pattern recognition and Bollinger Band analysis.

Analyse the following pre-market data for {display} and make a trading decision for TODAY.

=== MACRO SIGNALS ===
Overall score: {macro['total']}/100 | Signal: {macro['signal']} ({macro['confidence']}% conf)
GIFT Nifty: {macro['scores']['GIFT']}/100
US Markets: {macro['scores']['US']}/100
Asia Markets: {macro['scores']['ASIA']}/100
FII Futures: {macro['scores']['FII_FUT']}/100
PCR + OI: {macro['scores']['PCR']}/100
VIX Regime: {macro['scores']['VIX']}/100
Technicals: {macro['scores']['TECH']}/100
News: {macro['scores']['NEWS']}/100
Momentum: {macro['scores']['MOM']}/100

=== PRICE DATA ===
Last 5 closes: {closes5}
Current/latest: {latest}
ATR(14): {round(atr_val) if atr_val else 'N/A'}

=== LITTLE RZY PATTERN ===
{rzy_str}

=== BOLLINGER BANDS (20,2) ===
{bb_str}

=== MARKET CONTEXT ===
India VIX: {vix['vix']:.1f} (change: {vix['vix_change']:.1f}%) | {vix}
FII Cash: {fii['fii_net']:.0f} Cr ({fii['date']}) | {fii} 
PCR: {opts['pcr'] if opts else 'N/A'}
News score: {news['combined_score']:.1f}/100 ({news['pos_count']} positive, {news['neg_count']} negative)
Top headlines: {' | '.join(news['headlines'][:3])}

=== TASK ===
1. Do macro signal and RZY agree or conflict?
2. What does BB position indicate?
3. Make final trading decision

Respond ONLY with valid JSON, no markdown, no backticks:
{{
  "signal": "BULLISH" or "BEARISH" or "NEUTRAL",
  "confidence": <0-100>,
  "signal_type": "HIGH_CONFIDENCE" or "MACRO_ONLY" or "CHART_ONLY" or "CONFLICT" or "NO_SIGNAL",
  "entry": <number or null>,
  "entry_zone_low": <number or null>,
  "entry_zone_high": <number or null>,
  "target": <number or null>,
  "stop_loss": <number or null>,
  "reasoning": "<2-3 sentences combining macro + chart + BB>",
  "key_risk": "<main risk to this trade>",
  "macro_chart_agreement": "AGREE" or "CONFLICT" or "NEUTRAL"
}}"""

def parse_gemini_response(raw):
    if not raw:
        return None
    try:
        # Strip markdown fences and whitespace
        clean = raw.replace("```json", "").replace("```", "").strip()
        # Find JSON boundaries — handle both { and unicode variants
        start = -1
        end   = -1
        for i, ch in enumerate(clean):
            if ch == "{" and start == -1:
                start = i
        for i in range(len(clean)-1, -1, -1):
            if clean[i] == "}":
                end = i
                break

        if start == -1 or end == -1 or end <= start:
            print(f"  No valid JSON found. Response length: {len(clean)}")
            print(f"  First 400 chars: {repr(clean[:400])}")
            return None

        json_str = clean[start:end+1]

        # Check if JSON appears truncated
        if json_str.count("{") != json_str.count("}"):
            print(f"  JSON appears truncated — mismatched braces")
            print(f"  Attempting partial parse...")

        result = json.loads(json_str)

        if "signal" not in result:
            print(f"  Missing signal field")
            return None

        # Ensure all required fields exist with defaults
        result.setdefault("confidence", 50)
        result.setdefault("signal_type", "MACRO_ONLY")
        result.setdefault("entry", None)
        result.setdefault("entry_zone_low", None)
        result.setdefault("entry_zone_high", None)
        result.setdefault("target", None)
        result.setdefault("stop_loss", None)
        result.setdefault("reasoning", "No reasoning provided")
        result.setdefault("key_risk", "Unknown")
        result.setdefault("macro_chart_agreement", "NEUTRAL")

        print(f"  Parsed OK: signal={result['signal']} conf={result['confidence']}")
        return result

    except json.JSONDecodeError as e:
        print(f"  JSON decode error: {e}")
        print(f"  Attempting to extract key fields manually...")
        # Manual extraction fallback for truncated JSON
        return extract_fields_manually(raw)
    except Exception as e:
        print(f"  Parse exception: {e}")
        return None


def extract_fields_manually(raw):
    """Fallback: extract key fields from truncated JSON using string search"""
    import re  # noqa
    result = {}
    try:
        # Extract signal
        m = re.search(r'"signal"\s*:\s*"(BULLISH|BEARISH|NEUTRAL)"', raw)
        if m: result["signal"] = m.group(1)
        else: return None

        # Extract confidence
        m = re.search(r'"confidence"\s*:\s*(\d+)', raw)
        result["confidence"] = int(m.group(1)) if m else 50

        # Extract signal_type
        m = re.search(r'"signal_type"\s*:\s*"([^"]+)"', raw)
        result["signal_type"] = m.group(1) if m else "MACRO_ONLY"

        # Extract entry
        m = re.search(r'"entry"\s*:\s*(\d+)', raw)
        result["entry"] = int(m.group(1)) if m else None

        # Extract target
        m = re.search(r'"target"\s*:\s*(\d+)', raw)
        result["target"] = int(m.group(1)) if m else None

        # Extract stop_loss
        m = re.search(r'"stop_loss"\s*:\s*(\d+)', raw)
        result["stop_loss"] = int(m.group(1)) if m else None

        # Extract entry zones
        m = re.search(r'"entry_zone_low"\s*:\s*(\d+)', raw)
        result["entry_zone_low"] = int(m.group(1)) if m else None
        m = re.search(r'"entry_zone_high"\s*:\s*(\d+)', raw)
        result["entry_zone_high"] = int(m.group(1)) if m else None

        # Extract reasoning
        m = re.search(r'"reasoning"\s*:\s*"([^"]{10,})"', raw)
        result["reasoning"] = m.group(1) if m else "Extracted from truncated response"

        # Extract key_risk
        m = re.search(r'"key_risk"\s*:\s*"([^"]{5,})"', raw)
        result["key_risk"] = m.group(1) if m else "Unknown"

        # Extract agreement
        m = re.search(r'"macro_chart_agreement"\s*:\s*"(AGREE|CONFLICT|NEUTRAL)"', raw)
        result["macro_chart_agreement"] = m.group(1) if m else "NEUTRAL"

        print(f"  Manual extraction OK: signal={result['signal']}")
        return result
    except Exception as e:
        print(f"  Manual extraction failed: {e}")
        return None

# ─────────────────────────────────────────────
# SHEET WRITING — always appends, never overwrites
# ─────────────────────────────────────────────
def get_week_number(wb):
    """Get current week number of 4-week tracking period"""
    try:
        sh = get_or_create_sheet(wb, "Meta")
        start_val = sh.cell(1, 1).value
        if not start_val:
            today_str = datetime.datetime.now(IST).strftime("%Y-%m-%d")
            sh.update("A1", today_str)
            return 1
        start = datetime.datetime.strptime(start_val, "%Y-%m-%d").replace(tzinfo=IST)
        now = datetime.datetime.now(IST)
        diff_days = (now - start).days
        return min(4, max(1, diff_days // 7 + 1))
    except:
        return 1

def get_trading_day(wb):
    """Increment and return trading day counter"""
    try:
        sh = get_or_create_sheet(wb, "Meta")
        val = sh.cell(1, 2).value
        day_num = int(val) + 1 if val else 1
        sh.update("B1", day_num)
        return day_num
    except:
        return 1

def append_daily_record(sh, today, week_num, day_num, idx_display, macro, chart_signal,
                        chart_conf, rzy, bb, ai, atr_val):
    row = [
        today, week_num, day_num, idx_display,
        macro["signal"], macro["total"], macro["confidence"],
        round(macro["scores"].get("GIFT", 50), 1),
        round(macro["scores"].get("US", 50), 1),
        round(macro["scores"].get("ASIA", 50), 1),
        round(macro["scores"].get("FII_FUT", 50), 1),
        round(macro["scores"].get("PCR", 50), 1),
        round(macro["scores"].get("VIX", 50), 1),
        round(macro["scores"].get("TECH", 50), 1),
        round(macro["scores"].get("NEWS", 50), 1),
        round(macro["scores"].get("MOM", 50), 1),
        chart_signal, chart_conf,
        rzy.get("pattern", "—") if rzy else "—",
        bb["position"] if bb else "—",
        rzy.get("trend", "—") if rzy else "—",
        ai.get("signal", "—"),
        ai.get("confidence", 0),
        ai.get("signal_type", "—"),
        ai.get("entry") or "—",
        ai.get("target") or "—",
        ai.get("stop_loss") or "—",
        "—",  # actual close — filled at 3:30 PM
        "—",  # P&L — filled at 3:30 PM
        "—",  # result — filled at 3:30 PM
        ai.get("reasoning", "—"),
    ]
    sh.append_row(row, value_input_option="USER_ENTERED")

def append_paper_trade(sh, today, week_num, day_num, idx_display, ai, macro_total):
    if ai.get("signal_type") in ("NO_SIGNAL",):
        return
    entry = ai.get("entry") or "—"
    entry_zone = (f"{ai.get('entry_zone_low')}–{ai.get('entry_zone_high')}"
                  if ai.get("entry_zone_low") else "±ATR")
    row = [
        today, week_num, day_num, idx_display,
        ai.get("signal", "—"),
        ai.get("confidence", 0),
        ai.get("signal_type", "—"),
        entry, entry_zone,
        ai.get("target") or "—",
        ai.get("stop_loss") or "—",
        "—",  # actual close
        "—",  # P&L points
        "—",  # P&L %
        "SKIP" if ai.get("signal_type") == "CONFLICT" else "OPEN",
        "—",  # exit time
        "—",  # exit reason
        macro_total,
        ai.get("reasoning", "—"),
    ]
    sh.append_row(row, value_input_option="USER_ENTERED")

def append_ai_log(sh, today, week_num, idx_display, ai, macro, rzy, bb):
    row = [
        today, week_num, idx_display,
        ai.get("signal", "—"),
        ai.get("confidence", 0),
        (ai.get("reasoning", "—") or "—") + "\n\nKey Risk: " + (ai.get("key_risk", "—") or "—"),
        f"Macro:{macro['total']}/100 | RZY:{rzy.get('pattern','N/A') if rzy else 'N/A'} | "
        f"BB:{bb['position'] if bb else 'N/A'} | Agreement:{ai.get('macro_chart_agreement','—')}",
    ]
    sh.append_row(row, value_input_option="USER_ENTERED")

def append_pattern_log(sh, today, week_num, idx_display, rzy, bb):
    if not rzy or rzy.get("pattern") != "RZY_DETECTED":
        return
    row = [
        today, week_num, idx_display, rzy["trend"],
        rzy.get("impulse_start"), rzy.get("impulse_end"),
        str(rzy.get("impulse_pct")) + "%",
        rzy.get("trendline"), rzy.get("measured_move"), rzy.get("projected_target"),
        bb["position"] if bb else "—",
    ]
    sh.append_row(row, value_input_option="USER_ENTERED")

def update_dashboard_today(dash_sh, results):
    """Update today's signal section on Dashboard_4W (rows 55-58)"""
    for i, r in enumerate(results):
        ai = r["ai"]
        row_num = 55 + i
        dash_sh.update(values=[[
            ai.get("signal", "—"),
            str(ai.get("confidence", 0)) + "%",
            ai.get("entry") or "—",
            ai.get("target") or "—",
            ai.get("stop_loss") or "—",
            ai.get("reasoning", "—"),
            "SKIP" if ai.get("signal_type") == "CONFLICT" else "OPEN 🟡"
        ]], range_name=f"B{row_num}:H{row_num}")

# ─────────────────────────────────────────────
# AFTERNOON — close trades, update records
# ─────────────────────────────────────────────
def afternoon_run(wb):
    print("=== AFTERNOON RUN ===")
    today = datetime.datetime.now(IST).strftime("%Y-%m-%d")
    pt_sh = get_or_create_sheet(wb, "Paper_Trades")
    dr_sh = get_or_create_sheet(wb, "Daily_Records")

    # Fetch closing prices
    closes = {}
    for key, idx in INDICES.items():
        latest = fetch_latest(idx["yahoo"])
        if latest:
            closes[idx["display"]] = round(latest["price"])
        time.sleep(0.5)

    # Update Paper_Trades
    pt_data = pt_sh.get_all_values()
    for i, row in enumerate(pt_data[3:], start=4):  # skip 3 header rows
        if len(row) < 15: continue
        row_date     = row[0]
        idx_display  = row[3]
        direction    = row[4]
        entry_price  = row[7]
        result       = row[14]

        if row_date != today: continue
        if result not in ("OPEN", "", "—"): continue
        if idx_display not in closes: continue

        close_price = closes[idx_display]
        try:
            entry = float(entry_price)
        except:
            continue

        pnl = (close_price - entry) if direction == "BULLISH" else (entry - close_price)
        pnl_pct = f"{(pnl / entry * 100):.2f}%"
        result_str = "WIN ✅" if pnl > 0 else "LOSS ❌"

        pt_sh.update(f"L{i}:Q{i}", [[
            close_price, round(pnl), pnl_pct,
            result_str, "15:30", "EOD close"
        ]])

    # Update Daily_Records actual close + P&L
    dr_data = dr_sh.get_all_values()
    for i, row in enumerate(dr_data[2:], start=3):  # skip 2 header rows
        if len(row) < 22: continue
        row_date    = row[0]
        idx_display = row[3]
        direction   = row[21]
        entry_str   = row[23]

        if row_date != today: continue
        if idx_display not in closes: continue

        close_price = closes[idx_display]
        try:
            entry = float(entry_str)
            pnl = (close_price - entry) if direction == "BULLISH" else (entry - close_price)
            result = "WIN ✅" if pnl > 0 else "LOSS ❌" if pnl < 0 else "NEUTRAL"
            dr_sh.update(f"AB{i}:AD{i}", [[close_price, round(pnl), result]])
        except:
            continue

    # Refresh 4-week dashboard
    refresh_dashboard(wb)
    print("=== AFTERNOON DONE ===")

def refresh_dashboard(wb):
    """Recalculate all 4-week metrics from Paper_Trades"""
    print("Refreshing dashboard...")
    pt_sh   = get_or_create_sheet(wb, "Paper_Trades")
    dash_sh = get_or_create_sheet(wb, "Dashboard_4W")
    pt_data = pt_sh.get_all_values()

    index_names = ["Nifty 50", "Bank Nifty", "Nifty Midcap 50", "Nifty Smallcap 50"]
    metrics = {idx: {"total":0,"win":0,"loss":0,"pnl":0,"best":0,"worst":0,
                     "hc_total":0,"hc_win":0,"skip":0,"conf_sum":0,"conf_n":0}
               for idx in index_names}
    week_stats = {w: {"total":0,"win":0,"pnl":0} for w in range(1,5)}
    all_total = all_win = all_pnl = 0

    for row in pt_data[3:]:
        if len(row) < 15: continue
        idx = row[3]; wk_str = row[1]; result = row[14]
        sig_type = row[6]; conf_str = row[5]; pnl_str = row[12]

        if idx not in metrics: continue
        if result in ("OPEN","","—","SKIP"): continue

        try:
            pnl = float(pnl_str) if pnl_str not in ("","—") else 0
            conf = float(conf_str) if conf_str else 0
            wk = int(wk_str) if wk_str else 1
        except:
            continue

        is_win = "WIN" in result
        m = metrics[idx]
        m["total"] += 1; m["conf_sum"] += conf; m["conf_n"] += 1
        if is_win: m["win"] += 1; m["pnl"] += pnl
        else: m["loss"] += 1; m["pnl"] += pnl
        m["best"]  = max(m["best"],  pnl)
        m["worst"] = min(m["worst"], pnl)
        if sig_type == "HIGH_CONFIDENCE":
            m["hc_total"] += 1
            if is_win: m["hc_win"] += 1
        if sig_type == "CONFLICT": m["skip"] += 1
        all_total += 1
        if is_win: all_win += 1
        all_pnl += pnl
        if 1 <= wk <= 4:
            week_stats[wk]["total"] += 1
            if is_win: week_stats[wk]["win"] += 1
            week_stats[wk]["pnl"] += pnl

    # Write overall performance
    for ci, idx in enumerate(index_names):
        m = metrics[idx]
        wr = f"{(m['win']/m['total']*100):.1f}%" if m["total"] > 0 else "—"
        hc_wr = f"{(m['hc_win']/m['hc_total']*100):.1f}%" if m["hc_total"] > 0 else "—"
        avg_pnl = round(m["pnl"] / m["total"]) if m["total"] > 0 else "—"
        avg_conf = f"{(m['conf_sum']/m['conf_n']):.1f}%" if m["conf_n"] > 0 else "—"
        vals = [m["total"], m["win"], m["loss"], wr, avg_pnl,
                m["best"] or "—", m["worst"] or "—", round(m["pnl"]),
                m["hc_total"], hc_wr, m["skip"], avg_conf]
        col = chr(ord("B") + ci)
        for ri, v in enumerate(vals):
            dash_sh.update(f"{col}{7+ri}", [[v]])

    # Weekly breakdown
    for wk in range(1, 5):
        ws = week_stats[wk]
        wr = f"{(ws['win']/ws['total']*100):.1f}%" if ws["total"] > 0 else "—"
        avg = round(ws["pnl"] / ws["total"]) if ws["total"] > 0 else "—"
        dash_sh.update(f"C{22+wk-1}:F{22+wk-1}", [[ws["total"], ws["win"], wr, avg]])

    # All indices totals
    all_wr = f"{(all_win/all_total*100):.1f}%" if all_total > 0 else "—"
    dash_sh.update("F7:F8", [[all_total], [all_win]])
    dash_sh.update("F10", [[all_wr]])
    dash_sh.update("F15", [[round(all_pnl)]])

    # Last updated
    now_str = datetime.datetime.now(IST).strftime("%d-%b-%Y %H:%M") + " IST"
    dash_sh.update("G3:H3", [[now_str, ""]])
    print("Dashboard refreshed")

def generate_weekly_summary(wb, week_num):
    """Gemini weekly summary on Fridays"""
    pt_sh = get_or_create_sheet(wb, "Paper_Trades")
    ai_sh = get_or_create_sheet(wb, "AI_Analysis")
    dash_sh = get_or_create_sheet(wb, "Dashboard_4W")
    pt_data = pt_sh.get_all_values()

    week_trades = [row for row in pt_data[3:]
                   if len(row) > 14 and str(row[1]) == str(week_num)
                   and row[14] not in ("OPEN","","—")]

    if not week_trades:
        return

    wins  = sum(1 for t in week_trades if "WIN" in t[14])
    total = len(week_trades)

    prompt = f"""Analyse week {week_num} of Indian equity paper trades using a macro + RZY + BB signal system.

Week {week_num}: {total} trades | {wins} wins | {total-wins} losses | {(wins/total*100):.1f}% win rate

Trades:
{chr(10).join([f"{t[0]} | {t[3]} | {t[4]} ({t[5]}%) | {t[6]} | {t[14]} | P&L: {t[12]} pts" for t in week_trades[:20]])}

Write 3-4 sentences covering:
1. What worked (which signals, indices)
2. What failed and why
3. One key observation
4. Recommendation for next week

Be concise and data-driven."""

    summary = call_gemini(prompt)
    if summary:
        dash_sh.update("A41:H44", [[summary, "", "", "", "", "", "", ""]])
        ai_sh.append_row([
            datetime.datetime.now(IST).strftime("%Y-%m-%d"),
            week_num, "ALL INDICES", "WEEKLY SUMMARY", "—", summary,
            f"{total} trades | {wins} wins | {(wins/total*100):.1f}% win rate"
        ], value_input_option="USER_ENTERED")

# ─────────────────────────────────────────────
# MORNING RUN — main orchestrator
# ─────────────────────────────────────────────
def morning_run(wb):
    print("=== MORNING RUN ===")
    today    = datetime.datetime.now(IST).strftime("%Y-%m-%d")
    week_num = get_week_number(wb)
    day_num  = get_trading_day(wb)

    # Init NSE session
    print("Initialising NSE session...")
    init_nse_session()
    time.sleep(2)

    # Fetch global data
    print("Fetching global markets...")
    gift     = fetch_latest("^NSEI")
    sp500    = fetch_latest("^GSPC")
    dow      = fetch_latest("^DJI")
    nasdaq   = fetch_latest("^IXIC")
    nikkei   = fetch_latest("^N225")
    hangseng = fetch_latest("^HSI")
    crude    = fetch_latest("CL=F")
    usdinr   = fetch_latest("USDINR=X")

    global_data = {
        "sp500":    sp500["change_pct"]    if sp500    else 0,
        "dow":      dow["change_pct"]      if dow      else 0,
        "nasdaq":   nasdaq["change_pct"]   if nasdaq   else 0,
        "nikkei":   nikkei["change_pct"]   if nikkei   else 0,
        "hangseng": hangseng["change_pct"] if hangseng else 0,
        "crude":    crude["change_pct"]    if crude    else 0,
        "usdinr":   usdinr["price"]        if usdinr   else 83.5,
    }

    print("Fetching NSE data...")
    vix_data  = fetch_vix();      time.sleep(1)
    fii_data  = fetch_fii_dii();  time.sleep(1)
    part_oi   = fetch_participant_oi(); time.sleep(1)
    nifty_opts = fetch_options("NIFTY");      time.sleep(1)
    bnf_opts   = fetch_options("BANKNIFTY");  time.sleep(1)

    print("Fetching news...")
    news_data = fetch_news()

    # Get sheets
    dr_sh  = get_or_create_sheet(wb, "Daily_Records")
    pt_sh  = get_or_create_sheet(wb, "Paper_Trades")
    ai_sh  = get_or_create_sheet(wb, "AI_Analysis")
    pl_sh  = get_or_create_sheet(wb, "Pattern_Log")
    dash_sh = get_or_create_sheet(wb, "Dashboard_4W")

    all_results = []

    for key, idx in INDICES.items():
        print(f"Analysing {idx['display']}...")
        hist = fetch_yahoo(idx["yahoo"], "6mo", "1d")
        # Try fallback ticker if primary returned insufficient data
        if (not hist or len(hist["closes"]) < 50) and idx.get("yahoo_fallback"):
            print(f"  Primary ticker failed, trying fallback: {idx['yahoo_fallback']}")
            hist = fetch_yahoo(idx["yahoo_fallback"], "6mo", "1d")
        if not hist or len(hist["closes"]) < 50:
            print(f"  Skipping {key} — insufficient data from both tickers")
            continue

        tech    = get_technicals(hist)
        atr_val = calc_atr(hist, 14)
        rzy     = detect_rzy(hist)
        bb      = calc_bb(hist["closes"], 20, 2)
        opts    = bnf_opts if key == "BANKNIFTY" else nifty_opts
        macro   = score_macro(gift, global_data, vix_data, fii_data, part_oi, opts, tech, news_data)

        # Chart signal from RZY
        if rzy and rzy.get("pattern") == "RZY_DETECTED":
            chart_signal = ("BULLISH" if rzy["trend"] == "UPTREND" else
                            "BEARISH" if rzy["trend"] == "DOWNTREND" else "NEUTRAL")
            chart_conf = 70
        else:
            chart_signal = "NEUTRAL"
            chart_conf = 20

        # Call Gemini
        print(f"  Calling Gemini for {idx['display']}...")
        prompt = build_prompt(
            idx["display"], macro, rzy, bb,
            vix_data or {"vix": 15, "vix_change": 0},
            fii_data or {"fii_net": 0, "dii_net": 0, "date": "—"},
            opts, news_data, hist["closes"], atr_val
        )
        raw = call_gemini(prompt)
        ai  = parse_gemini_response(raw)

        # Fallback
        if not ai:
            ai = {
                "signal": macro["signal"],
                "confidence": macro["confidence"],
                "signal_type": "MACRO_ONLY" if macro["signal"] != "NEUTRAL" else "NO_SIGNAL",
                "entry": round(hist["closes"][-1]),
                "entry_zone_low": None, "entry_zone_high": None,
                "target": rzy.get("projected_target") if rzy else None,
                "stop_loss": round(hist["closes"][-1] - (atr_val or 0) * 1.5),
                "reasoning": "Gemini unavailable — macro signal used.",
                "key_risk": "AI unavailable.",
                "macro_chart_agreement": "NEUTRAL",
            }

        # Write records — all append, never overwrite
        append_daily_record(dr_sh, today, week_num, day_num, idx["display"],
                            macro, chart_signal, chart_conf, rzy, bb, ai, atr_val)
        append_paper_trade(pt_sh, today, week_num, day_num, idx["display"], ai, macro["total"])
        append_ai_log(ai_sh, today, week_num, idx["display"], ai, macro, rzy, bb)
        append_pattern_log(pl_sh, today, week_num, idx["display"], rzy, bb)

        all_results.append({"display": idx["display"], "ai": ai, "macro": macro})
        time.sleep(1)

    # Update today's dashboard
    update_dashboard_today(dash_sh, all_results)
    print("=== MORNING RUN COMPLETE ===")

# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
def main():
    print(f"Starting NiftyPulse Auto — {RUN_TYPE} run")
    print(f"Time: {datetime.datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}")

    if not GEMINI_API_KEY:
        print("ERROR: GEMINI_API_KEY not set in environment")
        return
    if not GOOGLE_SHEET_ID:
        print("ERROR: GOOGLE_SHEET_ID not set in environment")
        return
    if not GOOGLE_CREDS_JSON:
        print("ERROR: GOOGLE_CREDS_JSON not set in environment")
        return

    # Connect to Google Sheet
    print("Connecting to Google Sheet...")
    wb = connect_sheet()
    print("Connected!")

    now = datetime.datetime.now(IST)
    # Skip weekends
    if now.weekday() >= 5:
        print("Weekend — skipping")
        return

    if RUN_TYPE == "MORNING":
        morning_run(wb)
    elif RUN_TYPE == "AFTERNOON":
        afternoon_run(wb)
    elif RUN_TYPE == "WEEKLY":
        week_num = get_week_number(wb)
        generate_weekly_summary(wb, week_num)
    else:
        print(f"Unknown RUN_TYPE: {RUN_TYPE}")

if __name__ == "__main__":
    main()
