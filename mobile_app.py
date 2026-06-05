#!/usr/bin/env python3
"""Mobile web viewer for Feishu sheet account status."""

import argparse
import hmac
import ipaddress
import json
import mimetypes
import os
import secrets
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")


def _load_dotenv():
    """加载同目录 .env（不覆盖已存在的环境变量）。无第三方依赖。"""
    path = os.path.join(BASE_DIR, ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception:
        pass


_load_dotenv()

from tools.feishu_sheet import feishu_sheet_to_dict, load_mapping, read_servers  # noqa: E402


ADMIN_USERNAME = os.environ.get("VIEWER_ADMIN_USER", "Admin")
ADMIN_PASSWORD = os.environ.get("VIEWER_ADMIN_PASSWORD", "")
ALL_SHEETS = "__all__"
SESSION_TTL = 12 * 60 * 60
MAX_BODY_SIZE = 1024 * 1024

_SESSIONS = {}
_IP_LOCATION_CACHE = {}


def _now():
    return int(time.time())


def _natural_key(value):
    text = str(value)
    return (0, int(text)) if text.isdigit() else (1, text.lower())


def _load_sheets():
    sheets, _ = load_mapping()
    return sheets or {}


def _sheet_names():
    return sorted(_load_sheets().keys(), key=str.lower)


def _resolve_sheet_name(name):
    raw = str(name or "").strip()
    if raw in {"", ALL_SHEETS, "全部"} or raw.lower() == "all":
        return ALL_SHEETS
    sheets = _load_sheets()
    for key in sheets:
        if key.lower() == raw.lower():
            return key
    return None


def _load_sheet_data(sheet_name):
    sheets = _load_sheets()
    token = sheets.get(sheet_name)
    if not token:
        raise RuntimeError(f"未配置飞书表格: {sheet_name}")
    data = feishu_sheet_to_dict(token)
    if not isinstance(data, dict):
        raise RuntimeError(f"飞书表格读取失败: {sheet_name}")

    return data, {"source": "feishu", "error": ""}


def _iter_accounts(raw_data):
    account_ids = [key for key in raw_data if key and not str(key).startswith("_")]
    for account_id in sorted(account_ids, key=_natural_key):
        account = raw_data.get(account_id)
        if isinstance(account, dict):
            yield str(account_id), account


def _first_text(account, keys):
    for key in keys:
        value = account.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _server_raw(account):
    return _first_text(account, ("服务器", "server"))


def _display_server(server):
    text = str(server or "").strip()
    if text == "0":
        return "本机"
    if not text:
        return "无编号"
    return text


def _leading_time_minutes(log_text):
    text = str(log_text or "").strip()
    if len(text) < 5 or text[2] != ":":
        return None
    try:
        hour = int(text[0:2])
        minute = int(text[3:5])
        second = 0
        if len(text) >= 8 and text[5] == ":":
            second = int(text[6:8])
    except ValueError:
        return None
    if hour > 23 or minute > 59 or second > 59:
        return None
    return hour * 60 + minute + second / 60


def _current_minutes(offset_hours):
    now = time.gmtime(time.time() + offset_hours * 3600)
    return now.tm_hour * 60 + now.tm_min + now.tm_sec / 60


def _circular_minute_diff(a, b):
    diff = abs(a - b)
    return min(diff, 1440 - diff)


def _is_stale_log(log_text, server_raw):
    log_minutes = _leading_time_minutes(log_text)
    if log_minutes is None:
        return False
    try:
        server_num = int(str(server_raw or "").strip())
    except ValueError:
        server_num = 0
    is_remote_server = server_num >= 2
    base_minutes = _current_minutes(0 if is_remote_server else 8)
    limit = 10 if is_remote_server else 30
    return _circular_minute_diff(log_minutes, base_minutes) > limit


def _account_has_error(account_id, account):
    log_text = _first_text(account, ("日志", "log"))
    if not log_text:
        return False
    if "休息" in log_text:
        return False
    if "异常" in log_text or "失败" in log_text:
        return True
    return _is_stale_log(log_text, _server_raw(account))


def _account_payload(account_id, account, include_password=False, sheet_name=""):
    nickname = _first_text(account, ("nickname", "昵称"))
    log_text = _first_text(account, ("日志", "log"))
    server_raw = _server_raw(account)
    payload = {
        "id": account_id,
        "scene": sheet_name,
        "nickname": nickname,
        "account": _first_text(account, ("login_email", "账号", "account", "登录邮箱")),
        "server": _display_server(server_raw),
        "serverRaw": server_raw,
        "log": log_text,
        "location": _first_text(account, ("位置", "seat", "location")),
    }
    if include_password:
        payload["password"] = _first_text(account, ("login_password", "密码", "password", "登录密码"))
    return payload


def _find_user(raw_data, username, password):
    username = str(username or "").strip()
    password = str(password or "")
    if not username or not password:
        return None

    for account_id, account in _iter_accounts(raw_data):
        login_name = _first_text(account, ("login_email", "账号", "登录邮箱"))
        login_password = _first_text(account, ("login_password", "密码", "登录密码"))
        candidates = {login_name, account_id}
        if username in candidates and hmac.compare_digest(password, login_password):
            return account_id, account
    return None


def _count_accounts(raw_data):
    return sum(1 for _ in _iter_accounts(raw_data))


def _server_sort_key(item):
    server, _ = item
    if server == "本机":
        return (0, 0)
    if server == "无编号":
        return (1, 0)
    return (2, int(server)) if str(server).isdigit() else (3, str(server).lower())


def _server_inventory():
    inventory = {}
    for server in read_servers():
        name = _display_server(server.get("number"))
        number = str(server.get("number") or "").strip()
        label = str(server.get("label") or "").strip()
        entry = {
            "name": name,
            "number": number,
            "ip": str(server.get("ip") or "").strip(),
            "ipLocation": _server_ip_location(server),
            "password": str(server.get("password") or "").strip(),
            "label": label,
            "status": str(server.get("status") or "").strip(),
        }
        if name not in inventory:
            inventory[name] = entry
        else:
            current = inventory[name]
            for key, value in entry.items():
                if not current.get(key) and value:
                    current[key] = value
    return inventory


def _server_ip_location(server):
    for key in ("ip_location", "IP归属地", "归属地", "地区", "location", "region", "country", "城市"):
        value = str(server.get(key) or "").strip()
        if value:
            return value
    return ""


def _lookup_ip_location(ip):
    ip_text = str(ip or "").strip()
    if not ip_text:
        return ""
    if ip_text in _IP_LOCATION_CACHE:
        return _IP_LOCATION_CACHE[ip_text]

    try:
        parsed_ip = ipaddress.ip_address(ip_text)
    except ValueError:
        _IP_LOCATION_CACHE[ip_text] = ""
        return ""

    if parsed_ip.is_loopback:
        location = "本机"
    elif parsed_ip.is_private:
        location = "内网"
    elif not parsed_ip.is_global:
        location = "非公网"
    else:
        location = ""
        try:
            request = Request(
                f"http://ip-api.com/json/{ip_text}?fields=status,country,regionName,city,message&lang=zh-CN",
                headers={"User-Agent": "TicketMatrixMobile/1.0"},
            )
            with urlopen(request, timeout=2) as response:
                data = json.loads(response.read().decode("utf-8"))
            if data.get("status") == "success":
                parts = []
                for key in ("country", "regionName", "city"):
                    value = str(data.get(key) or "").strip()
                    if value and value not in parts:
                        parts.append(value)
                location = " ".join(parts)
        except Exception as exc:
            print(f"[mobile_app] IP归属地查询失败 {ip_text}: {exc}")

    _IP_LOCATION_CACHE[ip_text] = location
    return location


def _scene_stats(current_sheet=None, current_raw_data=None):
    stats = []
    for sheet_name in _sheet_names():
        raw_data = current_raw_data if sheet_name == current_sheet else _load_sheet_data(sheet_name)[0]
        stats.append({"name": sheet_name, "count": _count_accounts(raw_data)})
    return stats


def _all_scene_server_stats(current_sheet=None, current_raw_data=None):
    counts = {}
    error_counts = {}
    scene_counts = {}
    servers = _server_inventory()
    for server_name in servers:
        counts.setdefault(server_name, 0)
        error_counts.setdefault(server_name, 0)
        scene_counts.setdefault(server_name, {})

    for sheet_name in _sheet_names():
        raw_data = current_raw_data if sheet_name == current_sheet else _load_sheet_data(sheet_name)[0]
        for account_id, account in _iter_accounts(raw_data):
            server = _display_server(_server_raw(account))
            counts[server] = counts.get(server, 0) + 1
            scene_bucket = scene_counts.setdefault(server, {})
            scene_bucket[sheet_name] = scene_bucket.get(sheet_name, 0) + 1
            if _account_has_error(account_id, account):
                error_counts[server] = error_counts.get(server, 0) + 1

    return [
        {
            "name": name,
            "number": servers.get(name, {}).get("number", "0" if name == "本机" else ""),
            "count": count,
            "errorCount": error_counts.get(name, 0),
            "hasError": bool(error_counts.get(name, 0)),
            "label": servers.get(name, {}).get("label", ""),
            "ip": servers.get(name, {}).get("ip", ""),
            "ipLocation": servers.get(name, {}).get("ipLocation", ""),
            "sceneCounts": [
                {"name": scene, "count": scene_count}
                for scene, scene_count in sorted(scene_counts.get(name, {}).items(), key=lambda item: item[0].lower())
            ],
        }
        for name, count in sorted(counts.items(), key=_server_sort_key)
    ]


def _admin_stats(sheet_name=None, raw_data=None):
    return {
        "servers": _all_scene_server_stats(sheet_name, raw_data),
        "scenes": _scene_stats(sheet_name, raw_data),
    }


def _accounts_for_sheet(sheet_name, raw_data, include_password=False):
    return [
        _account_payload(account_id, account, include_password=include_password, sheet_name=sheet_name)
        for account_id, account in _iter_accounts(raw_data)
    ]


def _accounts_for_all_sheets(include_password=False):
    accounts = []
    for sheet_name in _sheet_names():
        raw_data, _ = _load_sheet_data(sheet_name)
        accounts.extend(_accounts_for_sheet(sheet_name, raw_data, include_password=include_password))
    return accounts


def _find_user_all_sheets(username, password):
    accounts = []
    for sheet_name in _sheet_names():
        raw_data, _ = _load_sheet_data(sheet_name)
        found = _find_user(raw_data, username, password)
        if not found:
            continue
        account_id, account = found
        accounts.append(_account_payload(account_id, account, include_password=False, sheet_name=sheet_name))
    return accounts


def _new_session(role, sheet_name, account_id=None, username="", password=""):
    token = secrets.token_urlsafe(32)
    _SESSIONS[token] = {
        "role": role,
        "sheet": sheet_name,
        "account_id": account_id,
        "username": username,
        "password": password,
        "created_at": _now(),
    }
    return token


def _cleanup_sessions():
    expires_before = _now() - SESSION_TTL
    for token, session in list(_SESSIONS.items()):
        if session.get("created_at", 0) < expires_before:
            _SESSIONS.pop(token, None)


def _session_from_headers(headers):
    _cleanup_sessions()
    auth = headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth.split(" ", 1)[1].strip()
    session = _SESSIONS.get(token)
    if not session:
        return None
    return token, session


def _dashboard_for_session(session, sheet_name=None):
    sheet_name = sheet_name or session["sheet"]
    if session["role"] == "admin":
        if sheet_name == ALL_SHEETS:
            accounts = _accounts_for_all_sheets(include_password=True)
            meta = {"source": "feishu", "error": ""}
        else:
            raw_data, meta = _load_sheet_data(sheet_name)
            accounts = _accounts_for_sheet(sheet_name, raw_data, include_password=True)
        return {
            "role": "admin",
            "sheet": sheet_name,
            "accounts": accounts,
            "stats": _admin_stats(sheet_name if sheet_name != ALL_SHEETS else None, None if sheet_name == ALL_SHEETS else raw_data),
            "meta": meta,
        }

    if sheet_name == ALL_SHEETS:
        accounts = _find_user_all_sheets(session.get("username"), session.get("password"))
        if not accounts:
            return {
                "role": "user",
                "sheet": sheet_name,
                "accounts": [],
                "meta": {"source": "feishu", "error": ""},
                "message": "该场次下没有信息",
            }
        return {
            "role": "user",
            "sheet": sheet_name,
            "accounts": accounts,
            "meta": {"source": "feishu", "error": ""},
        }

    raw_data, meta = _load_sheet_data(sheet_name)
    found = _find_user(raw_data, session.get("username"), session.get("password"))
    if not found:
        return {
            "role": "user",
            "sheet": sheet_name,
            "account": None,
            "meta": meta,
            "message": "该场次下没有信息",
        }
    target_id, account = found
    session["account_id"] = target_id
    return {
        "role": "user",
        "sheet": sheet_name,
        "account": _account_payload(target_id, account, include_password=False, sheet_name=sheet_name),
        "meta": meta,
    }


class MobileAppHandler(BaseHTTPRequestHandler):
    server_version = "TicketMatrixMobile/1.0"

    def log_message(self, fmt, *args):
        print(f"[mobile_app] {self.address_string()} - {fmt % args}")

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status, message):
        self._send_json(status, {"ok": False, "message": message})

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > MAX_BODY_SIZE:
            raise ValueError("请求体过大")
        body = self.rfile.read(length) if length else b"{}"
        if not body:
            return {}
        return json.loads(body.decode("utf-8"))

    def _send_static(self, relative_path):
        relative_path = relative_path.strip("/") or "index.html"
        if relative_path == "app":
            relative_path = "index.html"
        full_path = os.path.abspath(os.path.join(WEB_DIR, relative_path))
        if not full_path.startswith(os.path.abspath(WEB_DIR) + os.sep):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not os.path.isfile(full_path):
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        content_type, _ = mimetypes.guess_type(full_path)
        with open(full_path, "rb") as f:
            body = f.read()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/sheets":
            sheets = _sheet_names()
            self._send_json(HTTPStatus.OK, {"ok": True, "sheets": sheets})
            return

        if parsed.path == "/api/ip-location":
            auth = _session_from_headers(self.headers)
            if not auth:
                self._send_error(HTTPStatus.UNAUTHORIZED, "请先登录")
                return
            ip = parse_qs(parsed.query).get("ip", [""])[0]
            location = _lookup_ip_location(ip)
            self._send_json(HTTPStatus.OK, {"ok": True, "location": location})
            return

        if parsed.path == "/api/data":
            auth = _session_from_headers(self.headers)
            if not auth:
                self._send_error(HTTPStatus.UNAUTHORIZED, "请先登录")
                return
            token, session = auth
            requested_sheet = parse_qs(parsed.query).get("sheet", [""])[0]
            sheet_name = session["sheet"]
            if requested_sheet:
                sheet_name = _resolve_sheet_name(requested_sheet)
                if not sheet_name:
                    self._send_error(HTTPStatus.BAD_REQUEST, "请选择正确的场次")
                    return
            session["sheet"] = sheet_name
            try:
                data = _dashboard_for_session(session, sheet_name=sheet_name)
            except Exception as exc:
                print(f"[mobile_app] 飞书刷新失败: {exc}")
                self._send_error(HTTPStatus.BAD_GATEWAY, "刷新失败，请重新刷新")
                return
            _SESSIONS[token] = session
            self._send_json(HTTPStatus.OK, {"ok": True, "data": data})
            return

        if parsed.path == "/":
            self._send_static("index.html")
            return
        self._send_static(parsed.path)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/login":
            self._send_error(HTTPStatus.NOT_FOUND, "接口不存在")
            return

        try:
            payload = self._read_json()
        except Exception:
            self._send_error(HTTPStatus.BAD_REQUEST, "请求格式不正确")
            return

        sheet_name = _resolve_sheet_name(payload.get("sheet"))
        if not sheet_name:
            self._send_error(HTTPStatus.BAD_REQUEST, "请选择正确的表格")
            return

        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        raw_data = {}
        meta = {"source": "feishu", "error": ""}
        if sheet_name != ALL_SHEETS:
            try:
                raw_data, meta = _load_sheet_data(sheet_name)
            except Exception as exc:
                print(f"[mobile_app] 飞书登录读取失败: {exc}")
                self._send_error(HTTPStatus.BAD_GATEWAY, "刷新失败，请重新刷新")
                return

        if ADMIN_PASSWORD and username == ADMIN_USERNAME and hmac.compare_digest(password, ADMIN_PASSWORD):
            try:
                accounts = (
                    _accounts_for_all_sheets(include_password=True)
                    if sheet_name == ALL_SHEETS
                    else _accounts_for_sheet(sheet_name, raw_data, include_password=True)
                )
                stats = _admin_stats(
                    sheet_name if sheet_name != ALL_SHEETS else None,
                    None if sheet_name == ALL_SHEETS else raw_data,
                )
            except Exception as exc:
                print(f"[mobile_app] 飞书管理员读取失败: {exc}")
                self._send_error(HTTPStatus.BAD_GATEWAY, "刷新失败，请重新刷新")
                return
            token = _new_session(
                "admin",
                sheet_name,
                username=username,
            )
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "token": token,
                    "data": {
                        "role": "admin",
                        "sheet": sheet_name,
                        "accounts": accounts,
                        "stats": stats,
                        "meta": meta,
                    },
                },
            )
            return

        if sheet_name == ALL_SHEETS:
            try:
                accounts = _find_user_all_sheets(username, password)
            except Exception as exc:
                print(f"[mobile_app] 飞书用户读取失败: {exc}")
                self._send_error(HTTPStatus.BAD_GATEWAY, "刷新失败，请重新刷新")
                return
            if not accounts:
                self._send_error(HTTPStatus.UNAUTHORIZED, "账号或密码不正确")
                return
            token = _new_session(
                "user",
                sheet_name,
                username=username,
                password=password,
            )
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "token": token,
                    "data": {"role": "user", "sheet": sheet_name, "accounts": accounts, "meta": meta},
                },
            )
            return

        found = _find_user(raw_data, username, password)
        if not found:
            self._send_error(HTTPStatus.UNAUTHORIZED, "账号或密码不正确")
            return

        account_id, account = found
        account_data = _account_payload(account_id, account, include_password=False, sheet_name=sheet_name)
        token = _new_session(
            "user",
            sheet_name,
            account_id=account_id,
            username=username,
            password=password,
        )
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "token": token,
                "data": {"role": "user", "sheet": sheet_name, "account": account_data, "meta": meta},
            },
        )

def main():
    parser = argparse.ArgumentParser(description="TicketMatrix 手机网页查看器")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址，默认 0.0.0.0")
    parser.add_argument("--port", type=int, default=8765, help="监听端口，默认 8765")
    args = parser.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), MobileAppHandler)
    print(f"手机网页查看器已启动: http://{args.host}:{args.port}")
    print("局域网手机访问时，请使用本机局域网 IP，例如 http://192.168.x.x:8765")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
