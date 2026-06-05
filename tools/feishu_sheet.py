#!/usr/bin/env python3
"""feishu_sheet - 飞书电子表格 → config JSON 自动同步

从飞书在线电子表格读取配置数据，自动同步到本地 config/*.json。
支持后台轮询模式（watch）和手动单次同步。

飞书表格结构约定：
  第 1 行: 标题行
  第 2~4 行: 默认字段（目标URL / 开售时间 / NST API Key）
  空行后: 表头行 + 账号数据行

用法:
  python tools/feishu_sheet.py              # 单次同步
  python tools/feishu_sheet.py --watch      # 持续监听
  python tools/feishu_sheet.py --interval 30  # 30秒轮询
"""

import json
import os
import sys
import time
import threading

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(BASE_DIR, "config")
MAPPING_FILE = os.path.join(CONFIG_DIR, "feishu_sheets.json")

if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from tools.config_schema import DEFAULTS_LABEL_MAP, ACCOUNT_HEADER_MAP

try:
    import requests
except ImportError:
    print("❌ 需要安装 requests: pip install requests")
    sys.exit(1)

_http = requests.Session()

# ==================== 飞书凭证（复用 payment/feishu.py） ====================

FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "cli_aa9b50308eb85cd9")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "sbGBkbtRVPqLGjZx51gwXbu0Lfi1XYOb")
API_BASE = "https://open.feishu.cn/open-apis"

_token_cache = {"token": None, "expire": 0}


def _get_token():
    """获取 tenant_access_token，带简单缓存"""
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expire"]:
        return _token_cache["token"]

    resp = _http.post(
        f"{API_BASE}/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        timeout=10,
    )
    data = resp.json()
    token = data.get("tenant_access_token")
    if not token:
        raise RuntimeError(f"飞书 token 获取失败: {data}")

    _token_cache["token"] = token
    _token_cache["expire"] = now + data.get("expire", 7200) - 300
    return token


def _api_get(path, params=None):
    """带鉴权的 GET 请求"""
    token = _get_token()
    resp = _http.get(
        f"{API_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=15,
    )
    data = resp.json()
    if data.get("code", 0) != 0:
        raise RuntimeError(f"飞书 API 错误: {data.get('msg', data)}")
    return data


def _api_put(path, body):
    """带鉴权的 PUT 请求（用于写入表格）"""
    token = _get_token()
    resp = _http.put(
        f"{API_BASE}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json=body,
        timeout=15,
    )
    data = resp.json()
    if data.get("code", 0) != 0:
        raise RuntimeError(f"飞书写入错误: {data.get('msg', data)}")
    return data


def _api_post(path, body, timeout=60):
    """带鉴权的 POST 请求"""
    token = _get_token()
    resp = _http.post(
        f"{API_BASE}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json=body,
        timeout=timeout,
    )
    data = resp.json()
    if data.get("code", 0) != 0:
        raise RuntimeError(f"飞书写入错误: {data.get('msg', data)}")
    return data


# ==================== Wiki → Spreadsheet 解析 ====================

_wiki_token_cache = {}
_sheet_id_cache = {}


def _resolve_token(token_or_wiki):
    """将 wiki node token 或 spreadsheet token 统一解析为 spreadsheet_token。

    支持三种输入：
      - 纯 spreadsheet token（直接返回）
      - wiki node token（通过 API 获取关联的 spreadsheet token）
      - 飞书 URL（自动提取 token 部分）
    """
    raw = token_or_wiki.strip()

    if "feishu.cn/" in raw:
        if "/sheets/" in raw:
            raw = raw.split("/sheets/")[1].split("?")[0].split("/")[0]
            return raw
        elif "/wiki/" in raw:
            raw = raw.split("/wiki/")[1].split("?")[0].split("/")[0]

    if raw in _wiki_token_cache:
        return _wiki_token_cache[raw]

    try:
        data = _api_get("/wiki/v2/spaces/get_node", params={"token": raw})
        node = data.get("data", {}).get("node", {})
        if node.get("obj_type") == "sheet":
            spreadsheet_token = node["obj_token"]
            _wiki_token_cache[raw] = spreadsheet_token
            return spreadsheet_token
    except Exception:
        pass

    return raw


# ==================== 读取飞书表格 ====================

def _get_first_sheet_id(spreadsheet_token):
    """获取电子表格第一个 sheet 的 sheetId"""
    cached = _sheet_id_cache.get(spreadsheet_token)
    if cached:
        return cached

    data = _api_get(f"/sheets/v2/spreadsheets/{spreadsheet_token}/metainfo")
    sheets = data.get("data", {}).get("sheets", [])
    if not sheets:
        sheets = data.get("data", {}).get("properties", {}).get("sheets", [])
    if not sheets:
        raise RuntimeError(f"表格 {spreadsheet_token} 中没有 sheet")
    sheet_id = sheets[0]["sheetId"]
    _sheet_id_cache[spreadsheet_token] = sheet_id
    return sheet_id


def _read_sheet_values(spreadsheet_token, sheet_id):
    """读取整个 sheet 的所有值，返回二维列表"""
    range_str = f"{sheet_id}"
    data = _api_get(
        f"/sheets/v2/spreadsheets/{spreadsheet_token}/values/{range_str}",
        params={"valueRenderOption": "ToString"},
    )
    values = data.get("data", {}).get("valueRange", {}).get("values", [])
    return values


def feishu_sheet_to_dict(token_or_wiki):
    """读取飞书电子表格，解析为统一配置 dict

    支持传入 spreadsheet token、wiki node token 或完整飞书 URL。

    Returns:
        dict: {"_defaults": {...}, "1": {...}, "2": {...}, ...}
    """
    spreadsheet_token = _resolve_token(token_or_wiki)
    sheet_id = _get_first_sheet_id(spreadsheet_token)
    rows = _read_sheet_values(spreadsheet_token, sheet_id)

    if not rows:
        return None

    defaults = {}
    header_row_idx = None

    for i, row in enumerate(rows):
        if not row:
            continue
        label = str(row[0] if row[0] is not None else "").strip()

        if label in DEFAULTS_LABEL_MAP:
            key = DEFAULTS_LABEL_MAP[label]
            val = str(row[1] if len(row) > 1 and row[1] is not None else "").strip()
            if val:
                defaults[key] = val
        elif label in ACCOUNT_HEADER_MAP:
            header_row_idx = i
            break

    if header_row_idx is None:
        for i, row in enumerate(rows):
            if row and str(row[0] if row[0] is not None else "").strip() == "账号ID":
                header_row_idx = i
                break

    if header_row_idx is None:
        print(f"  ⚠️ 飞书表格中未找到表头行")
        return None

    header_row = rows[header_row_idx]
    headers = []
    for cell in header_row:
        h = str(cell if cell is not None else "").strip()
        key = ACCOUNT_HEADER_MAP.get(h, h)
        headers.append(key)

    result = {"_defaults": defaults}

    for row in rows[header_row_idx + 1:]:
        if not row:
            continue
        account_id = str(row[0] if row[0] is not None else "").strip()
        if not account_id or account_id.startswith("_"):
            continue

        acc = {}
        for col_idx, key in enumerate(headers):
            if key == "account":
                continue
            val = str(row[col_idx] if col_idx < len(row) and row[col_idx] is not None else "").strip()
            if val:
                acc[key] = val

        if acc:
            result[account_id] = acc

    return result


