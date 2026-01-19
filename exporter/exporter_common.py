#!/usr/bin/env python3
import argparse
import asyncio
import json
import logging
import os
import ssl
import time
from http.cookiejar import CookieJar
from urllib.request import HTTPCookieProcessor, HTTPSHandler, Request, build_opener, urlopen

from prometheus_client import REGISTRY, start_http_server
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

try:
    import websockets
except Exception:
    websockets = None


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)


def _parse_listen(value: str):
    if ":" not in value:
        return value, 9108
    host, port = value.rsplit(":", 1)
    return host, int(port)


def _join_path(base: str, path: str) -> str:
    if not base.endswith("/"):
        base = base + "/"
    if path.startswith("/"):
        path = path[1:]
    return base + path


class JsonCollector:
    def __init__(
        self,
        source: str | None,
        timeout: int,
        delay_unit: str,
        api_source: str | None = None,
        api_timeout: int | None = None,
        user_map_file: str | None = None,
        ui_host: str | None = None,
        ui_port: int | None = None,
        ui_basepath: str = "/",
        ui_scheme: str = "https",
        ui_username: str | None = None,
        ui_password: str | None = None,
        ui_bearer_token: str | None = None,
        ui_api_key: str | None = None,
        ui_login_path: str = "/login",
        ui_inbounds_path: str = "/api/inbounds",
        ui_online_path: str = "/api/onlineClients",
        ui_insecure: bool = False,
        ui_ws_url: str | None = None,
        ui_ws_timeout: int = 5,
        ui_ws_cache_ttl: int = 30,
        ui_ws_messages: int = 3,
    ):
        self.source = source
        self.timeout = timeout
        self.delay_unit = delay_unit
        self.api_source = api_source
        self.api_timeout = api_timeout or timeout
        self.user_map_file = user_map_file

        self.ui_host = ui_host
        self.ui_port = ui_port
        self.ui_basepath = ui_basepath or "/"
        self.ui_scheme = ui_scheme
        self.ui_username = ui_username
        self.ui_password = ui_password
        self.ui_bearer_token = ui_bearer_token
        self.ui_api_key = ui_api_key
        self.ui_login_path = ui_login_path
        self.ui_inbounds_path = ui_inbounds_path
        self.ui_online_path = ui_online_path
        self.ui_insecure = ui_insecure
        self.ui_ws_url = ui_ws_url
        self.ui_ws_timeout = ui_ws_timeout
        self.ui_ws_cache_ttl = ui_ws_cache_ttl
        self.ui_ws_messages = ui_ws_messages

        self._fetch_errors = 0
        self._parse_errors = 0
        self._ui_cookie_ts = 0
        self._ui_cookie_ttl = 24 * 60 * 60
        self._ui_inbounds_cache_ts = 0
        self._ui_inbounds_cache_ttl = 24 * 60 * 60
        self._ui_inbounds_cache = None
        self._ui_ws_cache_ts = 0
        self._ui_ws_cache = None

        self._cookie_jar = CookieJar()
        handler = HTTPCookieProcessor(self._cookie_jar)
        https_handler = HTTPSHandler(context=self._make_ssl_context())
        self._opener = build_opener(handler, https_handler)

    def _make_ssl_context(self):
        if not self.ui_insecure:
            return ssl.create_default_context()
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    def _fetch_from_source(self, source: str, timeout: int, name: str):
        if source.startswith("http://") or source.startswith("https://"):
            req = Request(source, headers={"User-Agent": "xray-exporter"})
            with urlopen(req, timeout=timeout) as r:
                raw = r.read()
        else:
            with open(source, "rb") as f:
                raw = f.read()
        return json.loads(raw.decode("utf-8")), len(raw)

    def _fetch(self):
        return self._fetch_from_source(self.source, self.timeout, "source")

    def _iter_numeric_paths(self, obj, prefix=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                path = f"{prefix}.{k}" if prefix else str(k)
                yield from self._iter_numeric_paths(v, path)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                path = f"{prefix}[{i}]"
                yield from self._iter_numeric_paths(v, path)
        else:
            if isinstance(obj, (int, float)):
                yield prefix, float(obj)

    def _headers(self):
        headers = {"User-Agent": "xray-exporter"}
        if self.ui_bearer_token:
            headers["Authorization"] = f"Bearer {self.ui_bearer_token}"
        if self.ui_api_key:
            headers["x-api-key"] = self.ui_api_key
        return headers

    def _login_if_needed(self):
        if not self.ui_username or not self.ui_password:
            return False
        if time.time() - self._ui_cookie_ts < self._ui_cookie_ttl:
            return True

        url = self._ui_url(self.ui_login_path)
        payload = json.dumps({"username": self.ui_username, "password": self.ui_password}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        headers.update(self._headers())
        req = Request(url, data=payload, headers=headers, method="POST")
        with self._opener.open(req, timeout=self.timeout) as r:
            _ = r.read()
        self._ui_cookie_ts = time.time()
        return True

    def _ui_url(self, path: str):
        base = f"{self.ui_scheme}://{self.ui_host}:{self.ui_port}"
        full = _join_path(self.ui_basepath, path)
        if not full.startswith("/"):
            full = "/" + full
        return base + full

    def _fetch_3xui(self):
        if not self.ui_host or not self.ui_port:
            return None, None

        try:
            self._login_if_needed()
        except Exception as e:
            logging.warning("3x-ui login failed: %s", e)

        inbounds_payload = None
        online_payload = None

        now = time.time()
        if self._ui_inbounds_cache and (now - self._ui_inbounds_cache_ts) < self._ui_inbounds_cache_ttl:
            inbounds_payload = self._ui_inbounds_cache
        else:
            try:
                url = self._ui_url(self.ui_inbounds_path)
                req = Request(url, headers=self._headers())
                with self._opener.open(req, timeout=self.timeout) as r:
                    raw = r.read()
                inbounds_payload = json.loads(raw.decode("utf-8"))
                self._ui_inbounds_cache = inbounds_payload
                self._ui_inbounds_cache_ts = now
            except Exception as e:
                logging.warning("3x-ui inbounds fetch failed: %s", e)

        try:
            url = self._ui_url(self.ui_online_path)
            req = Request(url, headers=self._headers())
            with self._opener.open(req, timeout=self.timeout) as r:
                raw = r.read()
            online_payload = json.loads(raw.decode("utf-8"))
        except Exception as e:
            logging.debug("3x-ui online fetch failed: %s", e)

        return inbounds_payload, online_payload

    def _cookies_header(self):
        parts = []
        for c in self._cookie_jar:
            if c.name and c.value:
                parts.append(f"{c.name}={c.value}")
        if not parts:
            return None
        return "; ".join(parts)

    def _fetch_3xui_ws(self):
        if not self.ui_ws_url:
            return None
        if websockets is None:
            logging.warning("websockets package is not installed")
            return None

        now_ts = time.time()
        if self._ui_ws_cache and (now_ts - self._ui_ws_cache_ts) < self.ui_ws_cache_ttl:
            return self._ui_ws_cache

        headers = self._headers()
        cookie_header = self._cookies_header()
        if cookie_header:
            headers["Cookie"] = cookie_header

        async def _ws_once():
            connect_kwargs = dict(
                open_timeout=self.ui_ws_timeout,
                close_timeout=self.ui_ws_timeout,
            )
            try:
                connect_kwargs["additional_headers"] = headers
                ws_conn = websockets.connect(self.ui_ws_url, **connect_kwargs)
            except TypeError:
                connect_kwargs.pop("additional_headers", None)
                connect_kwargs["extra_headers"] = headers
                ws_conn = websockets.connect(self.ui_ws_url, **connect_kwargs)

            async with ws_conn as ws:
                payload = None
                for _ in range(self.ui_ws_messages):
                    msg = await asyncio.wait_for(ws.recv(), timeout=self.ui_ws_timeout)
                    try:
                        payload = json.loads(msg)
                    except Exception:
                        continue
                return payload

        try:
            payload = asyncio.run(_ws_once())
            self._ui_ws_cache = payload
            self._ui_ws_cache_ts = now_ts
            logging.info("3x-ui WS payload fetched")
            return payload
        except Exception as e:
            logging.warning("3x-ui WS fetch failed: %s", e)
            return None

    @staticmethod
    def _load_user_map(path: str | None) -> dict:
        if not path:
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            logging.warning("User map file not found: %s", path)
            return {}
        except Exception as e:
            logging.warning("Failed to read user map file %s: %s", path, e)
            return {}

        if isinstance(raw, dict):
            return {str(k): str(v) for k, v in raw.items()}
        if isinstance(raw, list):
            out = {}
            for item in raw:
                if not isinstance(item, dict):
                    continue
                user = item.get("user") or item.get("email") or item.get("id")
                alias = item.get("alias") or item.get("label") or item.get("comment")
                if user and alias is not None:
                    out[str(user)] = str(alias)
            return out
        logging.warning("Unsupported user map format in %s", path)
        return {}

    @staticmethod
    def _unwrap_obj(payload):
        if isinstance(payload, dict) and "obj" in payload:
            return payload.get("obj")
        return payload

    def _extract_clients_from_inbounds(self, payload):
        inbounds = self._unwrap_obj(payload)
        if not isinstance(inbounds, list):
            return []
        clients = []
        for inbound in inbounds:
            if not isinstance(inbound, dict):
                continue
            for key in ("clients", "clientStats"):
                if isinstance(inbound.get(key), list):
                    clients.extend([c for c in inbound.get(key) if isinstance(c, dict)])
            settings = inbound.get("settings")
            if isinstance(settings, str):
                try:
                    settings = json.loads(settings)
                except Exception as e:
                    logging.debug("Failed to parse inbound.settings JSON: %s", e)
            if isinstance(settings, dict) and isinstance(settings.get("clients"), list):
                clients.extend([c for c in settings.get("clients") if isinstance(c, dict)])
        return clients

    def _build_user_map_from_3xui(self, inbounds_payload):
        mapping = {}
        for c in self._extract_clients_from_inbounds(inbounds_payload):
            user = c.get("email") or c.get("user") or c.get("id") or c.get("uuid")
            alias = c.get("remark") or c.get("comment") or c.get("tag") or c.get("name")
            if user and alias is not None:
                mapping[str(user)] = str(alias)
        return mapping

    @staticmethod
    def _extract_online_count(payload):
        data = JsonCollector._unwrap_obj(payload)
        if isinstance(data, dict):
            for k in ("count", "online", "onlineCount", "online_count"):
                if k in data and isinstance(data[k], (int, float)):
                    return float(data[k])
            for k in ("clients", "onlineClients", "list"):
                if isinstance(data.get(k), list):
                    return float(len(data.get(k)))
            if isinstance(data.get("lastOnlineMap"), dict):
                return float(len(data.get("lastOnlineMap")))
        if isinstance(data, list):
            return float(len(data))
        return None

    def _extract_uptime(self, payload):
        if payload is None:
            return None
        keys = {"uptime", "uptime_seconds", "uptimeSeconds", "uptimeSec"}
        found = self._find_numeric_by_keys(payload, keys)
        if found:
            return found[0]
        return None

    def _find_numeric_by_keys(self, obj, keys: set[str]):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in keys:
                    val = self._to_float(v, f"api.{k}")
                    if val is not None:
                        return val, k
                found = self._find_numeric_by_keys(v, keys)
                if found:
                    return found
        if isinstance(obj, list):
            for v in obj:
                found = self._find_numeric_by_keys(v, keys)
                if found:
                    return found
        return None

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
            "1 if the last scrape failed, otherwise 0",
        )
        scrape_duration = GaugeMetricFamily(
            "xray_exporter_scrape_duration_seconds",
            "Seconds spent fetching and parsing the JSON.",
        )
        scrape_size = GaugeMetricFamily(
            "xray_exporter_scrape_size_bytes",
            "Response size in bytes.",
        )
        last_scrape = GaugeMetricFamily(
            "xray_exporter_last_scrape_timestamp_seconds",
            "Unix timestamp of last successful scrape.",
        )
        fetch_errors = CounterMetricFamily(
            "xray_exporter_fetch_errors_total",
            "Total number of fetch errors.",
        )
        parse_errors = CounterMetricFamily(
            "xray_exporter_parse_errors_total",
            "Total number of JSON parse errors.",
        )

        start = time.perf_counter()
        api_data = None
        ws_data = None
        ui_inbounds = None
        ui_online = None

        data = None
        if self.source:
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
        else:
            scrape_error.add_metric([], 0.0)
            scrape_size.add_metric([], 0.0)
            last_scrape.add_metric([], time.time())
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

        if data is not None:
            version = (
                data.get("version")
                or data.get("xray_version")
                or (data.get("xray") or {}).get("version")
                or (data.get("core") or {}).get("version")
            )
            xray_info = GaugeMetricFamily("xray_info", "Xray version info.", labels=["version"])
            if version:
                xray_info.add_metric([str(version)], 1.0)
            yield xray_info

        if self.api_source:
            try:
                api_data, _ = self._fetch_from_source(self.api_source, self.api_timeout, "api")
            except Exception as e:
                logging.warning("Xray API fetch failed: %s", e)

        if self.ui_host and self.ui_port:
            ui_inbounds, ui_online = self._fetch_3xui()
            if self.ui_ws_url:
                ws_data = self._fetch_3xui_ws()

        if data is not None:
            obs = data.get("observatory", {}) or {}
            alive = GaugeMetricFamily("xray_observatory_alive", "Alive flag (1/0).", labels=["outbound_tag"])
            delay_g = GaugeMetricFamily("xray_observatory_delay_ms", "Delay (milliseconds).", labels=["outbound_tag"])
            last_seen = GaugeMetricFamily("xray_observatory_last_seen_time", "Unix ts.", labels=["outbound_tag"])
            last_try = GaugeMetricFamily("xray_observatory_last_try_time", "Unix ts.", labels=["outbound_tag"])

            for key, v in obs.items():
                if not isinstance(v, dict):
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

            yield alive
            yield delay_g
            yield last_seen
            yield last_try

        online_users = 0
        if data is not None:
            stats = data.get("stats", {}) or {}
            total_down = CounterMetricFamily("xray_traffic_downlink_bytes_total", "Total downlink bytes.")
            total_up = CounterMetricFamily("xray_traffic_uplink_bytes_total", "Total uplink bytes.")
            inbound_down = CounterMetricFamily(
                "xray_traffic_inbound_downlink_bytes_total",
                "Inbound downlink bytes.",
                labels=["protocol"],
            )
            inbound_up = CounterMetricFamily(
                "xray_traffic_inbound_uplink_bytes_total",
                "Inbound uplink bytes.",
                labels=["protocol"],
            )
            outbound_down = CounterMetricFamily(
                "xray_traffic_outbound_downlink_bytes_total",
                "Outbound downlink bytes.",
                labels=["outbound_tag"],
            )
            outbound_up = CounterMetricFamily(
                "xray_traffic_outbound_uplink_bytes_total",
                "Outbound uplink bytes.",
                labels=["outbound_tag"],
            )
            user_down = CounterMetricFamily(
                "xray_traffic_user_downlink_bytes_total",
                "User downlink bytes.",
                labels=["user"],
            )
            user_up = CounterMetricFamily(
                "xray_traffic_user_uplink_bytes_total",
                "User uplink bytes.",
                labels=["user"],
            )
            user_up_bytes = GaugeMetricFamily(
                "xray_traffic_user_uplink_bytes",
                "User uplink bytes (raw).",
                labels=["user"],
            )
            user_conn = GaugeMetricFamily(
                "xray_user_conn_count",
                "User connection count (gauge).",
                labels=["user"],
            )

            total_dl = 0.0
            total_ul = 0.0

            for section in ("inbound", "outbound", "user"):
                sec = stats.get(section, {}) or {}
                if not isinstance(sec, dict):
                    continue
                for name, vv in sec.items():
                    if vv is None or not isinstance(vv, dict):
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
                                if conn_f > 0:
                                    online_users += 1

            total_down.add_metric([], total_dl)
            total_up.add_metric([], total_ul)

            yield total_down
            yield total_up
            yield inbound_down
            yield inbound_up
            yield outbound_down
            yield outbound_up
            yield user_down
            yield user_up
            yield user_up_bytes
            yield user_conn

        user_map = self._load_user_map(self.user_map_file)
        if ui_inbounds:
            ui_map = self._build_user_map_from_3xui(ui_inbounds)
            for k, v in ui_map.items():
                user_map.setdefault(k, v)

        if user_map:
            alias_g = GaugeMetricFamily(
                "xray_user_alias_info",
                "User alias map (value=1).",
                labels=["user", "alias"],
            )
            for user, alias in user_map.items():
                alias_g.add_metric([str(user), str(alias)], 1.0)
            yield alias_g

        users_online = None
        if ws_data is not None:
            users_online = self._extract_online_count(ws_data)
        if users_online is None and ui_online is not None:
            users_online = self._extract_online_count(ui_online)
        if users_online is None and api_data is not None:
            users_online = self._extract_online_count(api_data)
        if users_online is None:
            users_online = float(online_users)

        uptime = None
        if ws_data is not None:
            uptime = self._extract_uptime(ws_data)
        if uptime is None and api_data is not None:
            uptime = self._extract_uptime(api_data)

        users_online_g = GaugeMetricFamily("xray_users_online", "Online users count.")
        users_online_g.add_metric([], float(users_online))
        yield users_online_g

        if uptime is not None:
            uptime_g = GaugeMetricFamily("xray_uptime_seconds", "Uptime in seconds.")
            uptime_g.add_metric([], float(uptime))
            yield uptime_g

        if data is not None:
            all_vars = GaugeMetricFamily(
                "xray_var",
                "All numeric values from Xray debug/vars (path label).",
                labels=["path"],
            )
            for path, val in self._iter_numeric_paths(data):
                if not path:
                    continue
                all_vars.add_metric([path], val)
            yield all_vars


def run_exporter(require_source: bool):
    ap = argparse.ArgumentParser(description="Expose Prometheus metrics from a JSON (file or URL).")
    ap.add_argument(
        "--source",
        default=os.getenv("SOURCE"),
        required=require_source and not bool(os.getenv("SOURCE")),
        help="Путь к JSON-файлу или HTTP(S) URL (или ENV SOURCE).",
    )
    ap.add_argument(
        "--api-source",
        default=os.getenv("XRAY_API_SOURCE"),
        help="HTTP(S) URL Xray API для uptime/online (ENV XRAY_API_SOURCE).",
    )
    ap.add_argument(
        "--listen",
        default=os.getenv("LISTEN", "0.0.0.0:9108"),
        help="Адрес:порт HTTP (или ENV LISTEN).",
    )
    ap.add_argument(
        "--timeout",
        type=int,
        default=int(os.getenv("TIMEOUT", "5")),
        help="Таймаут чтения, сек (или ENV TIMEOUT).",
    )
    ap.add_argument(
        "--api-timeout",
        type=int,
        default=int(os.getenv("XRAY_API_TIMEOUT", "0") or 0),
        help="Таймаут Xray API, сек (ENV XRAY_API_TIMEOUT; 0 = TIMEOUT).",
    )
    ap.add_argument(
        "--delay-unit",
        choices=["ms", "s"],
        default=os.getenv("DELAY_UNIT", "ms"),
        help='Единицы "delay": ms|s (или ENV DELAY_UNIT).',
    )
    ap.add_argument(
        "--user-map-file",
        default=os.getenv("USER_MAP_FILE"),
        help="JSON-файл маппинга user->alias (ENV USER_MAP_FILE).",
    )
    ap.add_argument("--ui-host", default=os.getenv("UI_HOST"), help="Хост 3x-ui API (ENV UI_HOST).")
    ap.add_argument(
        "--ui-port",
        type=int,
        default=int(os.getenv("UI_PORT", "0") or 0),
        help="Порт 3x-ui API (ENV UI_PORT).",
    )
    ap.add_argument(
        "--ui-basepath",
        default=os.getenv("UI_BASEPATH", "/"),
        help="Basepath 3x-ui (ENV UI_BASEPATH).",
    )
    ap.add_argument(
        "--ui-scheme",
        default=os.getenv("UI_SCHEME", "https"),
        help="Схема 3x-ui (http|https).",
    )
    ap.add_argument("--ui-username", default=os.getenv("UI_USERNAME"), help="Логин 3x-ui.")
    ap.add_argument("--ui-password", default=os.getenv("UI_PASSWORD"), help="Пароль 3x-ui.")
    ap.add_argument(
        "--ui-bearer-token",
        default=os.getenv("UI_BEARER_TOKEN"),
        help="Bearer token 3x-ui (если включён).",
    )
    ap.add_argument(
        "--ui-api-key",
        default=os.getenv("UI_API_KEY"),
        help="apiKey header (если требуется).",
    )
    ap.add_argument(
        "--ui-login-path",
        default=os.getenv("UI_LOGIN_PATH", "/login"),
        help="Путь Login (ENV UI_LOGIN_PATH).",
    )
    ap.add_argument(
        "--ui-inbounds-path",
        default=os.getenv("UI_INBOUNDS_PATH", "/api/inbounds"),
        help="Путь Inbounds (ENV UI_INBOUNDS_PATH).",
    )
    ap.add_argument(
        "--ui-online-path",
        default=os.getenv("UI_ONLINE_PATH", "/api/onlineClients"),
        help="Путь Online Clients (ENV UI_ONLINE_PATH).",
    )
    ap.add_argument(
        "--ui-insecure",
        action="store_true",
        default=os.getenv("UI_INSECURE", "false").lower() == "true",
        help="Отключить проверку TLS (ENV UI_INSECURE=true).",
    )
    ap.add_argument("--ui-ws-url", default=os.getenv("UI_WS_URL"), help="WebSocket URL 3x-ui (ENV UI_WS_URL).")
    ap.add_argument(
        "--ui-ws-timeout",
        type=int,
        default=int(os.getenv("UI_WS_TIMEOUT", "5")),
        help="Таймаут WS (сек).",
    )
    ap.add_argument(
        "--ui-ws-cache-ttl",
        type=int,
        default=int(os.getenv("UI_WS_CACHE_TTL", "30")),
        help="TTL WS кэша (сек).",
    )
    ap.add_argument(
        "--ui-ws-messages",
        type=int,
        default=int(os.getenv("UI_WS_MESSAGES", "3")),
        help="Сколько WS сообщений читать за сессию.",
    )

    args = ap.parse_args()

    host, port = _parse_listen(args.listen)
    api_timeout = args.api_timeout if args.api_timeout and args.api_timeout > 0 else args.timeout
    logging.info(
        "Starting exporter: listen=%s:%d source=%s timeout=%ss delay_unit=%s api_source=%s api_timeout=%ss",
        host,
        port,
        args.source,
        args.timeout,
        args.delay_unit,
        args.api_source,
        api_timeout,
    )

    REGISTRY.register(
        JsonCollector(
            args.source,
            args.timeout,
            args.delay_unit,
            api_source=args.api_source,
            api_timeout=api_timeout,
            user_map_file=args.user_map_file,
            ui_host=args.ui_host,
            ui_port=args.ui_port or None,
            ui_basepath=args.ui_basepath,
            ui_scheme=args.ui_scheme,
            ui_username=args.ui_username,
            ui_password=args.ui_password,
            ui_bearer_token=args.ui_bearer_token,
            ui_api_key=args.ui_api_key,
            ui_login_path=args.ui_login_path,
            ui_inbounds_path=args.ui_inbounds_path,
            ui_online_path=args.ui_online_path,
            ui_insecure=args.ui_insecure,
            ui_ws_url=args.ui_ws_url,
            ui_ws_timeout=args.ui_ws_timeout,
            ui_ws_cache_ttl=args.ui_ws_cache_ttl,
            ui_ws_messages=args.ui_ws_messages,
        )
    )
    start_http_server(port, addr=host)
    print(f"Serving on http://{host}:{port}/metrics; pulling from {args.source}", flush=True)

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass