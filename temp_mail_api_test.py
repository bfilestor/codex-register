#!/usr/bin/env python
"""
Temp-Mail admin API test script.

Reference:
https://temp-mail-docs.awsl.uk/zh/guide/feature/mail-api.html

Lifecycle flow:
1) POST   /admin/new_address
2) DELETE /admin/clear_inbox/{address_id}
3) DELETE /admin/delete_address/{address_id}

Fetch-only flow:
- GET /admin/mails?address=...
"""

from __future__ import annotations

import argparse
import base64
import json
import random
import string
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


def rand_name() -> str:
    letters = "".join(random.choices(string.ascii_lowercase, k=5))
    digits = "".join(random.choices(string.digits, k=random.randint(1, 3)))
    tail = "".join(random.choices(string.ascii_lowercase, k=random.randint(1, 2)))
    return f"{letters}{digits}{tail}"


def request_json(
    base_url: str,
    method: str,
    path: str,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    timeout: int = 30,
) -> Tuple[int, Any]:
    base = base_url.rstrip("/")
    url = f"{base}{path}"
    if params:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        if query:
            url = f"{url}?{query}"

    req_headers = {"Accept": "application/json"}
    if headers:
        req_headers.update({k: v for k, v in headers.items() if k and v is not None})

    body_bytes = None
    if json_body is not None:
        body_bytes = json.dumps(json_body).encode("utf-8")
        req_headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url=url, data=body_bytes, method=method.upper(), headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, {"raw_response": raw}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw_response": raw}
    except urllib.error.URLError as e:
        return 599, {"error": str(e)}


def default_headers(args: argparse.Namespace) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    if args.user_agent:
        headers["User-Agent"] = args.user_agent.strip()
    if args.cookie:
        headers["Cookie"] = args.cookie.strip()
    if args.origin:
        headers["Origin"] = args.origin.strip()
    if args.referer:
        headers["Referer"] = args.referer.strip()
    headers["x-admin-auth"] = args.admin_password
    if args.custom_auth:
        headers["x-custom-auth"] = args.custom_auth
    return headers


def print_cloudflare_hint(status_code: int, data: Any) -> None:
    if status_code != 403 or not isinstance(data, dict):
        return
    if str(data.get("error_code", "")).strip() != "1010":
        return
    print("[hint] Cloudflare 1010: 当前请求指纹被站点规则拦截。")
    print("[hint] 可尝试：")
    print("  1) Cloudflare WAF 放行 /admin/*")
    print("  2) 放行你的出口 IP")
    print("  3) 传入浏览器 Cookie：--cookie \"cf_clearance=...\"")
    print("  4) 自定义 --user-agent/--origin/--referer")


