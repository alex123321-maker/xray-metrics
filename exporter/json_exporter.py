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
        self._fetch_errors = 0
        self._parse_errors = 0

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
        return data, len(raw)

    @staticmethod
    def _to_float(val, ctx):
        try:
            return float(val)
        except Exception as e:
            logging.warning("Bad numeric value for %s: %r (%s)", ctx, val, e)
            return None

    def collect(self):
        scrape_error = GaugeMetricFamily(
            "json_exporter_last_scrape_error",
            "1 if the last scrape failed, otherwise 0"
        )
        scrape_duration = GaugeMetricFamily(
            "xray_exporter_scrape_duration_seconds",
            "Seconds spent fetching and parsing the JSON."
        )
        scrape_size = GaugeMetricFamily(
            "xray_exporter_scrape_size_bytes",
            "Response size in bytes."
        )
        last_scrape = GaugeMetricFamily(
            "xray_exporter_last_scrape_timestamp_seconds",
            "Unix timestamp of last successful scrape."
        )
        fetch_errors = CounterMetricFamily(
            "xray_exporter_fetch_errors_total",
            "Total number of fetch errors."
        )
        parse_errors = CounterMetricFamily(
            "xray_exporter_parse_errors_total",
            "Total number of JSON parse errors."
        )
        start = time.perf_counter()
        try:
            data, raw_len = self._fetch()
            scrape_error.add_metric([], 0.0)
            scrape_size.add_metric([], float(raw_len))
            last_scrape.add_metric([], time.time())
        except json.JSONDecodeError:
            scrape_error.add_metric([], 1.0)
            self._parse_errors += 1
            parse_errors.add_metric([], float(self._parse_errors))
            yield scrape_error
            yield scrape_duration
            yield parse_errors
            return
        except Exception:
            scrape_error.add_metric([], 1.0)
            self._fetch_errors += 1
            fetch_errors.add_metric([], float(self._fetch_errors))
            yield scrape_error
            yield scrape_duration
            yield fetch_errors
            yield parse_errors
            return
        finally:
            elapsed = time.perf_counter() - start
            scrape_duration.add_metric([], float(elapsed))

        yield scrape_error
        yield scrape_duration
        yield scrape_size
        yield last_scrape
        fetch_errors.add_metric([], float(self._fetch_errors))
        parse_errors.add_metric([], float(self._parse_errors))
        yield fetch_errors
        yield parse_errors

        # ---- xray info ----
        version = (
            data.get("version")
            or data.get("xray_version")
            or (data.get("xray") or {}).get("version")
            or (data.get("core") or {}).get("version")
        )
        xray_info = GaugeMetricFamily("xray_info", "Xray version info.", labels=["version"])
        if version:
            xray_info.add_metric([str(version)], 1.0)
        else:
            logging.debug("xray version not found in payload")
        yield xray_info

        # ---- observatory ----
        obs = data.get("observatory", {}) or {}
        if not obs:
            logging.debug("observatory section is empty or missing")
        alive = GaugeMetricFamily("xray_observatory_alive", "Alive flag (1/0).",
                      labels=["outbound_tag"])
        delay_g = GaugeMetricFamily("xray_observatory_delay_ms",
                        "Delay (milliseconds).", labels=["outbound_tag"])
        last_seen = GaugeMetricFamily("xray_observatory_last_seen_time",
                          "Unix ts.", labels=["outbound_tag"])
        last_try = GaugeMetricFamily("xray_observatory_last_try_time",
                         "Unix ts.", labels=["outbound_tag"])

        for key, v in obs.items():
            if not isinstance(v, dict):
                logging.warning("observatory.%s is not an object: %r", key, v)
                continue
            outbound_tag = v.get("outbound_tag", key)
            alive.add_metric([outbound_tag], 1.0 if v.get("alive") else 0.0)

            d = v.get("delay")
            if d is not None:
                d = self._to_float(d, f"observatory.{outbound_tag}.delay")
                if d is not None:
                    if self.delay_unit == "s":
                        d = d * 1000.0
                    delay_g.add_metric([outbound_tag], d)

            ts = v.get("last_seen_time")
            if ts is not None:
                ts_f = self._to_float(ts, f"observatory.{outbound_tag}.last_seen_time")
                if ts_f is not None:
                    last_seen.add_metric([outbound_tag], ts_f)

            ts = v.get("last_try_time")
            if ts is not None:
                ts_f = self._to_float(ts, f"observatory.{outbound_tag}.last_try_time")
                if ts_f is not None:
                    last_try.add_metric([outbound_tag], ts_f)

        yield alive; yield delay_g; yield last_seen; yield last_try

        # ---- traffic stats ----
        stats = data.get("stats", {}) or {}
        if not stats:
            logging.debug("stats section is empty or missing")
        total_down = CounterMetricFamily("xray_traffic_downlink_bytes_total",
                         "Total downlink bytes.")
        total_up = CounterMetricFamily("xray_traffic_uplink_bytes_total",
                           "Total uplink bytes.")
        inbound_down = CounterMetricFamily("xray_traffic_inbound_downlink_bytes_total",
                           "Inbound downlink bytes.", labels=["protocol"])
        inbound_up = CounterMetricFamily("xray_traffic_inbound_uplink_bytes_total",
                         "Inbound uplink bytes.", labels=["protocol"])
        outbound_down = CounterMetricFamily("xray_traffic_outbound_downlink_bytes_total",
                            "Outbound downlink bytes.", labels=["outbound_tag"])
        outbound_up = CounterMetricFamily("xray_traffic_outbound_uplink_bytes_total",
                          "Outbound uplink bytes.", labels=["outbound_tag"])
        user_down = CounterMetricFamily("xray_traffic_user_downlink_bytes_total",
                        "User downlink bytes.", labels=["user"])
        user_up = CounterMetricFamily("xray_traffic_user_uplink_bytes_total",
                          "User uplink bytes.", labels=["user"])
        user_up_bytes = GaugeMetricFamily("xray_traffic_user_uplink_bytes",
                  "User uplink bytes (raw).", labels=["user"])
        user_conn = GaugeMetricFamily("xray_user_conn_count",
                "User connection count (gauge).", labels=["user"])

        total_dl = 0.0
        total_ul = 0.0

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
                    dl_f = self._to_float(dl, f"stats.{section}.{name}.downlink")
                    if dl_f is not None:
                        total_dl += dl_f
                if ul is not None:
                    ul_f = self._to_float(ul, f"stats.{section}.{name}.uplink")
                    if ul_f is not None:
                        total_ul += ul_f

                if section == "inbound":
                    protocol = vv.get("protocol") or name
                    if dl is not None:
                        dl_f = self._to_float(dl, f"inbound.{protocol}.downlink")
                        if dl_f is not None:
                            inbound_down.add_metric([str(protocol)], dl_f)
                    if ul is not None:
                        ul_f = self._to_float(ul, f"inbound.{protocol}.uplink")
                        if ul_f is not None:
                            inbound_up.add_metric([str(protocol)], ul_f)

                if section == "outbound":
                    outbound_tag = vv.get("outbound_tag") or name
                    if dl is not None:
                        dl_f = self._to_float(dl, f"outbound.{outbound_tag}.downlink")
                        if dl_f is not None:
                            outbound_down.add_metric([str(outbound_tag)], dl_f)
                    if ul is not None:
                        ul_f = self._to_float(ul, f"outbound.{outbound_tag}.uplink")
                        if ul_f is not None:
                            outbound_up.add_metric([str(outbound_tag)], ul_f)

                if section == "user":
                    user = vv.get("user") or name
                    if dl is not None:
                        dl_f = self._to_float(dl, f"user.{user}.downlink")
                        if dl_f is not None:
                            user_down.add_metric([str(user)], dl_f)
                    if ul is not None:
                        ul_f = self._to_float(ul, f"user.{user}.uplink")
                        if ul_f is not None:
                            user_up.add_metric([str(user)], ul_f)
                            user_up_bytes.add_metric([str(user)], ul_f)

                    conn_val = (
                        vv.get("conn_count")
                        or vv.get("connCount")
                        or vv.get("connection_count")
                    )
                    if conn_val is not None:
                        conn_f = self._to_float(conn_val, f"user.{user}.conn_count")
                        if conn_f is not None:
                            user_conn.add_metric([str(user)], conn_f)

        total_down.add_metric([], total_dl)
        total_up.add_metric([], total_ul)

        yield total_down; yield total_up
        yield inbound_down; yield inbound_up
        yield outbound_down; yield outbound_up
        yield user_down; yield user_up; yield user_up_bytes; yield user_conn

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

