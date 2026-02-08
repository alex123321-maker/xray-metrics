#!/usr/bin/env python3
import argparse
import asyncio
import json
import logging
import os
import ssl
import time
from http.cookiejar import CookieJar
from urllib.request import HTTPCookieProcessor, HTTPSHandler, Request, build_opener

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


class XuiCollector:
    def __init__(
        self,
        timeout: int,
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
        self.timeout = timeout
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
        self._ui_inbounds_cache_ttl = 10 * 60
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

    def _headers(self):
        headers = {"User-Agent": "xui-exporter"}
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

    def _fetch_json(self, url: str):
        req = Request(url, headers=self._headers())
        with self._opener.open(req, timeout=self.timeout) as r:
            raw = r.read()
        return json.loads(raw.decode("utf-8")), len(raw)

    def _fetch_3xui(self):
        if not self.ui_host or not self.ui_port:
            return None, None, 0

        try:
            self._login_if_needed()
        except Exception as e:
            logging.warning("3x-ui login failed: %s", e)

        inbounds_payload = None
        online_payload = None
        total_size = 0

        now = time.time()
        if self._ui_inbounds_cache and (now - self._ui_inbounds_cache_ts) < self._ui_inbounds_cache_ttl:
            inbounds_payload = self._ui_inbounds_cache
        else:
            try:
                url = self._ui_url(self.ui_inbounds_path)
                inbounds_payload, size = self._fetch_json(url)
                total_size += size
                self._ui_inbounds_cache = inbounds_payload
                self._ui_inbounds_cache_ts = now
            except json.JSONDecodeError:
                self._parse_errors += 1
            except Exception as e:
                self._fetch_errors += 1
                logging.warning("3x-ui inbounds fetch failed: %s", e)

        try:
            url = self._ui_url(self.ui_online_path)
            online_payload, size = self._fetch_json(url)
            total_size += size
        except json.JSONDecodeError:
            self._parse_errors += 1
        except Exception as e:
            self._fetch_errors += 1
            logging.debug("3x-ui online fetch failed: %s", e)

        return inbounds_payload, online_payload, total_size

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
                best_payload = None
                for _ in range(self.ui_ws_messages):
                    msg = await asyncio.wait_for(ws.recv(), timeout=self.ui_ws_timeout)
                    try:
                        payload = json.loads(msg)
                    except Exception:
                        continue
                    if self._is_traffic_payload(payload):
                        best_payload = payload
                return best_payload or payload

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
        if isinstance(payload, dict) and payload.get("type") == "traffic":
            payload = payload.get("payload")
        data = XuiCollector._unwrap_obj(payload)
        if isinstance(data, dict):
            online_clients = data.get("onlineClients")
            if isinstance(online_clients, list):
                return float(len(online_clients))
            last_online = data.get("lastOnlineMap")
            if isinstance(last_online, dict):
                return float(len(last_online))
            for k in ("count", "online", "onlineCount", "online_count"):
                if k in data and isinstance(data[k], (int, float)):
                    return float(data[k])
        if isinstance(data, list):
            return float(len(data))
        return None

    @staticmethod
    def _is_traffic_payload(payload):
        return isinstance(payload, dict) and payload.get("type") == "traffic"

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

    @staticmethod
    def _to_epoch_seconds(val, ctx):
        num = XuiCollector._to_float(val, ctx)
        if num is None:
            return None
        if num > 10_000_000_000:
            return num / 1000.0
        return num

    def collect(self):
        scrape_error = GaugeMetricFamily(
            "xui_exporter_last_scrape_error",
            "1 if the last scrape failed, otherwise 0",
        )
        scrape_duration = GaugeMetricFamily(
            "xui_exporter_scrape_duration_seconds",
            "Seconds spent fetching and parsing the JSON.",
        )
        scrape_size = GaugeMetricFamily(
            "xui_exporter_scrape_size_bytes",
            "Response size in bytes.",
        )
        last_scrape = GaugeMetricFamily(
            "xui_exporter_last_scrape_timestamp_seconds",
            "Unix timestamp of last successful scrape.",
        )
        fetch_errors = CounterMetricFamily(
            "xui_exporter_fetch_errors_total",
            "Total number of fetch errors.",
        )
        parse_errors = CounterMetricFamily(
            "xui_exporter_parse_errors_total",
            "Total number of JSON parse errors.",
        )

        start = time.perf_counter()
        inbounds_payload = None
        online_payload = None
        ws_payload = None
        total_size = 0

        inbounds_payload, online_payload, total_size = self._fetch_3xui()
        if self.ui_ws_url:
            ws_payload = self._fetch_3xui_ws()

        scrape_ok = any([inbounds_payload, online_payload, ws_payload])
        scrape_error.add_metric([], 0.0 if scrape_ok else 1.0)
        scrape_size.add_metric([], float(total_size))
        last_scrape.add_metric([], time.time())
        elapsed = time.perf_counter() - start
        scrape_duration.add_metric([], float(elapsed))
        fetch_errors.add_metric([], float(self._fetch_errors))
        parse_errors.add_metric([], float(self._parse_errors))

        yield scrape_error
        yield scrape_duration
        yield scrape_size
        yield last_scrape
        yield fetch_errors
        yield parse_errors

        if inbounds_payload is not None:
            inbounds = self._unwrap_obj(inbounds_payload)
        else:
            inbounds = None

        inbounds_total = GaugeMetricFamily("xui_inbounds_total", "Total inbounds count.")
        inbounds_enabled = GaugeMetricFamily("xui_inbounds_enabled_total", "Enabled inbounds count.")
        inbounds_disabled = GaugeMetricFamily("xui_inbounds_disabled_total", "Disabled inbounds count.")
        clients_total = GaugeMetricFamily("xui_clients_total", "Total clients across inbounds.")
        clients_enabled = GaugeMetricFamily("xui_clients_enabled_total", "Enabled clients across inbounds.")
        inbound_enabled = GaugeMetricFamily(
            "xui_inbound_enabled",
            "Inbound enabled flag (1/0).",
            labels=["inbound_id", "tag", "protocol"],
        )
        inbound_port = GaugeMetricFamily(
            "xui_inbound_port",
            "Inbound port.",
            labels=["inbound_id", "tag", "protocol"],
        )
        inbound_up = CounterMetricFamily(
            "xui_inbound_uplink_bytes_total",
            "Inbound uplink bytes (from panel).",
            labels=["inbound_id", "tag", "protocol"],
        )
        inbound_down = CounterMetricFamily(
            "xui_inbound_downlink_bytes_total",
            "Inbound downlink bytes (from panel).",
            labels=["inbound_id", "tag", "protocol"],
        )
        inbound_total = GaugeMetricFamily(
            "xui_inbound_total_bytes",
            "Inbound total limit bytes (from panel).",
            labels=["inbound_id", "tag", "protocol"],
        )
        inbound_expire = GaugeMetricFamily(
            "xui_inbound_expire_time_seconds",
            "Inbound expire time (epoch seconds, if provided).",
            labels=["inbound_id", "tag", "protocol"],
        )
        inbound_client_count = GaugeMetricFamily(
            "xui_inbound_clients_total",
            "Clients count per inbound.",
            labels=["inbound_id", "tag", "protocol"],
        )

        inbound_count = 0
        inbound_enabled_count = 0
        inbound_disabled_count = 0
        clients_count = 0
        clients_enabled_count = 0

        if isinstance(inbounds, list):
            for inbound in inbounds:
                if not isinstance(inbound, dict):
                    continue
                inbound_id = inbound.get("id") or inbound.get("_id") or inbound.get("inboundId") or "unknown"
                tag = inbound.get("tag") or inbound.get("remark") or inbound.get("name") or str(inbound_id)
                protocol = inbound.get("protocol") or inbound.get("type") or "unknown"
                enabled = inbound.get("enable")
                if enabled is None:
                    enabled = inbound.get("enabled")

                inbound_count += 1
                if bool(enabled):
                    inbound_enabled_count += 1
                else:
                    inbound_disabled_count += 1

                inbound_enabled.add_metric([str(inbound_id), str(tag), str(protocol)], 1.0 if enabled else 0.0)

                port = inbound.get("port")
                port_f = self._to_float(port, f"inbound.{inbound_id}.port") if port is not None else None
                if port_f is not None:
                    inbound_port.add_metric([str(inbound_id), str(tag), str(protocol)], port_f)

                up = inbound.get("up") or inbound.get("uplink")
                if up is not None:
                    up_f = self._to_float(up, f"inbound.{inbound_id}.up")
                    if up_f is not None:
                        inbound_up.add_metric([str(inbound_id), str(tag), str(protocol)], up_f)

                down = inbound.get("down") or inbound.get("downlink")
                if down is not None:
                    down_f = self._to_float(down, f"inbound.{inbound_id}.down")
                    if down_f is not None:
                        inbound_down.add_metric([str(inbound_id), str(tag), str(protocol)], down_f)

                total = inbound.get("total") or inbound.get("totalGB") or inbound.get("totalBytes")
                if total is not None:
                    total_f = self._to_float(total, f"inbound.{inbound_id}.total")
                    if total_f is not None:
                        inbound_total.add_metric([str(inbound_id), str(tag), str(protocol)], total_f)

                expire = inbound.get("expire") or inbound.get("expiryTime") or inbound.get("expiration")
                if expire is not None:
                    exp_f = self._to_epoch_seconds(expire, f"inbound.{inbound_id}.expire")
                    if exp_f is not None:
                        inbound_expire.add_metric([str(inbound_id), str(tag), str(protocol)], exp_f)

                clients = []
                for key in ("clientStats", "clients"):
                    if isinstance(inbound.get(key), list):
                        clients.extend([c for c in inbound.get(key) if isinstance(c, dict)])

                settings = inbound.get("settings")
                if isinstance(settings, str):
                    try:
                        settings = json.loads(settings)
                    except Exception:
                        settings = None
                if isinstance(settings, dict) and isinstance(settings.get("clients"), list):
                    clients.extend([c for c in settings.get("clients") if isinstance(c, dict)])

                if clients:
                    inbound_client_count.add_metric([str(inbound_id), str(tag), str(protocol)], float(len(clients)))
                    clients_count += len(clients)
                    for c in clients:
                        enabled_c = c.get("enable")
                        if enabled_c is None:
                            enabled_c = c.get("enabled")
                        if enabled_c is None or bool(enabled_c):
                            clients_enabled_count += 1

        inbounds_total.add_metric([], float(inbound_count))
        inbounds_enabled.add_metric([], float(inbound_enabled_count))
        inbounds_disabled.add_metric([], float(inbound_disabled_count))
        clients_total.add_metric([], float(clients_count))
        clients_enabled.add_metric([], float(clients_enabled_count))

        yield inbounds_total
        yield inbounds_enabled
        yield inbounds_disabled
        yield clients_total
        yield clients_enabled
        yield inbound_enabled
        yield inbound_port
        yield inbound_up
        yield inbound_down
        yield inbound_total
        yield inbound_expire
        yield inbound_client_count

        user_map = self._load_user_map(self.user_map_file)
        if inbounds_payload:
            ui_map = self._build_user_map_from_3xui(inbounds_payload)
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
        if ws_payload is not None:
            users_online = self._extract_online_count(ws_payload)
        if users_online is None and online_payload is not None:
            users_online = self._extract_online_count(online_payload)

        if users_online is not None:
            users_online_g = GaugeMetricFamily("xray_users_online", "Online users count.")
            users_online_g.add_metric([], float(users_online))
            yield users_online_g

        uptime = None
        if ws_payload is not None:
            uptime = self._extract_uptime(ws_payload)
        if uptime is not None:
            uptime_g = GaugeMetricFamily("xray_uptime_seconds", "Uptime in seconds.")
            uptime_g.add_metric([], float(uptime))
            yield uptime_g


def run_exporter():
    ap = argparse.ArgumentParser(description="Expose Prometheus metrics from 3x-ui API.")
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
    logging.info(
        "Starting 3x-ui exporter: listen=%s:%d ui=%s:%s timeout=%ss",
        host,
        port,
        args.ui_host,
        args.ui_port,
        args.timeout,
    )

    REGISTRY.register(
        XuiCollector(
            args.timeout,
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
    print(f"Serving on http://{host}:{port}/metrics; pulling from 3x-ui", flush=True)

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run_exporter()