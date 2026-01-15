#!/usr/bin/env python3
import argparse, json, time, os, sys, logging
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from prometheus_client import start_http_server, REGISTRY
from prometheus_client.core import GaugeMetricFamily, CounterMetricFamily

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)

class JsonCollector:
    def __init__(self, source: str, timeout: int, delay_unit: str):
        self.source = source
        self.timeout = timeout
        self.delay_unit = delay_unit  # 'ms' или 's'

    def _fetch(self):
        logging.debug("Fetching source: %s (timeout=%s)", self.source, self.timeout)
        try:
            if self.source.startswith(("http://", "https://")):
                req = Request(self.source, headers={"User-Agent": "json-exporter/1"})
                with urlopen(req, timeout=self.timeout) as r:
                    status = getattr(r, "status", None) or r.getcode()
                    raw = r.read()
                    logging.info("Fetched HTTP %s (%d bytes) from %s",
                                 status, len(raw), self.source)
                    if status and status >= 400:
                        raise HTTPError(self.source, status, "bad status", hdrs=r.headers, fp=None)
            else:
                with open(self.source, "rb") as f:
                    raw = f.read()
                    logging.info("Read file (%d bytes): %s", len(raw), self.source)
        except (HTTPError, URLError, TimeoutError) as e:
            logging.error("Fetch error for %s: %s", self.source, e)
            raise
        except Exception as e:
            logging.exception("Unhandled fetch exception for %s", self.source)
            raise

        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:
            logging.error("JSON decode error: %s", e)
            logging.debug("Raw head: %r", raw[:256])
            raise

        # Немного диагностики структуры
        logging.debug(
            "Top-level keys: %s", list(data.keys())
        )
        stats = data.get("stats") or {}
        for sec in ("inbound", "outbound", "user"):
            obj = stats.get(sec) or {}
            logging.debug("stats.%s keys: %s", sec, list(obj.keys()))
        obs = data.get("observatory") or {}
        logging.debug("observatory keys: %s", list(obs.keys()))
        return data

    def collect(self):
        scrape_error = GaugeMetricFamily(
            "json_exporter_last_scrape_error",
            "1 if the last scrape failed, otherwise 0"
        )
        try:
            data = self._fetch()
            scrape_error.add_metric([], 0.0)
        except Exception:
            scrape_error.add_metric([], 1.0)
            yield scrape_error
            return
        yield scrape_error

        # ---- observatory ----
        obs = data.get("observatory", {}) or {}
        alive = GaugeMetricFamily("observatory_alive", "Alive flag (1/0).",
                                  labels=["outbound_tag"])
        delay_g = GaugeMetricFamily("observatory_delay_seconds",
                                    "Delay (seconds).", labels=["outbound_tag"])
        last_seen = GaugeMetricFamily("observatory_last_seen_timestamp_seconds",
                                      "Unix ts.", labels=["outbound_tag"])
        last_try = GaugeMetricFamily("observatory_last_try_timestamp_seconds",
                                     "Unix ts.", labels=["outbound_tag"])

        for key, v in obs.items():
            if not isinstance(v, dict):
                logging.warning("observatory.%s is not an object: %r", key, v)
                continue
            outbound_tag = v.get("outbound_tag", key)
            alive.add_metric([outbound_tag], 1.0 if v.get("alive") else 0.0)

            d = v.get("delay")
            if d is not None:
                try:
                    d = float(d)
                    if self.delay_unit == "ms":
                        d = d / 1000.0
                    delay_g.add_metric([outbound_tag], d)
                except Exception as e:
                    logging.warning("Bad delay value for %s: %r (%s)", outbound_tag, d, e)

            ts = v.get("last_seen_time")
            if ts is not None:
                try:
                    last_seen.add_metric([outbound_tag], float(ts))
                except Exception as e:
                    logging.warning("Bad last_seen_time for %s: %r (%s)", outbound_tag, ts, e)

            ts = v.get("last_try_time")
            if ts is not None:
                try:
                    last_try.add_metric([outbound_tag], float(ts))
                except Exception as e:
                    logging.warning("Bad last_try_time for %s: %r (%s)", outbound_tag, ts, e)

        yield alive; yield delay_g; yield last_seen; yield last_try

        # ---- traffic stats ----
        stats = data.get("stats", {}) or {}
        down = CounterMetricFamily("traffic_downlink_bytes_total",
                                   "Total downlink bytes.", labels=["section","name"])
        up = CounterMetricFamily("traffic_uplink_bytes_total",
                                 "Total uplink bytes.", labels=["section","name"])

        for section in ("inbound", "outbound", "user"):
            sec = stats.get(section, {}) or {}
            if not isinstance(sec, dict):
                logging.warning("stats.%s is not an object: %r", section, type(sec))
                continue
            for name, vv in sec.items():
                if vv is None:
                    logging.debug("stats.%s.%s is null", section, name)
                    continue
                if not isinstance(vv, dict):
                    logging.warning("stats.%s.%s is not an object: %r", section, name, vv)
                    continue
                dl = vv.get("downlink")
                ul = vv.get("uplink")
                if dl is not None:
                    try:
                        down.add_metric([section, name], float(dl))
                    except Exception as e:
                        logging.warning("Bad downlink for %s.%s: %r (%s)", section, name, dl, e)
                if ul is not None:
                    try:
                        up.add_metric([section, name], float(ul))
                    except Exception as e:
                        logging.warning("Bad uplink for %s.%s: %r (%s)", section, name, ul, e)

        yield down; yield up

def _parse_listen(s: str):
    host, sep, port = s.rpartition(":")
    host = host if sep else "0.0.0.0"
    return host, int(port or 9108)

def main():
    ap = argparse.ArgumentParser(description="Expose Prometheus metrics from a JSON (file or URL).")
    ap.add_argument("--source", default=os.getenv("SOURCE"),
                    required=not bool(os.getenv("SOURCE")),
                    help="Путь к JSON-файлу или HTTP(S) URL (или ENV SOURCE).")
    ap.add_argument("--listen", default=os.getenv("LISTEN","0.0.0.0:9108"),
                    help="Адрес:порт HTTP (или ENV LISTEN).")
    ap.add_argument("--timeout", type=int, default=int(os.getenv("TIMEOUT","5")),
                    help="Таймаут чтения, сек (или ENV TIMEOUT).")
    ap.add_argument("--delay-unit", choices=["ms","s"], default=os.getenv("DELAY_UNIT","ms"),
                    help='Единицы "delay": ms|s (или ENV DELAY_UNIT).')
    args = ap.parse_args()

    host, port = _parse_listen(args.listen)
    logging.info("Starting exporter: listen=%s:%d source=%s timeout=%ss delay_unit=%s",
                 host, port, args.source, args.timeout, args.delay_unit)

    REGISTRY.register(JsonCollector(args.source, args.timeout, args.delay_unit))
    start_http_server(port, addr=host)
    print(f"Serving on http://{host}:{port}/metrics; pulling from {args.source}", flush=True)

    try:
        while True: time.sleep(3600)
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()

