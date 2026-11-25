"""Serve single-timeframe chan.py calculations from the MT5 bridge."""
import hmac
import json
import logging
import math
import os
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NamedTuple
from urllib.parse import parse_qs, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from Chan import CChan
from ChanConfig import CChanConfig
from Common.CEnum import BSP_TYPE, DATA_FIELD, KL_TYPE
from Common.CTime import CTime
from KLine.KLine_Unit import CKLine_Unit

TIMEFRAMES = {
    "M1": KL_TYPE.K_1M, "M5": KL_TYPE.K_5M, "M15": KL_TYPE.K_15M,
    "M30": KL_TYPE.K_30M, "H1": KL_TYPE.K_60M, "H4": KL_TYPE.K_60M,
    "D1": KL_TYPE.K_DAY, "W1": KL_TYPE.K_WEEK,
}
# H4 has no fork enum. The single-level calculation uses the actual bar times.
BSP_TYPES = {kind.value for kind in BSP_TYPE}
TOKEN_FALLBACK = Path("/home/hatch/workspace/gold-trade-app/assets/index.html")
CACHE_SECONDS = 60
CACHE_LIMIT = 128
# Bridge timestamps are MT5 server wall time as epoch; subtract this to get UTC.
# The App adds the same offset back, so both sides must agree (7200 = UTC+2).
TZ_OFFSET = int(os.environ.get("CHANPY_TZ_OFFSET", "7200"))
MAX_DIVERGENCE_RATE = 100
MAX_MIN_ZS_CNT = 1000


class Params(NamedTuple):
    symbol: str
    tf: str
    bars: int
    bi_strict: bool
    divergence_rate: float
    min_zs_cnt: int
    bs_type: str


def parse_params(query):
    values = parse_qs(query, keep_blank_values=True, max_num_fields=32)
    if any(len(items) != 1 for items in values.values()):
        raise ValueError
    get = lambda key, default: values.get(key, [default])[0]
    symbol = get("symbol", "XAUUSD")
    tf = get("tf", "M30")
    bars = max(100, min(5000, int(get("bars", "3000"))))
    strict = get("bi_strict", "1")
    # Quantize so near-identical floats (0.1 vs 0.10001) share one cache key;
    # otherwise a token holder can bust the cache with arbitrary decimals.
    divergence = round(float(get("divergence_rate", "0.9")), 2)
    min_zs = int(get("min_zs_cnt", "1"))
    types = set(get("bs_type", "1,2,3a").split(","))
    if (not re.fullmatch(r"[A-Za-z0-9_.#-]{1,32}", symbol)
            or tf not in TIMEFRAMES or strict not in {"0", "1"}
            or not math.isfinite(divergence) or divergence < 0
            or divergence > MAX_DIVERGENCE_RATE
            or min_zs < 0 or min_zs > MAX_MIN_ZS_CNT or not types <= BSP_TYPES):
        raise ValueError
    return Params(symbol, tf, bars, strict == "1", divergence, min_zs,
                  ",".join(sorted(types)))


def load_token():
    path = Path(os.environ.get("CHANPY_TOKEN_FILE", "/root/mt5/bridge_token"))
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        html = TOKEN_FALLBACK.read_text(encoding="utf-8", errors="ignore")
        match = re.search(r"""MT_TOK\s*[:=]\s*["']([^"']+)["']""", html)
        if not match:
            match = re.search(r"MT_TOK[^A-Za-z0-9_\-]{0,20}([A-Za-z0-9_\-.]{10,})", html)
        token = match[1] if match else ""
    if not re.fullmatch(r"[!-~]+", token):
        raise ValueError("Invalid bridge token")
    return token


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class BridgeError(Exception):
    pass


def iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def line_json(line):
    return {
        "dir": "up" if line.is_up() else "down",
        "s": iso(line.get_begin_time().ts),
        "e": iso(line.get_end_time().ts),
        "sp": line.get_begin_val(), "ep": line.get_end_val(),
        "sure": line.is_sure,
    }


def compute(params, rows):
    level = TIMEFRAMES[params.tf]
    target_types = set(params.bs_type.split(","))
    config = CChanConfig({
        "trigger_step": True, "print_warning": False, "print_err_time": False,
        "bi_strict": params.bi_strict, "divergence_rate": params.divergence_rate,
        "min_zs_cnt": params.min_zs_cnt, "bs_type": params.bs_type,
    })
    chan = CChan(code=params.symbol, lv_list=[level], config=config)
    units = []
    for row in rows:
        dt = datetime.fromtimestamp(row[0] - TZ_OFFSET, timezone.utc)
        o, h, l, c = map(float, row[1:5])
        if not all(map(math.isfinite, (o, h, l, c))):
            raise ValueError("Invalid OHLC")
        units.append(CKLine_Unit({
            DATA_FIELD.FIELD_TIME: CTime(dt.year, dt.month, dt.day, dt.hour,
                                         dt.minute, dt.second, auto=False),
            DATA_FIELD.FIELD_OPEN: o, DATA_FIELD.FIELD_HIGH: h,
            DATA_FIELD.FIELD_LOW: l, DATA_FIELD.FIELD_CLOSE: c,
        }))
    # Skip constructor loading, then calculate segments and centers once.
    config.trigger_step = False
    chan.trigger_load({level: units})
    kl = chan[0]
    # One record per (bar, direction): a point can carry several kinds
    # (e.g. "1" and "1p"); base kinds win so the App never draws two
    # overlapping labels at the same spot.
    bsp = []
    seen = set()
    for point in kl.bs_point_lst.getSortedBspList():
        kinds = sorted(
            (kind.value for kind in point.type if kind.value in target_types),
            key=lambda value: (value not in {"1", "2", "3a"}, value))
        for value in kinds:
            timestamp = point.bi.get_end_time().ts
            key = (timestamp, point.is_buy)
            if key in seen:
                continue
            seen.add(key)
            bsp.append({"type": value, "buy": point.is_buy,
                        "t": iso(timestamp),
                        "price": point.bi.get_end_val(), "sure": point.bi.is_sure})
    return {
        "symbol": params.symbol, "tf": params.tf, "bars": len(rows),
        "merged": len(kl.lst), "computed_at": datetime.now(timezone.utc).isoformat(),
        "bi": [line_json(line) for line in kl.bi_list.bi_list],
        "seg": [line_json(line) for line in kl.seg_list.lst],
        "zs": [
            {"s": iso(zs.begin_bi.get_begin_time().ts if zs.begin_bi is not None else zs.begin.time.ts),
             "e": iso(zs.end_bi.get_end_time().ts if zs.end_bi is not None else zs.end.time.ts),
             "low": zs.low, "high": zs.high, "sure": zs.is_sure}
            for zs in kl.zs_list.zs_lst
        ],
        "bsp": bsp,
    }


