"""
自定义域名邮箱服务实现（MoeMail 原生 API）
"""

import logging
import re
import time
from typing import Optional, Dict, Any, List

from .base import BaseEmailService, EmailServiceError, EmailServiceType
from ..core.http_client import HTTPClient, RequestConfig
from ..config.constants import OTP_CODE_PATTERN, CUSTOM_DOMAIN_API_ENDPOINTS


logger = logging.getLogger(__name__)


class CustomDomainEmailService(BaseEmailService):
    """
    自定义域名邮箱服务
    使用原生接口：
    - GET  /api/config
    - POST /api/emails/generate
    - GET  /api/emails/{emailId}
    """

    def __init__(self, config: Dict[str, Any] = None, name: str = None):
        super().__init__(EmailServiceType.CUSTOM_DOMAIN, name)

        self.config = self._normalize_config(config or {})
        http_config = RequestConfig(
            timeout=self.config["timeout"],
            max_retries=self.config["max_retries"],
        )
        self.http_client = HTTPClient(
            proxy_url=self.config.get("proxy_url"),
            config=http_config,
        )

        self._emails_cache: Dict[str, Dict[str, Any]] = {}
        self._cached_config: Optional[Dict[str, Any]] = None
        self._last_config_check: float = 0

    @staticmethod
    def _normalize_config(config: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(config or {})

        if "base_url" not in normalized and normalized.get("api_url"):
            normalized["base_url"] = normalized.get("api_url")

        # 过滤 None，避免请求头编码错误
        normalized = {k: v for k, v in normalized.items() if v is not None}

        base_url = str(normalized.get("base_url", "")).strip()
        api_key = str(normalized.get("api_key", "")).strip()
        if not base_url or not api_key:
            raise ValueError("custom_domain 配置不完整：base_url 与 api_key 为必填")

        default_domain = str(normalized.get("default_domain", "")).strip()

        return {
            "base_url": base_url.rstrip("/"),
            "api_key": api_key,
            "api_key_header": str(normalized.get("api_key_header", "X-API-Key")).strip() or "X-API-Key",
            "timeout": int(normalized.get("timeout", 30)),
            "max_retries": int(normalized.get("max_retries", 3)),
            "proxy_url": normalized.get("proxy_url"),
            "default_domain": default_domain or None,
            "default_expiry": int(normalized.get("default_expiry", 3600000)),
        }

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            self.config["api_key_header"]: self.config["api_key"],
        }
        return {str(k): str(v) for k, v in headers.items() if k is not None and v is not None}

    def _make_request(self, method: str, endpoint: str, **kwargs) -> Dict[str, Any]:
        url = f"{self.config['base_url']}{endpoint}"
        kwargs.setdefault("headers", {})
        kwargs["headers"] = {
            **self._headers(),
            **{str(k): str(v) for k, v in kwargs["headers"].items() if k is not None and v is not None},
        }

        try:
            response = self.http_client.request(method, url, **kwargs)

            if response.status_code >= 400:
                err = f"API 请求失败: {response.status_code}"
                try:
                    err = f"{err} - {response.json()}"
                except Exception:
                    err = f"{err} - {response.text[:200]}"
                raise EmailServiceError(err)

            try:
                return response.json()
            except Exception:
                return {"raw_response": response.text}

        except Exception as e:
            self.update_status(False, e)
            if isinstance(e, EmailServiceError):
                raise
            raise EmailServiceError(f"API 请求失败: {method} {endpoint} - {e}")

    def get_config(self, force_refresh: bool = False) -> Dict[str, Any]:
        if (
            not force_refresh
            and self._cached_config
            and time.time() - self._last_config_check < 300
        ):
            return self._cached_config

        try:
            endpoint = CUSTOM_DOMAIN_API_ENDPOINTS["get_config"]
            data = self._make_request("GET", endpoint)
            self._cached_config = data
            self._last_config_check = time.time()
            self.update_status(True)
            return data
        except Exception as e:
            logger.warning(f"获取配置失败: {e}")
            self.update_status(False, e)
            return {}

    def create_email(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        sys_config = self.get_config()
        request_config = dict(config or {})

        default_domain = self.config.get("default_domain")
        if not default_domain:
            domains = str(sys_config.get("emailDomains", "")).strip()
            if domains:
                default_domain = domains.split(",")[0].strip()

        payload = {
            "name": request_config.get("name", ""),
            "expiryTime": request_config.get("expiryTime", self.config.get("default_expiry", 3600000)),
            "domain": request_config.get("domain") or request_config.get("default_domain") or default_domain,
        }
        payload = {k: v for k, v in payload.items() if v not in (None, "")}

        try:
            endpoint = CUSTOM_DOMAIN_API_ENDPOINTS["create_email"]
            data = self._make_request("POST", endpoint, json=payload)

            email = str(data.get("email", "")).strip()
            email_id = str(data.get("id", "")).strip() or email
            if not email or not email_id:
                raise EmailServiceError(f"API 返回数据不完整: {data}")

            info = {
                "email": email,
                "service_id": email_id,
                "id": email_id,
                "created_at": time.time(),
                "domain": payload.get("domain"),
                "expiry": payload.get("expiryTime"),
                "raw_response": data,
            }
            self._emails_cache[email_id] = info

            logger.info(f"成功创建自定义域名邮箱: {email}")
            self.update_status(True)
            return info
        except Exception as e:
            self.update_status(False, e)
            if isinstance(e, EmailServiceError):
                raise
            raise EmailServiceError(f"创建邮箱失败: {e}")

    def _extract_verification_code(
        self,
        email: str,
        subject: str,
        body_text: str,
        pattern: str,
    ) -> Optional[str]:
        blocked_codes = set(re.findall(r"(?<!\d)(\d{6})(?!\d)", email or ""))
        email_pattern = r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"

        subject_clean = re.sub(email_pattern, " ", subject or "")
        body_clean = re.sub(email_pattern, " ", body_text or "")
        body_clean = re.sub(r"<[^>]+>", " ", body_clean)

        candidates: List[str] = []
        m_subject_semantic = re.search(
            r"(?:verification\s*code|code\s*is|your\s*code|chatgpt\s*code|验证码)\D{0,20}(\d{6})",
            subject_clean,
            flags=re.IGNORECASE,
        )
        if m_subject_semantic:
            candidates.append(m_subject_semantic.group(1))

        candidates.extend(re.findall(pattern, subject_clean))

        m_body_semantic = re.search(
            r"(?:verification\s*code|code\s*is|your\s*code|chatgpt\s*code|验证码)\D{0,40}(\d{6})",
            body_clean,
            flags=re.IGNORECASE,
        )
        if m_body_semantic:
            candidates.append(m_body_semantic.group(1))

        for code in candidates:
            if code and code not in blocked_codes:
                return code
        return None

    def _get_message_content(self, email_id: str, message_id: str) -> Dict[str, str]:
        try:
            endpoint = CUSTOM_DOMAIN_API_ENDPOINTS["get_message"].format(emailId=email_id, messageId=message_id)
            data = self._make_request("GET", endpoint)
            msg = data.get("message") if isinstance(data, dict) else None
            if not isinstance(msg, dict):
                msg = data if isinstance(data, dict) else {}

            sender = str(msg.get("from_address") or msg.get("from") or msg.get("sender") or "")
            subject = str(msg.get("subject") or "")
            body = str(msg.get("content") or msg.get("text") or msg.get("html") or msg.get("raw") or "")
            return {"sender": sender, "subject": subject, "body": body}
        except Exception as e:
            logger.debug(f"获取邮件内容失败: {email_id}/{message_id} - {e}")
            return {"sender": "", "subject": "", "body": ""}

    def get_verification_code(
        self,
        email: str,
        email_id: str = None,
        timeout: int = 120,
        pattern: str = OTP_CODE_PATTERN,
        otp_sent_at: Optional[float] = None,
    ) -> Optional[str]:
        target_email_id = str(email_id or "").strip()
        if not target_email_id:
            for eid, info in self._emails_cache.items():
                if info.get("email") == email:
                    target_email_id = eid
                    break

        if not target_email_id:
            logger.warning(f"未找到邮箱 {email} 的 service_id，无法获取验证码")
            return None

        logger.info(f"正在从 custom_domain 邮箱 {email} 获取验证码...")
        start_time = time.time()
        seen_ids: set = set()

        while time.time() - start_time < timeout:
            try:
                endpoint = CUSTOM_DOMAIN_API_ENDPOINTS["get_email_messages"].format(emailId=target_email_id)
                data = self._make_request("GET", endpoint)
                messages = data.get("messages", []) if isinstance(data, dict) else []
                if not isinstance(messages, list):
                    time.sleep(3)
                    continue

                for msg in messages:
                    msg_id = str(msg.get("id", "")).strip()
                    if not msg_id or msg_id in seen_ids:
                        continue
                    seen_ids.add(msg_id)

                    sender = str(msg.get("from_address") or msg.get("from") or msg.get("sender") or "").lower()
                    subject = str(msg.get("subject") or "").strip()
                    body = str(msg.get("content") or msg.get("text") or msg.get("html") or "")

                    if not body:
                        detail = self._get_message_content(target_email_id, msg_id)
                        sender = (detail.get("sender") or sender).lower()
                        subject = detail.get("subject") or subject
                        body = detail.get("body") or body

                    if "openai" not in sender and "openai" not in subject.lower() and "openai" not in body.lower():
                        continue

                    code = self._extract_verification_code(
                        email=email,
                        subject=subject,
                        body_text=body,
                        pattern=pattern,
                    )
                    if code:
                        logger.info(f"从 custom_domain 邮箱 {email} 找到验证码: {code}")
                        self.update_status(True)
                        return code

            except Exception as e:
                logger.debug(f"检查 custom_domain 邮件时出错: {e}")

            time.sleep(3)

        logger.warning(f"等待 custom_domain 验证码超时: {email}")
        return None

    def list_emails(self, cursor: str = None, **kwargs) -> List[Dict[str, Any]]:
        params = {}
        if cursor:
            params["cursor"] = cursor

        try:
            endpoint = CUSTOM_DOMAIN_API_ENDPOINTS["list_emails"]
            data = self._make_request("GET", endpoint, params=params)
            emails = data.get("emails") if isinstance(data, dict) else None
            if not isinstance(emails, list):
                emails = data.get("results", []) if isinstance(data, dict) else []
            if not isinstance(emails, list):
                emails = []

            for item in emails:
                email_id = str(item.get("id", "")).strip()
                if email_id:
                    self._emails_cache[email_id] = item

            self.update_status(True)
            return emails
        except Exception as e:
            logger.warning(f"列出邮箱失败: {e}")
            self.update_status(False, e)
            return []

    def delete_email(self, email_id: str) -> bool:
        try:
            endpoint = CUSTOM_DOMAIN_API_ENDPOINTS["delete_email"].format(emailId=email_id)
            data = self._make_request("DELETE", endpoint)
            success = bool(data.get("success", False))
            if success:
                self._emails_cache.pop(email_id, None)
            self.update_status(success)
            return success
        except Exception as e:
            logger.error(f"删除邮箱失败: {email_id} - {e}")
            self.update_status(False, e)
            return False

    def check_health(self) -> bool:
        try:
            cfg = self.get_config(force_refresh=True)
            ok = bool(cfg)
            self.update_status(ok)
            return ok
        except Exception as e:
            logger.warning(f"自定义域名邮箱服务健康检查失败: {e}")
            self.update_status(False, e)
            return False

    def get_email_messages(self, email_id: str, cursor: str = None) -> List[Dict[str, Any]]:
        params = {}
        if cursor:
            params["cursor"] = cursor

        try:
            endpoint = CUSTOM_DOMAIN_API_ENDPOINTS["get_email_messages"].format(emailId=email_id)
            data = self._make_request("GET", endpoint, params=params)
            messages = data.get("messages", []) if isinstance(data, dict) else []
            return messages if isinstance(messages, list) else []
        except Exception as e:
            logger.error(f"获取邮件列表失败: {email_id} - {e}")
            return []

    def get_message_detail(self, email_id: str, message_id: str) -> Optional[Dict[str, Any]]:
        try:
            endpoint = CUSTOM_DOMAIN_API_ENDPOINTS["get_message"].format(emailId=email_id, messageId=message_id)
            data = self._make_request("GET", endpoint)
            if isinstance(data, dict) and isinstance(data.get("message"), dict):
                return data["message"]
            return data if isinstance(data, dict) else None
        except Exception as e:
            logger.error(f"获取邮件详情失败: {email_id}/{message_id} - {e}")
            return None

    def get_service_info(self) -> Dict[str, Any]:
        return {
            "service_type": self.service_type.value,
            "name": self.name,
            "base_url": self.config.get("base_url"),
            "default_domain": self.config.get("default_domain"),
            "cached_emails_count": len(self._emails_cache),
            "status": self.status.value,
        }
