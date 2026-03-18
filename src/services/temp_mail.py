"""
Temp-Mail 邮箱服务实现
基于自部署 Cloudflare Worker 临时邮箱服务
接口文档参见 plan/temp-mail.md
"""

import re
import time
import json
import quopri
import logging
from typing import Optional, Dict, Any, List
from email import policy
from email.parser import Parser

from .base import BaseEmailService, EmailServiceError, EmailServiceType
from ..core.http_client import HTTPClient, RequestConfig
from ..config.constants import OTP_CODE_PATTERN


logger = logging.getLogger(__name__)


class TempMailService(BaseEmailService):
    """
    Temp-Mail 邮箱服务
    基于自部署 Cloudflare Worker 的临时邮箱，admin 模式管理邮箱
    不走代理，不使用 requests 库
    """

    def __init__(self, config: Dict[str, Any] = None, name: str = None):
        """
        初始化 TempMail 服务

        Args:
            config: 配置字典，支持以下键:
                - base_url: Worker 域名地址，如 https://mail.example.com (必需)
                - admin_password: Admin 密码，对应 x-admin-auth header (必需)
                - domain: 邮箱域名，如 example.com (必需)
                - enable_prefix: 是否启用前缀，默认 True
                - timeout: 请求超时时间，默认 30
                - max_retries: 最大重试次数，默认 3
            name: 服务名称
        """
        super().__init__(EmailServiceType.TEMP_MAIL, name)

        normalized_config = dict(config or {})
        # 兼容旧字段命名
        if "base_url" not in normalized_config and normalized_config.get("api_url"):
            normalized_config["base_url"] = normalized_config.get("api_url")
        if "admin_password" not in normalized_config and normalized_config.get("api_key"):
            normalized_config["admin_password"] = normalized_config.get("api_key")
        if "domain" not in normalized_config and normalized_config.get("default_domain"):
            normalized_config["domain"] = normalized_config.get("default_domain")

        required_keys = ["base_url", "admin_password", "domain"]
        missing_keys = [key for key in required_keys if not normalized_config.get(key)]
        if missing_keys:
            raise ValueError(f"缺少必需配置: {missing_keys}")

        default_config = {
            "enable_prefix": True,
            "timeout": 30,
            "max_retries": 3,
        }
        self.config = {**default_config, **normalized_config}

        # 不走代理，proxy_url=None
        http_config = RequestConfig(
            timeout=self.config["timeout"],
            max_retries=self.config["max_retries"],
        )
        self.http_client = HTTPClient(proxy_url=None, config=http_config)

        # 邮箱缓存：email -> {jwt, address}
        self._email_cache: Dict[str, Dict[str, Any]] = {}

    def _admin_headers(self) -> Dict[str, str]:
        """构造 admin 请求头"""
        headers = {
            "x-admin-auth": self.config.get("admin_password"),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        custom_auth = self.config.get("custom_auth")
        if custom_auth:
            headers["x-custom-auth"] = custom_auth

        # 过滤空键空值，避免底层请求编码报错
        return {str(k): str(v) for k, v in headers.items() if k is not None and v is not None}

    def _make_request(self, method: str, path: str, **kwargs) -> Any:
        """
        发送请求并返回 JSON 数据

        Args:
            method: HTTP 方法
            path: 请求路径（以 / 开头）
            **kwargs: 传递给 http_client.request 的额外参数

        Returns:
            响应 JSON 数据

        Raises:
            EmailServiceError: 请求失败
        """
        base_url = self.config["base_url"].rstrip("/")
        url = f"{base_url}{path}"

        # 合并默认 admin headers
        kwargs.setdefault("headers", {})
        for k, v in self._admin_headers().items():
            kwargs["headers"].setdefault(k, v)
        kwargs["headers"] = {
            str(k): str(v)
            for k, v in kwargs["headers"].items()
            if k is not None and v is not None
        }

        try:
            response = self.http_client.request(method, url, **kwargs)

            if response.status_code >= 400:
                error_msg = f"请求失败: {response.status_code}"
                try:
                    error_data = response.json()
                    error_msg = f"{error_msg} - {error_data}"
                except Exception:
                    error_msg = f"{error_msg} - {response.text[:200]}"
                self.update_status(False, EmailServiceError(error_msg))
                raise EmailServiceError(error_msg)

            try:
                return response.json()
            except json.JSONDecodeError:
                return {"raw_response": response.text}

        except Exception as e:
            self.update_status(False, e)
            if isinstance(e, EmailServiceError):
                raise
            raise EmailServiceError(f"请求失败: {method} {path} - {e}")

    def create_email(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        通过 admin API 创建临时邮箱

        Returns:
            包含邮箱信息的字典:
            - email: 邮箱地址
            - jwt: 用户级 JWT token
            - service_id: 同 email（用作标识）
        """
        import random
        import string

        request_config = config or {}

        # 生成随机邮箱名（除非调用方显式传入）
        letters = ''.join(random.choices(string.ascii_lowercase, k=5))
        digits = ''.join(random.choices(string.digits, k=random.randint(1, 3)))
        suffix = ''.join(random.choices(string.ascii_lowercase, k=random.randint(1, 3)))
        random_name = letters + digits + suffix

        name = str(request_config.get("name") or random_name)
        domain = (
            request_config.get("domain")
            or request_config.get("default_domain")
            or self.config["domain"]
        )
        enable_prefix = request_config.get("enable_prefix", self.config.get("enable_prefix", True))

        body = {
            "enablePrefix": enable_prefix,
            "name": name,
            "domain": domain,
        }

        try:
            response = self._make_request("POST", "/admin/new_address", json=body)

            address = response.get("address", "").strip()
            jwt = response.get("jwt", "").strip()

            if not address:
                raise EmailServiceError(f"API 返回数据不完整: {response}")

            email_info = {
                "email": address,
                "jwt": jwt,
                "service_id": address,
                "id": address,
                "created_at": time.time(),
            }

            # 缓存 jwt，供获取验证码时使用
            self._email_cache[address] = email_info

            logger.info(f"成功创建 TempMail 邮箱: {address}")
            self.update_status(True)
            return email_info

        except Exception as e:
            self.update_status(False, e)
            if isinstance(e, EmailServiceError):
                raise
            raise EmailServiceError(f"创建邮箱失败: {e}")

    def list_emails(self, **kwargs) -> List[Dict[str, Any]]:
        """
        列出当前缓存的邮箱

        Note:
            Temp-Mail admin API 无通用“地址列表”接口，这里返回本进程缓存。
        """
        return list(self._email_cache.values())

    def delete_email(self, email_id: str) -> bool:
        """
        删除邮箱（缓存层面）

        传入邮箱地址（address）或缓存中的 id 都可删除。
        """
        if not email_id:
            return False

        removed = False

        if email_id in self._email_cache:
            self._email_cache.pop(email_id, None)
            removed = True
        else:
            to_remove = [
                addr for addr, info in self._email_cache.items()
                if info.get("id") == email_id or info.get("service_id") == email_id
            ]
            for addr in to_remove:
                self._email_cache.pop(addr, None)
                removed = True

        return removed

    def get_verification_code(
        self,
        email: str,
        email_id: str = None,
        timeout: int = 120,
        pattern: str = OTP_CODE_PATTERN,
        otp_sent_at: Optional[float] = None,
    ) -> Optional[str]:
        """
        从 TempMail 邮箱获取验证码

        Args:
            email: 邮箱地址
            email_id: 未使用，保留接口兼容
            timeout: 超时时间（秒）
            pattern: 验证码正则
            otp_sent_at: OTP 发送时间戳（暂未使用）

        Returns:
            验证码字符串，超时返回 None
        """
        logger.info(f"正在从 TempMail 邮箱 {email} 获取验证码...")

        start_time = time.time()
        seen_mail_ids: set = set()
        blocked_codes = set(re.findall(r"(?<!\d)(\d{6})(?!\d)", email or ""))

        while time.time() - start_time < timeout:
            try:
                # 使用 admin API 查询邮件，通过 address 参数过滤
                response = self._make_request(
                    "GET",
                    "/admin/mails",
                    params={"limit": 20, "offset": 0, "address": email},
                )

                # admin/mails 返回格式: {"results": [...], "total": N}
                mails = response.get("results", [])
                if not isinstance(mails, list):
                    time.sleep(3)
                    continue

                for mail in mails:
                    mail_id = mail.get("id")
                    if not mail_id or mail_id in seen_mail_ids:
                        continue

                    seen_mail_ids.add(mail_id)

                    sender, subject, body_text = self._extract_mail_fields(mail)
                    sender = sender.lower()

                    # 只处理 OpenAI 邮件（按发件人/主题判断）
                    if "openai" not in sender and "openai" not in subject.lower():
                        continue

                    code = self._extract_verification_code(
                        subject=subject,
                        body_text=body_text,
                        pattern=pattern,
                        blocked_codes=blocked_codes,
                    )

                    if code:
                        logger.info(f"从 TempMail 邮箱 {email} 找到验证码: {code}")
                        self.update_status(True)
                        return code

            except Exception as e:
                logger.debug(f"检查 TempMail 邮件时出错: {e}")

            time.sleep(3)

        logger.warning(f"等待 TempMail 验证码超时: {email}")
        return None

    def _extract_mail_fields(self, mail: Dict[str, Any]) -> tuple[str, str, str]:
        """
        从 mail 对象中提取 sender/subject/body，必要时从 raw 兜底解析。
        """
        sender = str(mail.get("source", "") or "")
        subject = str(mail.get("subject", "") or "").strip()
        body_text = str(mail.get("text", "") or mail.get("html", "") or "")

        if subject or body_text:
            return sender, subject, body_text

        raw = str(mail.get("raw", "") or mail.get("raw_content", "") or mail.get("rawText", "") or "")
        if not raw:
            return sender, subject, body_text

        try:
            msg = Parser(policy=policy.default).parsestr(raw)
            parsed_sender = str(msg.get("From", "") or msg.get("Sender", "") or "")
            parsed_subject = str(msg.get("Subject", "") or "").strip()

            parts: List[str] = []
            if msg.is_multipart():
                for part in msg.walk():
                    ctype = str(part.get_content_type() or "").lower()
                    if ctype not in ("text/plain", "text/html"):
                        continue
                    payload = part.get_payload(decode=True)
                    if payload is None:
                        text = str(part.get_payload() or "")
                    else:
                        charset = part.get_content_charset() or "utf-8"
                        text = payload.decode(charset, errors="ignore")
                    parts.append(text)
            else:
                payload = msg.get_payload(decode=True)
                if payload is None:
                    text = str(msg.get_payload() or "")
                else:
                    charset = msg.get_content_charset() or "utf-8"
                    text = payload.decode(charset, errors="ignore")
                parts.append(text)

            parsed_body = "\n".join(p for p in parts if p).strip()
            if parsed_body:
                parsed_body = self._decode_quoted_printable_text(parsed_body)

            if not parsed_subject:
                m = re.search(r"(?im)^Subject:\s*(.+)$", raw)
                if m:
                    parsed_subject = m.group(1).strip()

            if not parsed_sender:
                m = re.search(r"(?im)^(?:From|Sender):\s*(.+)$", raw)
                if m:
                    parsed_sender = m.group(1).strip()

            if not parsed_body:
                body_split = re.split(r"\r?\n\r?\n", raw, maxsplit=1)
                if len(body_split) == 2:
                    parsed_body = self._decode_quoted_printable_text(body_split[1])

            return parsed_sender or sender, parsed_subject or subject, parsed_body or body_text

        except Exception:
            # parser 失败时，纯文本兜底
            m_subject = re.search(r"(?im)^Subject:\s*(.+)$", raw)
            m_sender = re.search(r"(?im)^(?:From|Sender):\s*(.+)$", raw)
            body_split = re.split(r"\r?\n\r?\n", raw, maxsplit=1)
            parsed_body = self._decode_quoted_printable_text(body_split[1]) if len(body_split) == 2 else ""

            return (
                (m_sender.group(1).strip() if m_sender else sender),
                (m_subject.group(1).strip() if m_subject else subject),
                parsed_body or body_text,
            )

    def _decode_quoted_printable_text(self, text: str) -> str:
        """解码 quoted-printable 文本（失败则返回原文）。"""
        if not text:
            return text
        try:
            return quopri.decodestring(text.encode("utf-8", errors="ignore")).decode("utf-8", errors="ignore")
        except Exception:
            return text

    def _extract_verification_code(
        self,
        subject: str,
        body_text: str,
        pattern: str,
        blocked_codes: set[str],
    ) -> Optional[str]:
        """
        提取验证码：
        1) Subject 语义匹配
        2) Subject 通用 6 位匹配
        3) Body 语义匹配
        并过滤邮箱地址中的 6 位数字。
        """
        email_pattern = r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"

        subject_clean = re.sub(email_pattern, " ", subject or "")
        body_clean = re.sub(email_pattern, " ", body_text or "")
        body_clean = re.sub(r"<[^>]+>", " ", body_clean)

        candidates: List[str] = []

        # Subject 语义匹配（优先）
        m_subject_semantic = re.search(
            r"(?:verification\s*code|code\s*is|your\s*code|chatgpt\s*code|验证码)\D{0,20}(\d{6})",
            subject_clean,
            flags=re.IGNORECASE,
        )
        if m_subject_semantic:
            candidates.append(m_subject_semantic.group(1))

        # Subject 通用匹配（次优）
        candidates.extend(re.findall(pattern, subject_clean))

        # Body 仅做语义匹配，降低噪声数字误判
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

    def check_health(self) -> bool:
        """检查服务健康状态"""
        try:
            self._make_request(
                "GET",
                "/admin/mails",
                params={"limit": 1, "offset": 0},
            )
            self.update_status(True)
            return True
        except Exception as e:
            logger.warning(f"TempMail 健康检查失败: {e}")
            self.update_status(False, e)
            return False

    def get_service_info(self) -> Dict[str, Any]:
        """获取服务信息"""
        return {
            "service_type": self.service_type.value,
            "name": self.name,
            "base_url": self.config.get("base_url"),
            "domain": self.config.get("domain"),
            "enable_prefix": self.config.get("enable_prefix", True),
            "cached_emails_count": len(self._email_cache),
            "status": self.status.value,
        }
