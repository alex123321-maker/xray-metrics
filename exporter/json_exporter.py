#!/usr/bin/env python3
from exporter_common import run_exporter


if __name__ == "__main__":
    run_exporter(require_source=True)

"""
import argparse, json, time, os, sys, logging, ssl, asyncio
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from http.cookiejar import CookieJar
from urllib.request import build_opener, HTTPCookieProcessor, HTTPSHandler
from prometheus_client import start_http_server, REGISTRY
from prometheus_client.core import GaugeMetricFamily, CounterMetricFamily

try:
    import websockets
except Exception:
    websockets = None

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
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

        # ---- xray info ----
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
            else:
                logging.debug("xray version not found in payload")
            yield xray_info

        # ---- optional xray api ----
        if self.api_source:
            try:
                api_data, _ = self._fetch_from_source(self.api_source, self.api_timeout, "api")
            except Exception as e:
                logging.warning("Xray API fetch failed: %s", e)

        # ---- optional 3x-ui api ----
        if self.ui_host and self.ui_port:
            ui_inbounds, ui_online = self._fetch_3xui()
            if self.ui_ws_url:
                ws_data = self._fetch_3xui_ws()

        # ---- observatory ----
        if data is not None:
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
        online_users = 0
        if data is not None:
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
                                if conn_f > 0:
                                    online_users += 1

            total_down.add_metric([], total_dl)
            total_up.add_metric([], total_ul)

            yield total_down; yield total_up
            yield inbound_down; yield inbound_up
            yield outbound_down; yield outbound_up
            yield user_down; yield user_up; yield user_up_bytes; yield user_conn

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
            # common locations: inbound['clients'], inbound['clientStats'], inbound['settings']['clients']
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
            # sometimes list under data["clients"]
            for k in ("clients", "onlineClients", "list"):
                if isinstance(data.get(k), list):
                    return float(len(data.get(k)))
        if isinstance(data, list):
            return float(len(data))
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
        api_data = None
        ws_data = None
        ui_inbounds = None
        ui_online = None
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

        # ---- optional xray api ----
        if self.api_source:
            try:
                api_data, _ = self._fetch_from_source(self.api_source, self.api_timeout, "api")
            except Exception as e:
                logging.warning("Xray API fetch failed: %s", e)

        # ---- optional 3x-ui api ----
        if self.ui_host and self.ui_port:
            ui_inbounds, ui_online = self._fetch_3xui()
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
            if data is not None:
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

        yield total_down; yield total_up
        yield inbound_down; yield inbound_up
        yield outbound_down; yield outbound_up
        yield user_down; yield user_up; yield user_up_bytes; yield user_conn

        # ---- users online & aliases ----
        api_online = None
        api_uptime = None
        ws_online = None
        ws_uptime = None
        if api_data is not None:
            online_keys = {
                "online", "online_users", "users_online", "onlineUsers", "onlineUser",
                "online_count", "active_users", "activeUsers", "connections"
            }
            uptime_keys = {
                "uptime", "uptime_seconds", "uptime_sec", "uptimeSeconds",
                "uptime_ms", "uptimeMs", "up_time", "upTime"
            }
            found = self._find_numeric_by_keys(api_data, online_keys)
            if found:
                api_online, _ = found
            found = self._find_numeric_by_keys(api_data, uptime_keys)
            if found:
                api_uptime, k = found
                if "ms" in str(k).lower():
                    api_uptime = api_uptime / 1000.0

        if ws_data and isinstance(ws_data, dict):
            status = (ws_data.get("status") or {}).get("payload")
            traffic = (ws_data.get("traffic") or {}).get("payload")
            if isinstance(status, dict):
                if isinstance(status.get("uptime"), (int, float)):
                    ws_uptime = float(status.get("uptime"))
            if isinstance(traffic, dict):
                online_clients = traffic.get("onlineClients")
                if isinstance(online_clients, list):
                    ws_online = float(len(online_clients))

        online_from_ui = None
        if ui_online is not None:
            online_from_ui = self._extract_online_count(ui_online)

        users_online_g = GaugeMetricFamily(
            "xray_users_online",
            "Current online users (best effort)."
        )
        if ws_online is not None:
            users_online_g.add_metric([], float(ws_online))
        elif online_from_ui is not None:
            users_online_g.add_metric([], float(online_from_ui))
        elif api_online is not None:
            users_online_g.add_metric([], float(api_online))
        else:
            users_online_g.add_metric([], float(online_users))
        yield users_online_g

        if ws_uptime is not None:
            xray_uptime_g = GaugeMetricFamily(
                "xray_uptime_seconds",
                "Xray uptime in seconds (from API when available)."
            )
            xray_uptime_g.add_metric([], float(ws_uptime))
            yield xray_uptime_g
        elif api_uptime is not None:
            xray_uptime_g = GaugeMetricFamily(
                "xray_uptime_seconds",
                "Xray uptime in seconds (from API when available)."
            )
            xray_uptime_g.add_metric([], float(api_uptime))
            yield xray_uptime_g

        if ui_inbounds is not None:
            api_user_map = self._build_user_map_from_3xui(ui_inbounds)
            for k, v in api_user_map.items():
                self.user_map.setdefault(k, v)

        if self.user_map:
            user_alias = GaugeMetricFamily(
                "xray_user_alias_info",
                "User alias/comment mapping (value is always 1).",
                labels=["user", "alias"]
            )
            for user, alias in self.user_map.items():
                user_alias.add_metric([str(user), str(alias)], 1.0)
            yield user_alias

        

        # ---- all numeric vars (generic) ----
        if data is not None:
            all_vars = GaugeMetricFamily(
                "xray_var",
                "All numeric values from Xray debug/vars (path label).",
                labels=["path"]
            )
            paths_logged = []
            for path, val in self._iter_numeric_paths(data):
                if not path:
                    continue
                all_vars.add_metric([path], val)
                paths_logged.append(path)
            yield all_vars

def _parse_listen(s: str):
    host, sep, port = s.rpartition(":")
    host = host if sep else "0.0.0.0"
    return host, int(port or 9108)

def main():
    ap = argparse.ArgumentParser(description="Expose Prometheus metrics from a JSON (file or URL).")
    ap.add_argument("--source", default=os.getenv("SOURCE"),
                    required=not bool(os.getenv("SOURCE")),
                    help="Путь к JSON-файлу или HTTP(S) URL (или ENV SOURCE).")
    ap.add_argument("--api-source", default=os.getenv("XRAY_API_SOURCE"),
                    help="HTTP(S) URL Xray API для uptime/online (ENV XRAY_API_SOURCE).")
    ap.add_argument("--listen", default=os.getenv("LISTEN","0.0.0.0:9108"),
                    help="Адрес:порт HTTP (или ENV LISTEN).")
    ap.add_argument("--timeout", type=int, default=int(os.getenv("TIMEOUT","5")),
                    help="Таймаут чтения, сек (или ENV TIMEOUT).")
    ap.add_argument("--api-timeout", type=int, default=int(os.getenv("XRAY_API_TIMEOUT","0") or 0),
                    help="Таймаут Xray API, сек (ENV XRAY_API_TIMEOUT; 0 = TIMEOUT).")
    ap.add_argument("--delay-unit", choices=["ms","s"], default=os.getenv("DELAY_UNIT","ms"),
                    help='Единицы "delay": ms|s (или ENV DELAY_UNIT).')
    ap.add_argument("--user-map-file", default=os.getenv("USER_MAP_FILE"),
                    help="JSON-файл маппинга user->alias (ENV USER_MAP_FILE).")
    ap.add_argument("--ui-host", default=os.getenv("UI_HOST"),
                    help="Хост 3x-ui API (ENV UI_HOST).")
    ap.add_argument("--ui-port", type=int, default=int(os.getenv("UI_PORT", "0") or 0),
                    help="Порт 3x-ui API (ENV UI_PORT).")
    ap.add_argument("--ui-basepath", default=os.getenv("UI_BASEPATH", "/"),
                    help="Basepath 3x-ui (ENV UI_BASEPATH).")
    ap.add_argument("--ui-scheme", default=os.getenv("UI_SCHEME", "https"),
                    help="Схема 3x-ui (http|https).")
    ap.add_argument("--ui-username", default=os.getenv("UI_USERNAME"),
                    help="Логин 3x-ui.")
    ap.add_argument("--ui-password", default=os.getenv("UI_PASSWORD"),
                    help="Пароль 3x-ui.")
    ap.add_argument("--ui-bearer-token", default=os.getenv("UI_BEARER_TOKEN"),
                    help="Bearer token 3x-ui (если включён).")
    ap.add_argument("--ui-api-key", default=os.getenv("UI_API_KEY"),
                    help="apiKey header (если требуется).")
    ap.add_argument("--ui-login-path", default=os.getenv("UI_LOGIN_PATH", "/login"),
                    help="Путь Login (ENV UI_LOGIN_PATH).")
    ap.add_argument("--ui-inbounds-path", default=os.getenv("UI_INBOUNDS_PATH", "/api/inbounds"),
                    help="Путь Inbounds (ENV UI_INBOUNDS_PATH).")
    ap.add_argument("--ui-online-path", default=os.getenv("UI_ONLINE_PATH", "/api/onlineClients"),
                    help="Путь Online Clients (ENV UI_ONLINE_PATH).")
    ap.add_argument("--ui-insecure", action="store_true", default=os.getenv("UI_INSECURE", "false").lower() == "true",
                    help="Отключить проверку TLS (ENV UI_INSECURE=true).")
    ap.add_argument("--ui-ws-url", default=os.getenv("UI_WS_URL"),
                    help="WebSocket URL 3x-ui (ENV UI_WS_URL).")
    ap.add_argument("--ui-ws-timeout", type=int, default=int(os.getenv("UI_WS_TIMEOUT", "5")),
                    help="Таймаут WS (сек).")
    ap.add_argument("--ui-ws-cache-ttl", type=int, default=int(os.getenv("UI_WS_CACHE_TTL", "30")),
                    help="TTL WS кэша (сек).")
    ap.add_argument("--ui-ws-messages", type=int, default=int(os.getenv("UI_WS_MESSAGES", "3")),
                    help="Сколько WS сообщений читать за сессию.")
    args = ap.parse_args()

    host, port = _parse_listen(args.listen)
    api_timeout = args.api_timeout if args.api_timeout and args.api_timeout > 0 else args.timeout
    logging.info(
        "Starting exporter: listen=%s:%d source=%s timeout=%ss delay_unit=%s api_source=%s api_timeout=%ss",
        host, port, args.source, args.timeout, args.delay_unit, args.api_source, api_timeout
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
        while True: time.sleep(3600)
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
"""