def encode(payload):
    return json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()


class ChanServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, token, bridge_url):
        super().__init__(address, Handler)
        self.token = token
        self.authorization = ("Bearer " + token).encode("ascii")
        self.bridge_url = bridge_url.rstrip("/")
        self.opener = build_opener(NoRedirect())
        self.cache = OrderedDict()
        self._lock = threading.Lock()

    def fetch(self, params, count):
        query = urlencode(dict(symbol=params.symbol, tf=params.tf, count=count, pos=0))
        req = Request(f"{self.bridge_url}/kline?{query}",
                      headers={"Authorization": "Bearer " + self.token})
        try:
            with self.opener.open(req, timeout=30) as response:
                rows = json.load(response)["bars"]
            if not isinstance(rows, list) or not 0 < len(rows) <= count:
                raise ValueError("Invalid bridge response")
            for row in rows:
                if not isinstance(row, list) or len(row) < 5 or type(row[0]) is not int:
                    raise ValueError("Invalid bridge bar")
                o, h, l, c = map(float, row[1:5])
                if not all(map(math.isfinite, (o, h, l, c))) or not (
                        l <= min(o, c) <= max(o, c) <= h):
                    raise ValueError("Invalid bridge OHLC")
            return sorted(rows, key=lambda row: row[0])
        except Exception:
            raise BridgeError from None

    def calculate(self, params):
        # One recompute at a time: a slow /chan (about 2 s) must not stall
        # /health, and concurrent identical requests share one computation
        # through the cache check below. /health never takes this lock.
        with self._lock:
            entry = self.cache.get(params)
            if entry and time.monotonic() - entry[0] < CACHE_SECONDS:
                try:
                    latest = self.fetch(params, 1)[-1][0]
                except BridgeError:
                    # Bridge blip: serve the still-fresh cache instead of a 502.
                    return entry[2], entry[3]
                if latest == entry[1] and time.monotonic() - entry[0] < CACHE_SECONDS:
                    self.cache.move_to_end(params)
                    return entry[2], entry[3]
            rows = self.fetch(params, params.bars)
            payload = encode(compute(params, rows))
            self.cache[params] = (time.monotonic(), rows[-1][0], payload, len(rows))
            self.cache.move_to_end(params)
            if len(self.cache) > CACHE_LIMIT:
                self.cache.popitem(last=False)
            return payload, len(rows)

    def handle_error(self, request, client_address):
        # Log only the client address, never the request line, credentials,
        # or bridge exception text.
        logging.warning("request error from %s", client_address[0])


class Handler(BaseHTTPRequestHandler):
    timeout = 10

    def log_message(self, format, *args):
        pass

    def reply(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/health":
            self.reply(200, encode({"ok": True}))
            return
        if path != "/chan":
            self.reply(404, encode({"error": "not found"}))
            return
        auth = self.headers.get_all("Authorization", [])
        if len(auth) != 1 or not hmac.compare_digest(
                auth[0].encode("latin-1"), self.server.authorization):
            self.reply(401, encode({"error": "unauthorized"}))
            return
        try:
            params = parse_params(query)
        except ValueError:
            self.reply(400, encode({"error": "invalid parameters"}))
            return
        start = time.monotonic()
        count = 0
        try:
            body, count = self.server.calculate(params)
            status = 200
        except BridgeError:
            status = 502
            body = encode({"error": "bridge request or data failed"})
        except Exception:
            status = 502
            body = encode({"error": "calculation failed"})
        logging.info("tf=%s bars=%d bi_strict=%d divergence_rate=%g min_zs_cnt=%d "
                     "bs_type=%s elapsed_ms=%.1f count=%d",
                     params.tf, params.bars, params.bi_strict, params.divergence_rate,
                     params.min_zs_cnt, params.bs_type, (time.monotonic() - start) * 1000,
                     count)
        self.reply(status, body)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        token = load_token()
    except (OSError, ValueError):
        raise SystemExit("Unable to load bridge token") from None
    address = (os.environ.get("CHANPY_HOST", "0.0.0.0"),
               int(os.environ.get("CHANPY_PORT", "8905")))
    bridge = os.environ.get("CHANPY_BRIDGE_URL", "http://127.0.0.1:8896")
    with ChanServer(address, token, bridge) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