# ==================== 服务器表读取 ====================

_SERVER_COL_MAP = {
    "编号": "number",
    "IP": "ip",
    "IP归属地": "ip_location",
    "归属地": "ip_location",
    "地区": "ip_location",
    "密码": "password",
    "备注": "label",
    "状态": "status",
}


def _get_servers_token():
    """从 feishu_sheets.json 获取服务器表 token。"""
    if not os.path.exists(MAPPING_FILE):
        return None
    try:
        with open(MAPPING_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("servers")
    except Exception:
        return None


def _parse_server_sheet(rows):
    """解析飞书服务器表。"""
    header_idx = None
    headers = []

    for i, row in enumerate(rows):
        if not row:
            continue
        first = str(row[0] if row[0] is not None else "").strip()
        if first in _SERVER_COL_MAP:
            header_idx = i
            for cell in row:
                raw = str(cell if cell is not None else "").strip()
                headers.append(_SERVER_COL_MAP.get(raw, raw))
            break

    if header_idx is None:
        return None, [], []

    servers = []
    for row in rows[header_idx + 1:]:
        if not row:
            continue
        entry = {}
        for col_idx, key in enumerate(headers):
            val = str(
                row[col_idx] if col_idx < len(row) and row[col_idx] is not None else ""
            ).strip()
            if val:
                entry[key] = val
        if entry.get("ip"):
            servers.append(entry)

    return header_idx, headers, servers


def read_servers():
    """从飞书服务器表读取服务器列表。"""
    token = _get_servers_token()
    if not token:
        return []

    try:
        spreadsheet_token = _resolve_token(token)
        sheet_id = _get_first_sheet_id(spreadsheet_token)
        rows = _read_sheet_values(spreadsheet_token, sheet_id)
    except Exception as e:
        print(f"  ⚠️ 读取飞书服务器表格失败: {e}")
        return []

    if not rows:
        return []

    _, _, servers = _parse_server_sheet(rows)
    return servers


def add_servers_to_feishu(servers):
    """向飞书服务器表追加服务器。按 IP 去重。"""
    if not servers:
        return 0

    token = _get_servers_token()
    if not token:
        raise RuntimeError("未配置服务器飞书表 token")

    spreadsheet_token = _resolve_token(token)
    sheet_id = _get_first_sheet_id(spreadsheet_token)
    rows = _read_sheet_values(spreadsheet_token, sheet_id)
    header_idx, _, existing = _parse_server_sheet(rows)
    if header_idx is None:
        raise RuntimeError("飞书服务器表中未找到表头，请确认包含：编号 / IP / 密码 / 备注 / 状态")

    existing_ips = {s.get("ip") for s in existing if s.get("ip")}
    max_number = 0
    for i, s in enumerate(existing, 1):
        number = s.get("number")
        if str(number).isdigit():
            max_number = max(max_number, int(number))
        else:
            max_number = max(max_number, i)

    values = []
    added = 0
    next_number = max_number + 1
    for server in servers:
        ip = str(server.get("ip", "")).strip()
        password = str(server.get("password", "")).strip()
        if not ip or ip in existing_ips:
            continue

        number = str(server.get("number", "")).strip()
        if not number.isdigit():
            number = str(next_number)
            next_number += 1

        values.append([
            number,
            ip,
            password,
            str(server.get("label", server.get("instance_id", ip))).strip(),
            str(server.get("status", "")).strip(),
        ])
        existing_ips.add(ip)
        added += 1

    if not values:
        return 0

    start_row = header_idx + 2 + len(existing)
    end_row = start_row + len(values) - 1
    _api_put(
        f"/sheets/v2/spreadsheets/{spreadsheet_token}/values",
        {
            "valueRange": {
                "range": f"{sheet_id}!A{start_row}:E{end_row}",
                "values": values,
            }
        },
    )
    print(f"  ✅ 已添加 {added} 台服务器到飞书表格")
    return added


def update_server_in_feishu(target_ip, **updates):
    """按 IP 更新飞书服务器表中的单台服务器。"""
    if not target_ip:
        return False

    token = _get_servers_token()
    if not token:
        return False

    spreadsheet_token = _resolve_token(token)
    sheet_id = _get_first_sheet_id(spreadsheet_token)
    rows = _read_sheet_values(spreadsheet_token, sheet_id)
    header_idx, headers, servers = _parse_server_sheet(rows)
    if header_idx is None:
        return False

    target_row = None
    target_server = None
    for i, server in enumerate(servers):
        if server.get("ip") == target_ip:
            target_row = header_idx + 2 + i
            target_server = server
            break
    if target_row is None:
        return False

    reverse_cols = {
        "number": "编号",
        "ip": "IP",
        "password": "密码",
        "label": "备注",
        "status": "状态",
    }

    changed = 0
    for key, value in updates.items():
        if key not in reverse_cols:
            continue
        try:
            col_idx = headers.index(key)
        except ValueError:
            continue
        if str(target_server.get(key, "")).strip() == str(value).strip():
            continue
        col_letter = _col_to_letter(col_idx)
        _api_put(
            f"/sheets/v2/spreadsheets/{spreadsheet_token}/values",
            {
                "valueRange": {
                    "range": f"{sheet_id}!{col_letter}{target_row}:{col_letter}{target_row}",
                    "values": [[str(value)]],
                }
            },
        )
        changed += 1
    return changed > 0


def clear_servers_in_feishu():
    """清空飞书服务器表中表头以下的数据行。"""
    token = _get_servers_token()
    if not token:
        return 0

    spreadsheet_token = _resolve_token(token)
    sheet_id = _get_first_sheet_id(spreadsheet_token)
    rows = _read_sheet_values(spreadsheet_token, sheet_id)
    header_idx, _, servers = _parse_server_sheet(rows)
    if header_idx is None or not servers:
        return 0

    start_row = header_idx + 2
    end_row = start_row + len(servers) - 1
    _api_put(
        f"/sheets/v2/spreadsheets/{spreadsheet_token}/values",
        {
            "valueRange": {
                "range": f"{sheet_id}!A{start_row}:E{end_row}",
                "values": [["", "", "", "", ""] for _ in range(len(servers))],
            }
        },
    )
    print(f"  🧹 已清空飞书服务器表中的 {len(servers)} 行")
    return len(servers)


def init_servers_to_feishu(servers=None):
    """重建飞书服务器表内容。"""
    if servers is None:
        try:
            from tools.aws_server import SERVERS
            servers = SERVERS
        except Exception:
            servers = []
    clear_servers_in_feishu()
    return add_servers_to_feishu(servers)


# ==================== 服务器编号检测 ====================

_server_number_cache = None


def _get_public_ip_from_services():
    """非 AWS 环境下通过公网服务获取出口 IP。"""
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            resp = _http.get(url, timeout=5)
            if resp.status_code == 200:
                ip = resp.text.strip()
                if ip:
                    return ip
        except Exception:
            pass
    return None


def _detect_server_number():
    """检测当前机器的服务器编号。

    优先匹配飞书服务器表中的编号；若飞书不可用，再回退到
    `tools.aws_server` 中的 SERVERS 列表。支持 IMDSv1 和 IMDSv2。
    """
    global _server_number_cache
    if _server_number_cache is not None:
        return _server_number_cache

    servers = read_servers()
    if not servers:
        try:
            from tools.aws_server import load_servers
            servers = load_servers(quiet=True)
        except Exception:
            servers = []

    public_ip = None

    # IMDSv2: 先获取 token 再请求
    try:
        token_resp = _http.put(
            "http://169.254.169.254/latest/api/token",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
            timeout=2,
        )
        if token_resp.status_code == 200:
            imds_token = token_resp.text.strip()
            resp = _http.get(
                "http://169.254.169.254/latest/meta-data/public-ipv4",
                headers={"X-aws-ec2-metadata-token": imds_token},
                timeout=2,
            )
            if resp.status_code == 200:
                public_ip = resp.text.strip()
    except Exception:
        pass

    # IMDSv1 fallback
    if not public_ip:
        try:
            resp = _http.get(
                "http://169.254.169.254/latest/meta-data/public-ipv4",
                timeout=2,
            )
            if resp.status_code == 200:
                public_ip = resp.text.strip()
        except Exception:
            pass

    if public_ip:
        for i, s in enumerate(servers, 1):
            if s.get("ip") != public_ip:
                continue
            number = s.get("number")
            if str(number).isdigit():
                _server_number_cache = int(number)
            else:
                _server_number_cache = i
            return _server_number_cache
        print(f"  ⚠️ 检测到公网 IP {public_ip}，但未匹配到服务器列表中的任何服务器")

    # 非 AWS 本机：公网出口 IP 可能被多台机器共享，统一记为 0。
    if not public_ip and _get_public_ip_from_services():
        _server_number_cache = 0
        return _server_number_cache

    # 本地 IP fallback
    try:
        import socket
        hostname = socket.gethostname()
        local_ip = socket.gethostbyname(hostname)
        for i, s in enumerate(servers, 1):
            if s.get("ip") != local_ip:
                continue
            number = s.get("number")
            if str(number).isdigit():
                _server_number_cache = int(number)
            else:
                _server_number_cache = i
            return _server_number_cache
    except Exception:
        pass

    return None


def _col_to_letter(col_idx):
    """列索引(0-based)转字母: 0→A, 1→B, 25→Z, 26→AA"""
    result = ""
    while True:
        result = chr(ord("A") + col_idx % 26) + result
        col_idx = col_idx // 26 - 1
        if col_idx < 0:
            break
    return result


def _find_header(rows, *target_cols):
    """在表格中找到表头行，并返回目标列的索引

    Args:
        rows: 二维列表
        *target_cols: 需要查找的列名（如 "昵称", "服务器"）

    Returns:
        tuple: (header_row_idx, nickname_col_idx, {col_name: col_idx})
               找不到返回 (None, None, {})
    """
    for i, row in enumerate(rows):
        if not row:
            continue
        if str(row[0] if row[0] is not None else "").strip() == "账号ID":
            nickname_col = None
            cols = {}
            for j, cell in enumerate(row):
                cell_str = str(cell if cell is not None else "").strip()
                if cell_str == "昵称":
                    nickname_col = j
                if cell_str in target_cols:
                    cols[cell_str] = j
            return i, nickname_col, cols
    return None, None, {}


def _get_row_nickname(row, nickname_col_idx):
    """从行数据中提取昵称"""
    if nickname_col_idx is None or nickname_col_idx >= len(row):
        return ""
    return str(row[nickname_col_idx] if row[nickname_col_idx] is not None else "").strip()


def write_account_columns(config_name, account_id, updates):
    """将指定账号的列值回写到飞书表格。

    Args:
        config_name: 配置名/飞书表名（如 "bts"）
        account_id: 账号 ID（如 "14"）
        updates: dict，{列名: 值}
    """
    if not config_name or not account_id or not updates:
        return

    normalized_updates = {}
    for col_name, value in updates.items():
        if not col_name:
            continue
        text = str(value).strip()
        if not text:
            continue
        normalized_updates[str(col_name).strip()] = text
    if not normalized_updates:
        return

    sheets, _ = load_mapping()
    if not sheets:
        return

    token = None
    for k, v in sheets.items():
        if k.lower() == config_name.lower():
            token = v
            break
    if not token:
        return

    try:
        spreadsheet_token = _resolve_token(token)
        sheet_id = _get_first_sheet_id(spreadsheet_token)
        rows = _read_sheet_values(spreadsheet_token, sheet_id)
    except Exception as e:
        print(f"  ⚠️ 回写账号列失败（读取表格）: {e}")
        return

    if not rows:
        return

    header_row_idx, _, cols = _find_header(rows, *normalized_updates.keys())
    if header_row_idx is None:
        return

    target_row_num = None
    target_row = None
    account_id = str(account_id).strip()
    for i, row in enumerate(rows[header_row_idx + 1:]):
        if not row:
            continue
        row_account_id = str(row[0] if row[0] is not None else "").strip()
        if row_account_id != account_id:
            continue
        target_row_num = header_row_idx + 1 + i + 1
        target_row = row
        break

    if target_row_num is None or target_row is None:
        return

    written_cols = []
    for col_name, value in normalized_updates.items():
        col_idx = cols.get(col_name)
        if col_idx is None:
            continue
        current_val = str(
            target_row[col_idx] if col_idx < len(target_row) and target_row[col_idx] is not None else ""
        ).strip()
        if current_val == value:
            continue
        col_letter = _col_to_letter(col_idx)
        try:
            _api_put(
                f"/sheets/v2/spreadsheets/{spreadsheet_token}/values",
                {
                    "valueRange": {
                        "range": f"{sheet_id}!{col_letter}{target_row_num}:{col_letter}{target_row_num}",
                        "values": [[value]],
                    }
                },
            )
            written_cols.append(col_name)
        except Exception as e:
            print(f"  ⚠️ 回写账号列 [{account_id}:{col_name}] 失败: {e}")

    if written_cols:
        print(f"  📝 已回写账号 {account_id} 的列: {', '.join(written_cols)}")


def write_account_image_column(config_name, account_id, col_name, image_path):
    """将指定账号某一列回写为飞书表格内嵌图片。"""
    if not config_name or not account_id or not col_name or not image_path:
        return
    if not os.path.exists(image_path):
        raise FileNotFoundError(f"截图文件不存在: {image_path}")

    sheets, _ = load_mapping()
    if not sheets:
        return

    token = None
    for k, v in sheets.items():
        if k.lower() == str(config_name).lower():
            token = v
            break
    if not token:
        return

    try:
        spreadsheet_token = _resolve_token(token)
        sheet_id = _get_first_sheet_id(spreadsheet_token)
        rows = _read_sheet_values(spreadsheet_token, sheet_id)
    except Exception as e:
        print(f"  ⚠️ 回写账号图片列失败（读取表格）: {e}")
        return

    if not rows:
        return

    header_row_idx, _, cols = _find_header(rows, str(col_name).strip())
    if header_row_idx is None:
        return

    col_idx = cols.get(str(col_name).strip())
    if col_idx is None:
        return

    target_row_num = None
    account_id = str(account_id).strip()
    for i, row in enumerate(rows[header_row_idx + 1:]):
        if not row:
            continue
        row_account_id = str(row[0] if row[0] is not None else "").strip()
        if row_account_id != account_id:
            continue
        target_row_num = header_row_idx + 1 + i + 1
        break

    if target_row_num is None:
        return

    col_letter = _col_to_letter(col_idx)
    range_str = f"{sheet_id}!{col_letter}{target_row_num}:{col_letter}{target_row_num}"

    with open(image_path, "rb") as f:
        image_bytes = list(f.read())

    _api_post(
        f"/sheets/v2/spreadsheets/{spreadsheet_token}/values_image",
        {
            "range": range_str,
            "name": os.path.basename(image_path),
            "image": image_bytes,
        },
        timeout=120,
    )
    print(f"  🖼️ 已回写账号 {account_id} 的图片列: {col_name}")


def _acc_id_to_nickname_map(config_name):
    """从本地配置构建 account_id → nickname 映射"""
    config_path = os.path.join(CONFIG_DIR, f"{config_name}.json")
    if not os.path.exists(config_path):
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return {}
    result = {}
    for acc_id in raw:
        if acc_id.startswith("_"):
            continue
        acc = raw[acc_id]
        if isinstance(acc, dict):
            result[acc_id] = acc.get("nickname", "")
    return result


def _write_server_column(token_or_wiki, running_accounts_fn, config_name=None):
    """回写服务器编号到飞书表格的'服务器'列（按昵称匹配行）

    Args:
        token_or_wiki: 飞书表格 token
        running_accounts_fn: 返回当前配置运行中的账号 ID 集合的回调
        config_name: 配置名，用于 account_id → nickname 转换
    """
    server_num = _detect_server_number()
    server_str = str(server_num if server_num is not None else 0)

    running_ids = running_accounts_fn() if running_accounts_fn else set()
    if not running_ids:
        return

    # 将 running account_ids 转为 nicknames
    id_nick = _acc_id_to_nickname_map(config_name) if config_name else {}
    running_nicks = {id_nick.get(aid, "") for aid in running_ids}
    running_nicks.discard("")

    if not running_nicks:
        return

    spreadsheet_token = _resolve_token(token_or_wiki)
    sheet_id = _get_first_sheet_id(spreadsheet_token)
    rows = _read_sheet_values(spreadsheet_token, sheet_id)

    if not rows:
        return

    header_row_idx, nickname_col, cols = _find_header(rows, "服务器")
    server_col_idx = cols.get("服务器")

    if header_row_idx is None or nickname_col is None or server_col_idx is None:
        return

    col_letter = _col_to_letter(server_col_idx)
    updates = []

    for i, row in enumerate(rows[header_row_idx + 1:]):
        if not row:
            continue
        nick = _get_row_nickname(row, nickname_col)
        if not nick:
            continue

        actual_row = header_row_idx + 1 + i + 1
        current_val = str(row[server_col_idx] if server_col_idx < len(row) and row[server_col_idx] is not None else "").strip()

        if nick in running_nicks:
            if current_val != server_str:
                updates.append((actual_row, server_str, nick))
        else:
            if server_str != "0" and current_val == server_str:
                updates.append((actual_row, "", nick))

    if not updates:
        return

    written = 0
    for row_num, val, acc_id in updates:
        try:
            _api_put(
                f"/sheets/v2/spreadsheets/{spreadsheet_token}/values",
                {
                    "valueRange": {
                        "range": f"{sheet_id}!{col_letter}{row_num}:{col_letter}{row_num}",
                        "values": [[val]],
                    }
                },
            )
            written += 1
        except Exception as e:
            print(f"  ⚠️ 回写服务器列 [{acc_id}] 失败: {e}")

    if written:
        action = "更新" if updates[0][1] else "清除"
        print(f"  📝 服务器{server_str}: {action}了 {written} 个账号的服务器列")


def write_server_ids(config_name, account_ids):
    """立即将指定账号的服务器编号回写到飞书表格。

    用于 run_all 在收到启动命令后，先占位写入服务器列，
    其余 watcher 轮询逻辑保持不变。
    """
    if not account_ids:
        return

    sheets, _ = load_mapping()
    if not sheets:
        return

    token = None
    for k, v in sheets.items():
        if k.lower() == config_name.lower():
            token = v
            break

    if not token:
        return

    target_ids = set(account_ids)
    _write_server_column(token, lambda: target_ids, config_name=config_name)


def get_free_accounts(config_name, count):
    """查询飞书表格中"服务器"列为空的前 count 个账号 ID（从上往下）

    Args:
        config_name: 配置名（如 "bts"）
        count: 需要的账号数量

    Returns:
        list: 空闲账号 ID 列表，如 ["1", "3", "7"]
    """
    sheets, _ = load_mapping()
    if not sheets or config_name.lower() not in {k.lower() for k in sheets}:
        return []

    token = None
    for k, v in sheets.items():
        if k.lower() == config_name.lower():
            token = v
            break

    if not token:
        return []

    try:
        spreadsheet_token = _resolve_token(token)
        sheet_id = _get_first_sheet_id(spreadsheet_token)
        rows = _read_sheet_values(spreadsheet_token, sheet_id)
    except Exception:
        return []

    if not rows:
        return []

    header_row_idx = None
    server_col_idx = None
    for i, row in enumerate(rows):
        if not row:
            continue
        if str(row[0] if row[0] is not None else "").strip() == "账号ID":
            header_row_idx = i
            for j, cell in enumerate(row):
                if str(cell if cell is not None else "").strip() == "服务器":
                    server_col_idx = j
            break

    if header_row_idx is None or server_col_idx is None:
        return []

    free = []
    for row in rows[header_row_idx + 1:]:
        if not row:
            continue
        acc_id = str(row[0] if row[0] is not None else "").strip()
        if not acc_id or acc_id.startswith("_"):
            continue
        server_val = str(row[server_col_idx] if server_col_idx < len(row) and row[server_col_idx] is not None else "").strip()
        if not server_val:
            free.append(acc_id)
            if len(free) >= count:
                break

    return free


def write_profile_ids(config_name, account_profiles):
    """将 profile_id 回写到飞书表格的"浏览器Profile ID"列

    Args:
        config_name: 配置名（如 "bts"）
        account_profiles: dict，{account_id: profile_id}，如 {"1": "abc123", "3": "def456"}
    """
    if not account_profiles:
        return

    # 将 account_id → nickname 以便按昵称匹配飞书表格行
    id_nick = _acc_id_to_nickname_map(config_name)
    nick_profiles = {}
    for aid, pid in account_profiles.items():
        nick = id_nick.get(aid, "")
        if nick:
            nick_profiles[nick] = pid

    if not nick_profiles:
        return

    sheets, _ = load_mapping()
    if not sheets:
        return

    token = None
    for k, v in sheets.items():
        if k.lower() == config_name.lower():
            token = v
            break

    if not token:
        return

    try:
        spreadsheet_token = _resolve_token(token)
        sheet_id = _get_first_sheet_id(spreadsheet_token)
        rows = _read_sheet_values(spreadsheet_token, sheet_id)
    except Exception as e:
        print(f"  ⚠️ 回写 Profile ID 失败（读取表格）: {e}")
        return

    if not rows:
        return

    header_row_idx, nickname_col, cols = _find_header(rows, "浏览器Profile ID")
    profile_col_idx = cols.get("浏览器Profile ID")

    if header_row_idx is None or nickname_col is None or profile_col_idx is None:
        return

    col_letter = _col_to_letter(profile_col_idx)
    updates = []

    for i, row in enumerate(rows[header_row_idx + 1:]):
        if not row:
            continue
        nick = _get_row_nickname(row, nickname_col)
        if not nick or nick not in nick_profiles:
            continue

        actual_row = header_row_idx + 1 + i + 1
        current_val = str(row[profile_col_idx] if profile_col_idx < len(row) and row[profile_col_idx] is not None else "").strip()
        new_val = nick_profiles[nick]

        if current_val != new_val:
            updates.append((actual_row, new_val, nick))

    if not updates:
        return

    written = 0
    for row_num, val, acc_id in updates:
        try:
            _api_put(
                f"/sheets/v2/spreadsheets/{spreadsheet_token}/values",
                {
                    "valueRange": {
                        "range": f"{sheet_id}!{col_letter}{row_num}:{col_letter}{row_num}",
                        "values": [[val]],
                    }
                },
            )
            written += 1
        except Exception as e:
            print(f"  ⚠️ 回写 [{acc_id}] Profile ID 失败: {e}")

    if written:
        print(f"  📝 已回写 {written} 个 Profile ID 到飞书表格")


# ==================== 日志回写 ====================

_STATUS_DIR = os.path.join(BASE_DIR, "status")
_LOG_WRITE_INTERVAL = 180
_last_log_write_at = {}


def _read_account_logs(config_name):
    """读取指定配置下所有账号的最新状态日志（按昵称索引）

    通过扫描 status/ 目录下的 JSON 文件，匹配 config 中的账号。

    Returns:
        dict: {nickname: "HH:MM:SS 步骤 - 详情"}
    """
    if not os.path.isdir(_STATUS_DIR):
        return {}

    config_path = os.path.join(CONFIG_DIR, f"{config_name}.json")
    if not os.path.exists(config_path):
        return {}

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return {}

    # 构建 (account_id, nickname_in_filename) → nickname 映射
    acc_nick_map = {}
    for acc_id in raw:
        if acc_id.startswith("_"):
            continue
        acc = raw[acc_id]
        if not isinstance(acc, dict):
            continue
        nickname = acc.get("nickname", "")
        nick_in_file = nickname.replace(" ", "_")
        acc_nick_map[(acc_id, nick_in_file)] = nickname

    logs = {}
    for fname in os.listdir(_STATUS_DIR):
        if not fname.startswith("status_") or not fname.endswith(".json"):
            continue

        # status_{account_id}_{nickname}.json
        base = fname[len("status_"):-len(".json")]
        sep = base.find("_")
        if sep < 0:
            continue
        file_acc_id = base[:sep]
        file_nick = base[sep + 1:]

        nickname = acc_nick_map.get((file_acc_id, file_nick))
        if nickname is None:
            continue

        try:
            with open(os.path.join(_STATUS_DIR, fname), "r", encoding="utf-8") as f:
                status = json.load(f)
        except Exception:
            continue

        time_str = status.get("time_str", "")
        step = status.get("step", "")
        detail = status.get("detail", "")
        state = status.get("state", "")

        if state == "closed":
            log_text = f"{time_str} 已关闭"
        elif state == "error":
            log_text = f"{time_str} 异常: {detail}" if detail else f"{time_str} 异常"
        elif state == "resting":
            log_text = f"{time_str} 休息中"
        else:
            log_text = f"{time_str} {step}"
            if detail:
                log_text += f" - {detail}"

        logs[nickname] = log_text

    return logs


def _should_write_logs_now(config_name):
    """日志列 3 分钟节流，避免频繁回写。"""
    now = time.time()
    key = str(config_name).lower()
    last_at = _last_log_write_at.get(key, 0)
    if now - last_at < _LOG_WRITE_INTERVAL:
        return False
    _last_log_write_at[key] = now
    return True


def _write_log_column(token_or_wiki, config_name, running_accounts_fn=None):
    """将运行中账号的最新日志回写到飞书表格的"日志"列（按昵称匹配行）

    只有当前有进程在运行的账号才会回写日志。
    """
    running_ids = running_accounts_fn() if running_accounts_fn else set()
    if not running_ids:
        return

    logs = _read_account_logs(config_name)
    if not logs:
        return

    # 只保留运行中账号的日志
    id_nick = _acc_id_to_nickname_map(config_name)
    running_nicks = {id_nick.get(aid, "") for aid in running_ids}
    running_nicks.discard("")
    logs = {nick: log for nick, log in logs.items() if nick in running_nicks}
    if not logs:
        return

    try:
        spreadsheet_token = _resolve_token(token_or_wiki)
        sheet_id = _get_first_sheet_id(spreadsheet_token)
        rows = _read_sheet_values(spreadsheet_token, sheet_id)
    except Exception:
        return

    if not rows:
        return

    header_row_idx, nickname_col, cols = _find_header(rows, "日志")
    log_col_idx = cols.get("日志")

    if header_row_idx is None or nickname_col is None or log_col_idx is None:
        return

    col_letter = _col_to_letter(log_col_idx)
    updates = []

    for i, row in enumerate(rows[header_row_idx + 1:]):
        if not row:
            continue
        nick = _get_row_nickname(row, nickname_col)
        if not nick or nick not in logs:
            continue

        actual_row = header_row_idx + 1 + i + 1
        current_val = str(row[log_col_idx] if log_col_idx < len(row) and row[log_col_idx] is not None else "").strip()
        new_val = logs[nick]

        if current_val != new_val:
            updates.append((actual_row, new_val))

    if not updates:
        return

    for row_num, val in updates:
        try:
            _api_put(
                f"/sheets/v2/spreadsheets/{spreadsheet_token}/values",
                {
                    "valueRange": {
                        "range": f"{sheet_id}!{col_letter}{row_num}:{col_letter}{row_num}",
                        "values": [[val]],
                    }
                },
            )
        except Exception:
            pass


def _write_runtime_columns(token_or_wiki, config_name, running_accounts_fn=None):
    """一次读表内同时回写服务器列和日志列。"""
    running_ids = running_accounts_fn() if running_accounts_fn else set()
    if not running_ids:
        return False

    id_nick = _acc_id_to_nickname_map(config_name)
    running_nicks = {id_nick.get(aid, "") for aid in running_ids}
    running_nicks.discard("")
    if not running_nicks:
        return False

    allow_log_write = _should_write_logs_now(config_name)
    logs = {}
    if allow_log_write:
        logs = _read_account_logs(config_name)
        logs = {nick: log for nick, log in logs.items() if nick in running_nicks}
    server_num = _detect_server_number()
    server_str = str(server_num if server_num is not None else 0)

    spreadsheet_token = _resolve_token(token_or_wiki)
    sheet_id = _get_first_sheet_id(spreadsheet_token)
    rows = _read_sheet_values(spreadsheet_token, sheet_id)
    if not rows:
        return False

    header_row_idx, nickname_col, cols = _find_header(rows, "服务器", "日志")
    server_col_idx = cols.get("服务器")
    log_col_idx = cols.get("日志")
    if header_row_idx is None or nickname_col is None:
        return False

    server_updates = []
    log_updates = []

    for i, row in enumerate(rows[header_row_idx + 1:]):
        if not row:
            continue
        nick = _get_row_nickname(row, nickname_col)
        if not nick:
            continue

        actual_row = header_row_idx + 1 + i + 1

        if server_col_idx is not None:
            current_server = str(
                row[server_col_idx] if server_col_idx < len(row) and row[server_col_idx] is not None else ""
            ).strip()
            if nick in running_nicks:
                if current_server != server_str:
                    server_updates.append((actual_row, server_str, nick))
            else:
                if server_str != "0" and current_server == server_str:
                    server_updates.append((actual_row, "", nick))

        if allow_log_write and log_col_idx is not None and nick in logs:
            current_log = str(
                row[log_col_idx] if log_col_idx < len(row) and row[log_col_idx] is not None else ""
            ).strip()
            new_log = logs[nick]
            if current_log != new_log:
                log_updates.append((actual_row, new_log, nick))

    written_server = 0
    written_log = 0

    if server_col_idx is not None and server_updates:
        col_letter = _col_to_letter(server_col_idx)
        for row_num, val, acc_id in server_updates:
            try:
                _api_put(
                    f"/sheets/v2/spreadsheets/{spreadsheet_token}/values",
                    {
                        "valueRange": {
                            "range": f"{sheet_id}!{col_letter}{row_num}:{col_letter}{row_num}",
                            "values": [[val]],
                        }
                    },
                )
                written_server += 1
            except Exception as e:
                print(f"  ⚠️ 回写服务器列 [{acc_id}] 失败: {e}")

    if allow_log_write and log_col_idx is not None and log_updates:
        col_letter = _col_to_letter(log_col_idx)
        for row_num, val, acc_id in log_updates:
            try:
                _api_put(
                    f"/sheets/v2/spreadsheets/{spreadsheet_token}/values",
                    {
                        "valueRange": {
                            "range": f"{sheet_id}!{col_letter}{row_num}:{col_letter}{row_num}",
                            "values": [[val]],
                        }
                    },
                )
                written_log += 1
            except Exception as e:
                print(f"  ⚠️ 回写日志列 [{acc_id}] 失败: {e}")

    if written_server:
        print(f"  📝 服务器{server_str}: 更新了 {written_server} 个账号的服务器列")
    if written_log:
        print(f"  📝 [{config_name}] 已回写 {written_log} 条日志到飞书表格")
    return bool(written_server or written_log)


def clear_account_columns(config_name, account_ids):
    """清空指定账号在飞书表格中的"服务器"和"日志"列

    Args:
        config_name: 配置名（如 "bts"）
        account_ids: 要清空的账号 ID 列表
    """
    if not account_ids:
        return

    sheets, _ = load_mapping()
    if not sheets:
        return

    token = None
    for k, v in sheets.items():
        if k.lower() == config_name.lower():
            token = v
            break
    if not token:
        return

    id_nick = _acc_id_to_nickname_map(config_name)
    target_nicks = {id_nick.get(aid, "") for aid in account_ids}
    target_nicks.discard("")
    if not target_nicks:
        return

    try:
        spreadsheet_token = _resolve_token(token)
        sheet_id = _get_first_sheet_id(spreadsheet_token)
        rows = _read_sheet_values(spreadsheet_token, sheet_id)
    except Exception as e:
        print(f"  ⚠️ 清空飞书列失败（读取表格）: {e}")
        return

    if not rows:
        return

    header_row_idx, nickname_col, cols = _find_header(rows, "服务器", "日志")
    server_col_idx = cols.get("服务器")
    log_col_idx = cols.get("日志")

    if header_row_idx is None or nickname_col is None:
        return

    cleared = 0
    for i, row in enumerate(rows[header_row_idx + 1:]):
        if not row:
            continue
        nick = _get_row_nickname(row, nickname_col)
        if not nick or nick not in target_nicks:
            continue

        actual_row = header_row_idx + 1 + i + 1

        for col_idx in (server_col_idx, log_col_idx):
            if col_idx is None:
                continue
            current_val = str(row[col_idx] if col_idx < len(row) and row[col_idx] is not None else "").strip()
            if not current_val:
                continue
            col_letter = _col_to_letter(col_idx)
            try:
                _api_put(
                    f"/sheets/v2/spreadsheets/{spreadsheet_token}/values",
                    {
                        "valueRange": {
                            "range": f"{sheet_id}!{col_letter}{actual_row}:{col_letter}{actual_row}",
                            "values": [[""]],
                        }
                    },
                )
                cleared += 1
            except Exception:
                pass

    if cleared:
        print(f"  🧹 已清空 {len(target_nicks)} 个账号的服务器/日志列")


def clear_account_log_columns(config_name, account_ids):
    """只清空指定账号在飞书表格中的"日志"列，保留服务器列。"""
    if not account_ids:
        return

    sheets, _ = load_mapping()
    if not sheets:
        return

    token = None
    for k, v in sheets.items():
        if k.lower() == config_name.lower():
            token = v
            break
    if not token:
        return

    id_nick = _acc_id_to_nickname_map(config_name)
    target_nicks = {id_nick.get(aid, "") for aid in account_ids}
    target_nicks.discard("")
    if not target_nicks:
        return

    try:
        spreadsheet_token = _resolve_token(token)
        sheet_id = _get_first_sheet_id(spreadsheet_token)
        rows = _read_sheet_values(spreadsheet_token, sheet_id)
    except Exception as e:
        print(f"  ⚠️ 清空飞书日志列失败（读取表格）: {e}")
        return

    if not rows:
        return

    header_row_idx, nickname_col, cols = _find_header(rows, "日志")
    log_col_idx = cols.get("日志")
    if header_row_idx is None or nickname_col is None or log_col_idx is None:
        return

    col_letter = _col_to_letter(log_col_idx)
    cleared = 0
    for i, row in enumerate(rows[header_row_idx + 1:]):
        if not row:
            continue
        nick = _get_row_nickname(row, nickname_col)
        if not nick or nick not in target_nicks:
            continue

        current_val = str(row[log_col_idx] if log_col_idx < len(row) and row[log_col_idx] is not None else "").strip()
        if not current_val:
            continue

        actual_row = header_row_idx + 1 + i + 1
        try:
            _api_put(
                f"/sheets/v2/spreadsheets/{spreadsheet_token}/values",
                {
                    "valueRange": {
                        "range": f"{sheet_id}!{col_letter}{actual_row}:{col_letter}{actual_row}",
                        "values": [[""]],
                    }
                },
            )
            cleared += 1
        except Exception as e:
            print(f"  ⚠️ 清空日志列 [{nick}] 失败: {e}")

    if cleared:
        print(f"  🧹 已清空 {cleared} 个账号的日志列")


# ==================== 同步逻辑 ====================

_last_snapshot = {}


def _diff_configs(old, new):
    """比较两个 config dict，返回 (added, modified, removed) 账号 ID 列表"""
    old_ids = {k for k in old if not k.startswith("_")}
    new_ids = {k for k in new if not k.startswith("_")}

    added = sorted(new_ids - old_ids)
    removed = sorted(old_ids - new_ids)
    modified = []
    for aid in sorted(old_ids & new_ids):
        if old.get(aid) != new.get(aid):
            modified.append(aid)

    if old.get("_defaults") != new.get("_defaults"):
        modified.insert(0, "_defaults")

    return added, modified, removed


def sync_one(name, spreadsheet_token, running_accounts_fn=None):
    """同步单个飞书表格到 config/{name}.json，并回写服务器编号

    Args:
        running_accounts_fn: 回调函数，返回当前配置下运行中的账号 ID 集合

    Returns:
        bool: True 表示有变更并已写入，False 表示无变更
    """
    try:
        _write_runtime_columns(spreadsheet_token, name, running_accounts_fn=running_accounts_fn)
    except Exception as e:
        print(f"  ⚠️ [{name}] 回写服务器/日志异常: {e}")

    feishu_data = feishu_sheet_to_dict(spreadsheet_token)
    if feishu_data is None:
        print(f"  ⚠️ [{name}] 飞书表格读取失败或为空")
        return False

    json_path = os.path.join(CONFIG_DIR, f"{name}.json")

    local_data = {}
    if os.path.exists(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                local_data = json.load(f)
        except Exception:
            pass

    new_json = json.dumps(feishu_data, ensure_ascii=False, indent=4, sort_keys=True)
    old_json = json.dumps(local_data, ensure_ascii=False, indent=4, sort_keys=True) if local_data else ""

    cache_key = f"{name}:{spreadsheet_token}"
    if new_json == _last_snapshot.get(cache_key, old_json):
        return False

    _last_snapshot[cache_key] = new_json

    if new_json == old_json:
        return False

    added, modified, removed = _diff_configs(local_data, feishu_data)

    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as f:
        f.write(new_json)

    account_count = len([k for k in feishu_data if not k.startswith("_")])
    print(f"  ✅ [{name}] 已同步 ({account_count} 个账号)")
    if added:
        print(f"      新增: {', '.join(added)}")
    if modified:
        print(f"      修改: {', '.join(modified)}")
    if removed:
        print(f"      删除: {', '.join(removed)}")

    return True


# ==================== 映射配置 ====================

def load_mapping():
    """加载 config/feishu_sheets.json 映射配置

    Returns:
        tuple: (sheets_dict, poll_interval) 或 (None, None)
    """
    if not os.path.exists(MAPPING_FILE):
        return None, None

    try:
        with open(MAPPING_FILE, "r", encoding="utf-8") as f:
            mapping = json.load(f)
    except Exception as e:
        print(f"  ⚠️ 读取飞书映射配置失败: {e}")
        return None, None

    sheets = mapping.get("sheets", {})
    interval = mapping.get("poll_interval", 60)

    if not sheets:
        print("  ⚠️ feishu_sheets.json 中没有配置任何表格")
        return None, None

    return sheets, interval


# ==================== 轮询 ====================

_watcher_status = {
    "running": False,
    "last_sync": None,
    "last_error": None,
    "sync_count": 0,
}


_running_tasks_ref = None
_pending_accounts = {}
_pending_accounts_lock = threading.Lock()


def set_running_tasks_ref(ref):
    """设置 run_all.py 的 running_tasks 引用，用于回写服务器列"""
    global _running_tasks_ref
    _running_tasks_ref = ref


def add_pending_accounts(config_name, account_ids):
    """登记预启动成功但尚未进入 main.py 的账号。"""
    if not config_name or not account_ids:
        return
    key = config_name.lower()
    with _pending_accounts_lock:
        bucket = _pending_accounts.setdefault(key, set())
        bucket.update(account_ids)


def remove_pending_accounts(config_name, account_ids):
    """移除预启动占位账号。"""
    if not config_name or not account_ids:
        return
    key = config_name.lower()
    with _pending_accounts_lock:
        bucket = _pending_accounts.get(key)
        if not bucket:
            return
        bucket.difference_update(account_ids)
        if not bucket:
            _pending_accounts.pop(key, None)


def _get_running_accounts_for(config_name):
    """获取指定配置下运行中的账号 ID 集合"""
    result = set()

    if _running_tasks_ref is not None:
        for (cfg, acc), info in list(_running_tasks_ref.items()):
            if cfg.lower() == config_name.lower():
                if info["process"].poll() is None:
                    result.add(acc)

    with _pending_accounts_lock:
        result.update(_pending_accounts.get(config_name.lower(), set()))
    return result


def sync_all(sheets=None):
    """同步所有已配置的飞书表格，返回变更的配置数"""
    if sheets is None:
        sheets, _ = load_mapping()
        if not sheets:
            return 0

    changed = 0
    for name, token in sheets.items():
        try:
            fn = (lambda n: lambda: _get_running_accounts_for(n))(name)
            if sync_one(name, token, running_accounts_fn=fn):
                changed += 1
        except Exception as e:
            print(f"  ❌ [{name}] 同步异常: {e}")
            _watcher_status["last_error"] = f"[{name}] {e}"

    _watcher_status["last_sync"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _watcher_status["sync_count"] += 1
    return changed


def push_runtime_state_all(sheets=None):
    """仅回写服务器/日志状态，不同步飞书表格到本地配置。"""
    if sheets is None:
        sheets, _ = load_mapping()
        if not sheets:
            return 0

    changed = 0
    for name, token in sheets.items():
        try:
            fn = (lambda n: lambda: _get_running_accounts_for(n))(name)
            if _write_runtime_columns(token, name, running_accounts_fn=fn):
                changed += 1
        except Exception as e:
            print(f"  ❌ [{name}] 回写运行状态异常: {e}")
            _watcher_status["last_error"] = f"[{name}] {e}"

    _watcher_status["last_sync"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _watcher_status["sync_count"] += 1
    return changed


def watch_loop(sheets=None, interval=60):
    """后台轮询循环，仅回写服务器/日志状态。"""
    if sheets is None:
        sheets, interval = load_mapping()
        if not sheets:
            return

    _watcher_status["running"] = True
    names = ", ".join(sheets.keys())
    print(f"  📡 飞书状态回写已启动 [{names}]，轮询间隔 {interval}s")

    while _watcher_status["running"]:
        try:
            push_runtime_state_all(sheets)
        except Exception as e:
            _watcher_status["last_error"] = str(e)
        time.sleep(interval)


def start_watcher_thread():
    """启动后台 watcher 守护线程，仅负责回写运行状态。

    Returns:
        threading.Thread or None: 启动的线程，或 None（无配置时）
    """
    sheets, interval = load_mapping()
    if not sheets:
        return None

    t = threading.Thread(
        target=watch_loop,
        args=(sheets, interval),
        daemon=True,
        name="feishu-sheet-watcher",
    )
    t.start()
    return t


def stop_watcher():
    _watcher_status["running"] = False


def get_watcher_status():
    return dict(_watcher_status)


# ==================== 主入口 ====================

def main():
    import argparse

    parser = argparse.ArgumentParser(description="飞书电子表格 → config JSON 同步")
    parser.add_argument("--watch", action="store_true", help="持续监听模式")
    parser.add_argument("--interval", type=int, default=None, help="轮询间隔（秒）")
    args = parser.parse_args()

    sheets, cfg_interval = load_mapping()
    if not sheets:
        print("❌ 请先配置 config/feishu_sheets.json")
        print("   格式示例:")
        print('   {')
        print('     "poll_interval": 60,')
        print('     "sheets": {')
        print('       "skz": "你的飞书表格token",')
        print('       "bts": "你的飞书表格token"')
        print('     }')
        print('   }')
        return

    interval = args.interval or cfg_interval or 60

    print("=" * 50)
    print("  飞书表格 → 配置同步")
    print("=" * 50)
    print()

    if args.watch:
        print(f"  持续监听模式，轮询间隔 {interval}s")
        print(f"  按 Ctrl+C 停止")
        print()
        try:
            watch_loop(sheets, interval)
        except KeyboardInterrupt:
            print("\n  ⚠️ 已停止监听")
    else:
        print("  执行单次同步...")
        changed = sync_all(sheets)
        if changed:
            print(f"\n  🎉 同步完成，{changed} 个配置已更新")
        else:
            print(f"\n  ✅ 所有配置已是最新，无变更")


if __name__ == "__main__":
    main()