def decode_jwt_payload_no_verify(jwt_token: str) -> Dict[str, Any]:
    try:
        parts = jwt_token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        padding = "=" * ((4 - len(payload) % 4) % 4)
        raw = base64.urlsafe_b64decode((payload + padding).encode("ascii"))
        data = json.loads(raw.decode("utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def create_address(args: argparse.Namespace) -> Dict[str, Any]:
    name = args.name.strip() if args.name else rand_name()
    payload = {
        "name": name,
        "domain": args.domain.strip(),
        "enablePrefix": bool(args.enable_prefix),
    }

    code, data = request_json(
        base_url=args.base_url,
        method="POST",
        path="/admin/new_address",
        headers=default_headers(args),
        json_body=payload,
        timeout=args.timeout,
    )
    print(f"[create] status={code}, payload={json.dumps(payload, ensure_ascii=False)}")
    print(f"[create] response={json.dumps(data, ensure_ascii=False)}")
    print_cloudflare_hint(code, data)

    if code >= 400:
        raise RuntimeError("创建邮箱失败")

    address = str(data.get("address", "")).strip()
    jwt = str(data.get("jwt", "")).strip()
    if not address:
        raise RuntimeError("创建成功但返回中缺少 address")
    if not jwt:
        raise RuntimeError("创建成功但返回中缺少 jwt，无法解析 address_id")

    payload_data = decode_jwt_payload_no_verify(jwt)
    address_id = payload_data.get("address_id")
    if isinstance(address_id, str) and address_id.isdigit():
        address_id = int(address_id)
    if not isinstance(address_id, int):
        raise RuntimeError(f"无法从 jwt 解析 address_id。jwt_payload={json.dumps(payload_data, ensure_ascii=False)}")

    print(f"[create] address={address}")
    print(f"[create] address_id={address_id}")
    return {"address": address, "jwt": jwt, "address_id": address_id}


def clear_inbox(args: argparse.Namespace, address_id: int) -> None:
    path = f"/admin/clear_inbox/{address_id}"
    code, data = request_json(
        base_url=args.base_url,
        method="DELETE",
        path=path,
        headers=default_headers(args),
        timeout=args.timeout,
    )
    print(f"[clear_inbox] DELETE {path} -> status={code}, response={json.dumps(data, ensure_ascii=False)}")
    print_cloudflare_hint(code, data)
    if code >= 400:
        raise RuntimeError("清空邮箱失败")


def delete_address(args: argparse.Namespace, address_id: int) -> None:
    path = f"/admin/delete_address/{address_id}"
    code, data = request_json(
        base_url=args.base_url,
        method="DELETE",
        path=path,
        headers=default_headers(args),
        timeout=args.timeout,
    )
    print(f"[delete_address] DELETE {path} -> status={code}, response={json.dumps(data, ensure_ascii=False)}")
    print_cloudflare_hint(code, data)
    if code >= 400:
        raise RuntimeError("删除邮箱失败")


def fetch_mails_by_address(args: argparse.Namespace, address: str) -> None:
    params = {
        "limit": args.fetch_limit,
        "offset": args.fetch_offset,
        "address": address.strip(),
    }
    code, data = request_json(
        base_url=args.base_url,
        method="GET",
        path="/admin/mails",
        headers=default_headers(args),
        params=params,
        timeout=args.timeout,
    )
    print(f"[fetch_mails] GET /admin/mails params={json.dumps(params, ensure_ascii=False)} -> status={code}")
    print(f"[fetch_mails] response={json.dumps(data, ensure_ascii=False)}")
    print_cloudflare_hint(code, data)
    if code >= 400:
        raise RuntimeError("拉取指定邮箱邮件失败")

    if isinstance(data, dict):
        results = data.get("results", [])
        total = data.get("total")
        if isinstance(results, list):
            print(f"[fetch_mails] matched={len(results)}, total={total}")
            for idx, item in enumerate(results[:10], start=1):
                mail_id = item.get("id")
                sender = item.get("source") or item.get("from") or item.get("from_address") or ""
                subject = item.get("subject") or ""
                created = item.get("created_at") or item.get("date") or ""
                print(f"  {idx}. id={mail_id} from={sender} subject={subject} time={created}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Temp-Mail lifecycle test or fetch-only test")
    p.add_argument("--base-url", required=True, help="Worker base url, e.g. https://mail.example.com")
    p.add_argument("--admin-password", required=True, help="x-admin-auth")
    p.add_argument("--domain", default="", help="address domain (required for lifecycle mode)")
    p.add_argument("--name", default="", help="address name; random if empty")
    p.add_argument("--enable-prefix", action="store_true", default=True, help="send enablePrefix=true (default true)")
    p.add_argument("--disable-prefix", action="store_true", help="send enablePrefix=false")
    p.add_argument("--custom-auth", default="", help="optional x-custom-auth")
    p.add_argument("--timeout", type=int, default=30, help="http timeout seconds")
    p.add_argument("--skip-clear", action="store_true", help="skip clear inbox step")
    p.add_argument("--skip-delete", action="store_true", help="skip delete address step")
    p.add_argument("--address-id", type=int, default=0, help="override address_id (if > 0, do not parse from jwt)")
    p.add_argument("--fetch-only", action="store_true", help="only fetch mails for --fetch-address")
    p.add_argument("--fetch-address", default="", help="target email address for /admin/mails filter")
    p.add_argument("--fetch-limit", type=int, default=20, help="fetch page size")
    p.add_argument("--fetch-offset", type=int, default=0, help="fetch page offset")
    p.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="request User-Agent")
    p.add_argument("--cookie", default="", help="optional Cookie, e.g. cf_clearance=...")
    p.add_argument("--origin", default="", help="optional Origin")
    p.add_argument("--referer", default="", help="optional Referer")
    return p


def main() -> int:
    args = build_parser().parse_args()
    if args.disable_prefix:
        args.enable_prefix = False

    if args.fetch_only:
        if not args.fetch_address.strip():
            raise RuntimeError("fetch-only 模式需要 --fetch-address")
        fetch_mails_by_address(args, address=args.fetch_address)
        print("[done] fetch-only finished")
        return 0

    if not args.domain.strip():
        raise RuntimeError("lifecycle 模式需要 --domain")

    created = create_address(args)
    address_id = args.address_id if args.address_id > 0 else created["address_id"]

    if not args.skip_clear:
        clear_inbox(args, address_id=address_id)
    else:
        print("[clear_inbox] skipped")

    if not args.skip_delete:
        delete_address(args, address_id=address_id)
    else:
        print("[delete_address] skipped")

    if args.fetch_address.strip():
        fetch_mails_by_address(args, address=args.fetch_address)

    print("[done] all requested steps finished")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
