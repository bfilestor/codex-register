"""
自定义域名邮箱服务实现

说明：
- 保持 service_type 为 custom_domain，兼容现有任务与数据库记录
- 内部使用 Temp-Mail Admin API（/admin/new_address, /admin/mails）
- 配置字段兼容：
  - api_url -> base_url
  - api_key -> admin_password
  - default_domain -> domain
"""

import logging
from typing import Optional, Dict, Any, List

from .base import BaseEmailService, EmailServiceError, EmailServiceType
from .temp_mail import TempMailService
from ..config.constants import OTP_CODE_PATTERN


logger = logging.getLogger(__name__)


class CustomDomainEmailService(BaseEmailService):
    """
    自定义域名邮箱服务（Temp-Mail Admin API 适配层）
    """

    def __init__(self, config: Dict[str, Any] = None, name: str = None):
        super().__init__(EmailServiceType.CUSTOM_DOMAIN, name)

        self.config = self._normalize_config(config or {})
        backend_name = f"{self.name}_backend"
        self._backend = TempMailService(self.config, backend_name)

    @staticmethod
    def _normalize_config(config: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(config or {})

        # 兼容旧字段命名
        if "base_url" not in normalized and normalized.get("api_url"):
            normalized["base_url"] = normalized.get("api_url")
        if "admin_password" not in normalized and normalized.get("api_key"):
            normalized["admin_password"] = normalized.get("api_key")
        if "domain" not in normalized and normalized.get("default_domain"):
            normalized["domain"] = normalized.get("default_domain")

        # 过滤 None，避免底层请求编码报错
        normalized = {k: v for k, v in normalized.items() if v is not None}

        required_keys = ["base_url", "admin_password", "domain"]
        missing_keys = [key for key in required_keys if not normalized.get(key)]
        if missing_keys:
            raise ValueError(
                f"custom_domain 配置不完整，缺少: {missing_keys}。"
                f"请填写 base_url、api_key(或 admin_password)、default_domain(或 domain)"
            )

        normalized.setdefault("enable_prefix", True)
        normalized.setdefault("timeout", 30)
        normalized.setdefault("max_retries", 3)
        return normalized

    def create_email(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        try:
            # 兼容调用方传入 default_domain
            request_config = dict(config or {})
            if "domain" not in request_config and request_config.get("default_domain"):
                request_config["domain"] = request_config.get("default_domain")
            result = self._backend.create_email(request_config)
            self.update_status(True)
            return result
        except Exception as e:
            self.update_status(False, e)
            if isinstance(e, EmailServiceError):
                raise
            raise EmailServiceError(f"创建邮箱失败: {e}")

    def get_verification_code(
        self,
        email: str,
        email_id: str = None,
        timeout: int = 120,
        pattern: str = OTP_CODE_PATTERN,
        otp_sent_at: Optional[float] = None,
    ) -> Optional[str]:
        try:
            code = self._backend.get_verification_code(
                email=email,
                email_id=email_id,
                timeout=timeout,
                pattern=pattern,
                otp_sent_at=otp_sent_at,
            )
            self.update_status(bool(code))
            return code
        except Exception as e:
            self.update_status(False, e)
            logger.debug(f"获取验证码失败: {e}")
            return None

    def list_emails(self, **kwargs) -> List[Dict[str, Any]]:
        return self._backend.list_emails(**kwargs)

    def delete_email(self, email_id: str) -> bool:
        try:
            success = self._backend.delete_email(email_id)
            self.update_status(success)
            return success
        except Exception as e:
            self.update_status(False, e)
            logger.error(f"删除邮箱失败: {email_id} - {e}")
            return False

    def check_health(self) -> bool:
        try:
            ok = self._backend.check_health()
            self.update_status(ok)
            return ok
        except Exception as e:
            self.update_status(False, e)
            logger.warning(f"自定义域名邮箱服务健康检查失败: {e}")
            return False

    def get_config(self, force_refresh: bool = False) -> Dict[str, Any]:
        return {
            "mode": "temp_mail_admin",
            "base_url": self.config.get("base_url"),
            "domain": self.config.get("domain"),
            "enable_prefix": self.config.get("enable_prefix", True),
        }

    def get_service_info(self) -> Dict[str, Any]:
        return {
            "service_type": self.service_type.value,
            "name": self.name,
            "base_url": self.config.get("base_url"),
            "domain": self.config.get("domain"),
            "enable_prefix": self.config.get("enable_prefix", True),
            "cached_emails_count": len(self._backend.list_emails()),
            "status": self.status.value,
        }

