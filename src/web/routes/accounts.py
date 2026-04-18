"""
账号管理 API 路由
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, BackgroundTasks, File, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ...database import crud
from ...database.session import get_db
from ...database.models import Account
from ...config.constants import AccountStatus, REGEX_PATTERNS
from ...config.settings import get_settings

logger = logging.getLogger(__name__)
router = APIRouter()


# ============== Pydantic Models ==============

class AccountResponse(BaseModel):
    """账号响应模型"""
    id: int
    email: str
    password: Optional[str] = None
    client_id: Optional[str] = None
    email_service: str
    account_id: Optional[str] = None
    workspace_id: Optional[str] = None
    registered_at: Optional[str] = None
    last_refresh: Optional[str] = None
    expires_at: Optional[str] = None
    status: str
    proxy_used: Optional[str] = None
    cpa_uploaded: bool = False
    cpa_uploaded_at: Optional[str] = None
    cookies: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    class Config:
        from_attributes = True


class AccountListResponse(BaseModel):
    """账号列表响应"""
    total: int
    accounts: List[AccountResponse]


class AccountUpdateRequest(BaseModel):
    """账号更新请求"""
    status: Optional[str] = None
    metadata: Optional[dict] = None
    cookies: Optional[str] = None  # 完整 cookie 字符串，用于支付请求


class ManualAccountCreateRequest(BaseModel):
    """手动新增账号请求"""
    email: str
    password: str


class BatchDeleteRequest(BaseModel):
    """批量删除请求"""
    ids: List[int] = []
    select_all: bool = False
    status_filter: Optional[str] = None
    email_service_filter: Optional[str] = None
    search_filter: Optional[str] = None


class BatchUpdateRequest(BaseModel):
    """批量更新请求"""
    ids: List[int]
    status: str


# ============== Helper Functions ==============

def resolve_account_ids(
    db,
    ids: List[int],
    select_all: bool = False,
    status_filter: Optional[str] = None,
    email_service_filter: Optional[str] = None,
    search_filter: Optional[str] = None,
) -> List[int]:
    """当 select_all=True 时查询全部符合条件的 ID，否则直接返回传入的 ids"""
    if not select_all:
        return ids
    query = db.query(Account.id)
    if status_filter:
        query = query.filter(Account.status == status_filter)
    if email_service_filter:
        query = query.filter(Account.email_service == email_service_filter)
    if search_filter:
        pattern = f"%{search_filter}%"
        query = query.filter(
            (Account.email.ilike(pattern)) | (Account.account_id.ilike(pattern))
        )
    return [row[0] for row in query.all()]


def account_to_response(account: Account) -> AccountResponse:
    """转换 Account 模型为响应模型"""
    return AccountResponse(
        id=account.id,
        email=account.email,
        password=account.password,
        client_id=account.client_id,
        email_service=account.email_service,
        account_id=account.account_id,
        workspace_id=account.workspace_id,
        registered_at=account.registered_at.isoformat() if account.registered_at else None,
        last_refresh=account.last_refresh.isoformat() if account.last_refresh else None,
        expires_at=account.expires_at.isoformat() if account.expires_at else None,
        status=account.status,
        proxy_used=account.proxy_used,
        cpa_uploaded=account.cpa_uploaded or False,
        cpa_uploaded_at=account.cpa_uploaded_at.isoformat() if account.cpa_uploaded_at else None,
        cookies=account.cookies,
        created_at=account.created_at.isoformat() if account.created_at else None,
        updated_at=account.updated_at.isoformat() if account.updated_at else None,
    )


# ============== API Endpoints ==============

@router.get("", response_model=AccountListResponse)
async def list_accounts(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    status: Optional[str] = Query(None, description="状态筛选"),
    email_service: Optional[str] = Query(None, description="邮箱服务筛选"),
    search: Optional[str] = Query(None, description="搜索关键词"),
):
    """
    获取账号列表

    支持分页、状态筛选、邮箱服务筛选和搜索
    """
    with get_db() as db:
        # 构建查询
        query = db.query(Account)

        # 状态筛选
        if status:
            query = query.filter(Account.status == status)

        # 邮箱服务筛选
        if email_service:
            query = query.filter(Account.email_service == email_service)

        # 搜索
        if search:
            search_pattern = f"%{search}%"
            query = query.filter(
                (Account.email.ilike(search_pattern)) |
                (Account.account_id.ilike(search_pattern))
            )

        # 统计总数
        total = query.count()

        # 分页
        offset = (page - 1) * page_size
        accounts = query.order_by(Account.created_at.desc()).offset(offset).limit(page_size).all()

        return AccountListResponse(
            total=total,
            accounts=[account_to_response(acc) for acc in accounts]
        )


@router.get("/{account_id}", response_model=AccountResponse)
async def get_account(account_id: int):
    """获取单个账号详情"""
    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")
        return account_to_response(account)


@router.post("/manual", response_model=AccountResponse)
async def create_manual_account(request: ManualAccountCreateRequest):
    """手动新增账号（邮箱 + 密码）"""
    email = (request.email or "").strip().lower()
    password = (request.password or "").strip()

    if not email:
        raise HTTPException(status_code=400, detail="邮箱不能为空")
    if len(email) > 255 or not re.match(REGEX_PATTERNS["EMAIL"], email):
        raise HTTPException(status_code=400, detail="邮箱格式无效")
    if not password:
        raise HTTPException(status_code=400, detail="密码不能为空")
    if len(password) > 255:
        raise HTTPException(status_code=400, detail="密码长度不能超过 255")

    with get_db() as db:
        existing = crud.get_account_by_email(db, email)
        if existing:
            raise HTTPException(status_code=409, detail=f"邮箱已存在: {email}")

        account = crud.create_account(
            db,
            email=email,
            password=password,
            email_service="manual",
            status=AccountStatus.ACTIVE.value,
            source="manual",
        )
        return account_to_response(account)


@router.get("/{account_id}/tokens")
async def get_account_tokens(account_id: int):
    """获取账号的 Token 信息"""
    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")

        return {
            "id": account.id,
            "email": account.email,
            "access_token": account.access_token,
            "refresh_token": account.refresh_token,
            "id_token": account.id_token,
            "has_tokens": bool(account.access_token and account.refresh_token),
        }


@router.patch("/{account_id}", response_model=AccountResponse)
async def update_account(account_id: int, request: AccountUpdateRequest):
    """更新账号状态"""
    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")

        update_data = {}
        if request.status:
            if request.status not in [e.value for e in AccountStatus]:
                raise HTTPException(status_code=400, detail="无效的状态值")
            update_data["status"] = request.status

        if request.metadata:
            current_metadata = account.metadata or {}
            current_metadata.update(request.metadata)
            update_data["metadata"] = current_metadata

        if request.cookies is not None:
            # 留空则清空，非空则更新
            update_data["cookies"] = request.cookies or None

        account = crud.update_account(db, account_id, **update_data)
        return account_to_response(account)


@router.get("/{account_id}/cookies")
async def get_account_cookies(account_id: int):
    """获取账号的 cookie 字符串（仅供支付使用）"""
    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")
        return {"account_id": account_id, "cookies": account.cookies or ""}


@router.delete("/{account_id}")
async def delete_account(account_id: int):
    """删除单个账号"""
    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")

        crud.delete_account(db, account_id)
        return {"success": True, "message": f"账号 {account.email} 已删除"}


@router.post("/batch-delete")
async def batch_delete_accounts(request: BatchDeleteRequest):
    """批量删除账号"""
    with get_db() as db:
        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )
        deleted_count = 0
        errors = []

        for account_id in ids:
            try:
                account = crud.get_account_by_id(db, account_id)
                if account:
                    crud.delete_account(db, account_id)
                    deleted_count += 1
            except Exception as e:
                errors.append(f"ID {account_id}: {str(e)}")

        return {
            "success": True,
            "deleted_count": deleted_count,
            "errors": errors if errors else None
        }


@router.post("/batch-update")
async def batch_update_accounts(request: BatchUpdateRequest):
    """批量更新账号状态"""
    if request.status not in [e.value for e in AccountStatus]:
        raise HTTPException(status_code=400, detail="无效的状态值")

    with get_db() as db:
        updated_count = 0
        errors = []

        for account_id in request.ids:
            try:
                account = crud.get_account_by_id(db, account_id)
                if account:
                    crud.update_account(db, account_id, status=request.status)
                    updated_count += 1
            except Exception as e:
                errors.append(f"ID {account_id}: {str(e)}")

        return {
            "success": True,
            "updated_count": updated_count,
            "errors": errors if errors else None
        }


class BatchExportRequest(BaseModel):
    """批量导出请求"""
    ids: List[int] = []
    select_all: bool = False
    status_filter: Optional[str] = None
    email_service_filter: Optional[str] = None
    search_filter: Optional[str] = None


def _to_optional_string(
    value: Any,
    *,
    field_name: str,
    max_length: Optional[int] = None,
    strip_value: bool = True,
) -> Optional[str]:
    """将任意值转换为可选字符串，并校验长度"""
    if value is None:
        return None

    if isinstance(value, str):
        text = value.strip() if strip_value else value
    else:
        text = str(value).strip() if strip_value else str(value)

    if text == "":
        return None

    if max_length is not None and len(text) > max_length:
        raise ValueError(f"{field_name} 长度不能超过 {max_length}")

    return text


def _parse_optional_datetime(value: Any, *, field_name: str) -> Optional[datetime]:
    """解析可选时间字段，支持 ISO8601 字符串或时间戳"""
    if value in (None, ""):
        return None

    parsed: Optional[datetime] = None

    if isinstance(value, (int, float)):
        parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError(f"{field_name} 不是有效时间格式: {value}") from exc
    else:
        raise ValueError(f"{field_name} 类型无效: {type(value).__name__}")

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)

    return parsed


def _build_import_fields(
    record: Dict[str, Any],
    *,
    status_values: set,
    warnings: List[str],
    row_no: int,
) -> Dict[str, Any]:
    """将导入记录转换为 Account 可写字段"""
    email = _to_optional_string(record.get("email"), field_name="email", max_length=255)
    if not email:
        raise ValueError("email 不能为空")
    email = email.lower()
    if not re.match(REGEX_PATTERNS["EMAIL"], email):
        raise ValueError(f"email 格式无效: {email}")

    email_service = _to_optional_string(
        record.get("email_service"),
        field_name="email_service",
        max_length=50
    ) or "manual"

    status = (_to_optional_string(
        record.get("status"),
        field_name="status",
        max_length=20
    ) or AccountStatus.ACTIVE.value).lower()
    if status not in status_values:
        warnings.append(f"第 {row_no} 条记录状态值无效（{status}），已自动改为 active")
        status = AccountStatus.ACTIVE.value

    source = _to_optional_string(record.get("source"), field_name="source", max_length=20) or "import"

    registered_at = _parse_optional_datetime(record.get("registered_at"), field_name="registered_at")

    return {
        "email": email,
        "password": _to_optional_string(record.get("password"), field_name="password", max_length=255),
        "client_id": _to_optional_string(record.get("client_id"), field_name="client_id", max_length=255),
        "session_token": _to_optional_string(
            record.get("session_token"),
            field_name="session_token",
            strip_value=False
        ),
        "email_service": email_service,
        "email_service_id": _to_optional_string(record.get("email_service_id"), field_name="email_service_id", max_length=255),
        "account_id": _to_optional_string(record.get("account_id"), field_name="account_id", max_length=255),
        "workspace_id": _to_optional_string(record.get("workspace_id"), field_name="workspace_id", max_length=255),
        "access_token": _to_optional_string(record.get("access_token"), field_name="access_token", strip_value=False),
        "refresh_token": _to_optional_string(record.get("refresh_token"), field_name="refresh_token", strip_value=False),
        "id_token": _to_optional_string(record.get("id_token"), field_name="id_token", strip_value=False),
        "proxy_used": _to_optional_string(record.get("proxy_used"), field_name="proxy_used", max_length=255),
        "registered_at": registered_at or datetime.utcnow(),
        "last_refresh": _parse_optional_datetime(record.get("last_refresh"), field_name="last_refresh"),
        "expires_at": _parse_optional_datetime(record.get("expires_at"), field_name="expires_at"),
        "status": status,
        "source": source,
        "cookies": _to_optional_string(record.get("cookies"), field_name="cookies", strip_value=False),
    }


@router.post("/import/json")
async def import_accounts_json(file: UploadFile = File(...)):
    """从导出的 JSON 文件导入账号（仅创建，不更新已存在邮箱）"""
    filename = file.filename or ""
    if not filename:
        raise HTTPException(status_code=400, detail="请选择要导入的 JSON 文件")
    if not filename.lower().endswith(".json"):
        raise HTTPException(status_code=400, detail="仅支持导入 .json 文件")

    raw_content = await file.read()
    if not raw_content:
        raise HTTPException(status_code=400, detail="导入文件为空")

    try:
        payload = json.loads(raw_content.decode("utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="文件编码无效，请使用 UTF-8 JSON") from exc
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"JSON 解析失败: {exc.msg}") from exc

    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict) and isinstance(payload.get("accounts"), list):
        records = payload.get("accounts", [])
    else:
        raise HTTPException(status_code=400, detail="JSON 结构无效，应为数组或包含 accounts 数组的对象")

    total_count = len(records)
    if total_count == 0:
        return {
            "success": True,
            "total": 0,
            "created_count": 0,
            "updated_count": 0,
            "skipped_count": 0,
            "skipped_existing_count": 0,
            "skipped_existing": None,
            "failed_count": 0,
            "warnings": None,
            "errors": None
        }

    status_values = {e.value for e in AccountStatus}
    created_count = 0
    skipped_count = 0
    skipped_existing_count = 0
    skipped_existing: List[str] = []
    warnings: List[str] = []
    errors: List[str] = []
    seen_emails = set()

    with get_db() as db:
        for idx, item in enumerate(records, start=1):
            try:
                if not isinstance(item, dict):
                    raise ValueError("记录必须为 JSON 对象")

                fields = _build_import_fields(
                    item,
                    status_values=status_values,
                    warnings=warnings,
                    row_no=idx
                )
                email = fields["email"]

                if email in seen_emails:
                    raise ValueError(f"文件中存在重复邮箱: {email}")
                seen_emails.add(email)

                existing = crud.get_account_by_email(db, email)
                if existing:
                    skipped_count += 1
                    skipped_existing_count += 1
                    skipped_existing.append(f"第 {idx} 条: {email}")
                    continue

                account = Account(**fields)
                db.add(account)
                created_count += 1

                db.commit()
            except ValueError as exc:
                db.rollback()
                skipped_count += 1
                errors.append(f"第 {idx} 条: {exc}")
            except Exception as exc:
                db.rollback()
                skipped_count += 1
                logger.exception("导入账号异常（第 %s 条）", idx)
                errors.append(f"第 {idx} 条: {str(exc)}")

    return {
        "success": True,
        "total": total_count,
        "created_count": created_count,
        "updated_count": 0,
        "skipped_count": skipped_count,
        "skipped_existing_count": skipped_existing_count,
        "skipped_existing": skipped_existing or None,
        "failed_count": len(errors),
        "warnings": warnings or None,
        "errors": errors or None
    }


@router.post("/export/json")
async def export_accounts_json(request: BatchExportRequest):
    """导出账号为 JSON 格式"""
    with get_db() as db:
        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )
        accounts = db.query(Account).filter(Account.id.in_(ids)).all()

        export_data = []
        for acc in accounts:
            export_data.append({
                "email": acc.email,
                "password": acc.password,
                "client_id": acc.client_id,
                "account_id": acc.account_id,
                "workspace_id": acc.workspace_id,
                "access_token": acc.access_token,
                "refresh_token": acc.refresh_token,
                "id_token": acc.id_token,
                "session_token": acc.session_token,
                "email_service": acc.email_service,
                "registered_at": acc.registered_at.isoformat() if acc.registered_at else None,
                "last_refresh": acc.last_refresh.isoformat() if acc.last_refresh else None,
                "expires_at": acc.expires_at.isoformat() if acc.expires_at else None,
                "status": acc.status,
            })

        # 生成文件名
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"accounts_{timestamp}.json"

        # 返回 JSON 响应
        content = json.dumps(export_data, ensure_ascii=False, indent=2)

        return StreamingResponse(
            iter([content]),
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )


@router.post("/export/csv")
async def export_accounts_csv(request: BatchExportRequest):
    """导出账号为 CSV 格式"""
    import csv
    import io

    with get_db() as db:
        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )
        accounts = db.query(Account).filter(Account.id.in_(ids)).all()

        # 创建 CSV 内容
        output = io.StringIO()
        writer = csv.writer(output)

        # 写入表头
        writer.writerow([
            "ID", "Email", "Password", "Client ID",
            "Account ID", "Workspace ID",
            "Access Token", "Refresh Token", "ID Token", "Session Token",
            "Email Service", "Status", "Registered At", "Last Refresh", "Expires At"
        ])

        # 写入数据
        for acc in accounts:
            writer.writerow([
                acc.id,
                acc.email,
                acc.password or "",
                acc.client_id or "",
                acc.account_id or "",
                acc.workspace_id or "",
                acc.access_token or "",
                acc.refresh_token or "",
                acc.id_token or "",
                acc.session_token or "",
                acc.email_service,
                acc.status,
                acc.registered_at.isoformat() if acc.registered_at else "",
                acc.last_refresh.isoformat() if acc.last_refresh else "",
                acc.expires_at.isoformat() if acc.expires_at else ""
            ])

        # 生成文件名
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"accounts_{timestamp}.csv"

        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )


@router.post("/export/sub2api")
async def export_accounts_sub2api(request: BatchExportRequest):
    """导出账号为 Sub2Api 格式（所有选中账号合并到一个 JSON 的 accounts 数组中）"""

    def make_account_entry(acc) -> dict:
        expires_at = int(acc.expires_at.timestamp()) if acc.expires_at else 0
        return {
            "name": acc.email,
            "platform": "openai",
            "type": "oauth",
            "credentials": {
                "access_token": acc.access_token or "",
                "chatgpt_account_id": acc.account_id or "",
                "chatgpt_user_id": "",
                "client_id": acc.client_id or "",
                "expires_at": expires_at,
                "expires_in": 863999,
                "model_mapping": {
                    "gpt-5.1": "gpt-5.1",
                    "gpt-5.1-codex": "gpt-5.1-codex",
                    "gpt-5.1-codex-max": "gpt-5.1-codex-max",
                    "gpt-5.1-codex-mini": "gpt-5.1-codex-mini",
                    "gpt-5.2": "gpt-5.2",
                    "gpt-5.2-codex": "gpt-5.2-codex",
                    "gpt-5.3": "gpt-5.3",
                    "gpt-5.3-codex": "gpt-5.3-codex",
                    "gpt-5.4": "gpt-5.4"
                },
                "organization_id": acc.workspace_id or "",
                "refresh_token": acc.refresh_token or ""
            },
            "extra": {},
            "concurrency": 10,
            "priority": 1,
            "rate_multiplier": 1,
            "auto_pause_on_expired": True
        }

    with get_db() as db:
        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )
        accounts = db.query(Account).filter(Account.id.in_(ids)).all()

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        payload = {
            "proxies": [],
            "accounts": [make_account_entry(acc) for acc in accounts]
        }
        content = json.dumps(payload, ensure_ascii=False, indent=2)

        if len(accounts) == 1:
            filename = f"{accounts[0].email}_sub2api.json"
        else:
            filename = f"sub2api_tokens_{timestamp}.json"

        return StreamingResponse(
            iter([content]),
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )


@router.post("/export/cpa")
async def export_accounts_cpa(request: BatchExportRequest):
    """导出账号为 CPA Token JSON 格式（每个账号单独一个 JSON 文件，打包为 ZIP）"""
    import io
    import zipfile
    from ...core.cpa_upload import generate_token_json

    with get_db() as db:
        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )
        accounts = db.query(Account).filter(Account.id.in_(ids)).all()

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        if len(accounts) == 1:
            # 单个账号直接返回 JSON 文件
            acc = accounts[0]
            token_data = generate_token_json(acc)
            content = json.dumps(token_data, ensure_ascii=False, indent=2)
            filename = f"{acc.email}.json"
            return StreamingResponse(
                iter([content]),
                media_type="application/json",
                headers={"Content-Disposition": f"attachment; filename={filename}"}
            )

        # 多个账号打包为 ZIP
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for acc in accounts:
                token_data = generate_token_json(acc)
                content = json.dumps(token_data, ensure_ascii=False, indent=2)
                zf.writestr(f"{acc.email}.json", content)

        zip_buffer.seek(0)
        zip_filename = f"cpa_tokens_{timestamp}.zip"
        return StreamingResponse(
            zip_buffer,
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename={zip_filename}"}
        )


@router.get("/stats/summary")
async def get_accounts_stats():
    """获取账号统计信息"""
    with get_db() as db:
        from sqlalchemy import func

        # 总数
        total = db.query(func.count(Account.id)).scalar()

        # 按状态统计
        status_stats = db.query(
            Account.status,
            func.count(Account.id)
        ).group_by(Account.status).all()

        # 按邮箱服务统计
        service_stats = db.query(
            Account.email_service,
            func.count(Account.id)
        ).group_by(Account.email_service).all()

        return {
            "total": total,
            "by_status": {status: count for status, count in status_stats},
            "by_email_service": {service: count for service, count in service_stats}
        }


# ============== Token 刷新相关 ==============

class TokenRefreshRequest(BaseModel):
    """Token 刷新请求"""
    proxy: Optional[str] = None


class BatchRefreshRequest(BaseModel):
    """批量刷新请求"""
    ids: List[int] = []
    proxy: Optional[str] = None
    select_all: bool = False
    status_filter: Optional[str] = None
    email_service_filter: Optional[str] = None
    search_filter: Optional[str] = None


class TokenValidateRequest(BaseModel):
    """Token 验证请求"""
    proxy: Optional[str] = None


class BatchValidateRequest(BaseModel):
    """批量验证请求"""
    ids: List[int] = []
    proxy: Optional[str] = None
    select_all: bool = False
    status_filter: Optional[str] = None
    email_service_filter: Optional[str] = None
    search_filter: Optional[str] = None


@router.post("/{account_id}/refresh")
async def refresh_account_token(account_id: int, request: TokenRefreshRequest = None):
    """刷新单个账号的 Token"""
    from ...core.token_refresh import refresh_account_token as do_refresh

    # 使用传入的代理或全局代理配置
    proxy = request.proxy if request and request.proxy else get_settings().proxy_url
    result = do_refresh(account_id, proxy)

    if result.success:
        return {
            "success": True,
            "message": "Token 刷新成功",
            "expires_at": result.expires_at.isoformat() if result.expires_at else None
        }
    else:
        return {
            "success": False,
            "error": result.error_message
        }


@router.post("/batch-refresh")
async def batch_refresh_tokens(request: BatchRefreshRequest, background_tasks: BackgroundTasks):
    """批量刷新账号 Token"""
    from ...core.token_refresh import refresh_account_token as do_refresh

    # 使用传入的代理或全局代理配置
    proxy = request.proxy if request.proxy else get_settings().proxy_url

    results = {
        "success_count": 0,
        "failed_count": 0,
        "errors": []
    }

    with get_db() as db:
        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )

    for account_id in ids:
        try:
            result = do_refresh(account_id, proxy)
            if result.success:
                results["success_count"] += 1
            else:
                results["failed_count"] += 1
                results["errors"].append({"id": account_id, "error": result.error_message})
        except Exception as e:
            results["failed_count"] += 1
            results["errors"].append({"id": account_id, "error": str(e)})

    return results


@router.post("/{account_id}/validate")
async def validate_account_token(account_id: int, request: TokenValidateRequest = None):
    """验证单个账号的 Token 有效性"""
    from ...core.token_refresh import validate_account_token as do_validate

    # 使用传入的代理或全局代理配置
    proxy = request.proxy if request and request.proxy else get_settings().proxy_url
    is_valid, error = do_validate(account_id, proxy)

    return {
        "id": account_id,
        "valid": is_valid,
        "error": error
    }


@router.post("/batch-validate")
async def batch_validate_tokens(request: BatchValidateRequest):
    """批量验证账号 Token 有效性"""
    from ...core.token_refresh import validate_account_token as do_validate

    # 使用传入的代理或全局代理配置
    proxy = request.proxy if request.proxy else get_settings().proxy_url

    results = {
        "valid_count": 0,
        "invalid_count": 0,
        "details": []
    }

    with get_db() as db:
        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )

    for account_id in ids:
        try:
            is_valid, error = do_validate(account_id, proxy)
            results["details"].append({
                "id": account_id,
                "valid": is_valid,
                "error": error
            })
            if is_valid:
                results["valid_count"] += 1
            else:
                results["invalid_count"] += 1
        except Exception as e:
            results["invalid_count"] += 1
            results["details"].append({
                "id": account_id,
                "valid": False,
                "error": str(e)
            })

    return results


# ============== CPA 上传相关 ==============

class CPAUploadRequest(BaseModel):
    """CPA 上传请求"""
    proxy: Optional[str] = None
    cpa_service_id: Optional[int] = None  # 指定 CPA 服务 ID，不传则使用全局配置


class BatchCPAUploadRequest(BaseModel):
    """批量 CPA 上传请求"""
    ids: List[int] = []
    proxy: Optional[str] = None
    select_all: bool = False
    status_filter: Optional[str] = None
    email_service_filter: Optional[str] = None
    search_filter: Optional[str] = None
    cpa_service_id: Optional[int] = None  # 指定 CPA 服务 ID，不传则使用全局配置


@router.post("/{account_id}/upload-cpa")
async def upload_account_to_cpa(account_id: int, request: CPAUploadRequest = None):
    """上传单个账号到 CPA"""
    from ...core.cpa_upload import upload_to_cpa, generate_token_json

    proxy = request.proxy if request and request.proxy else get_settings().proxy_url
    cpa_service_id = request.cpa_service_id if request else None

    # 解析指定的 CPA 服务
    cpa_api_url = None
    cpa_api_token = None
    if cpa_service_id:
        with get_db() as db:
            svc = crud.get_cpa_service_by_id(db, cpa_service_id)
            if not svc:
                raise HTTPException(status_code=404, detail="指定的 CPA 服务不存在")
            cpa_api_url = svc.api_url
            cpa_api_token = svc.api_token

    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")

        if not account.access_token:
            return {
                "success": False,
                "error": "账号缺少 Token，无法上传"
            }

        # 生成 Token JSON
        token_data = generate_token_json(account)

        # 上传
        success, message = upload_to_cpa(token_data, proxy, api_url=cpa_api_url, api_token=cpa_api_token)

        if success:
            account.cpa_uploaded = True
            account.cpa_uploaded_at = datetime.utcnow()
            db.commit()
            return {"success": True, "message": message}
        else:
            return {"success": False, "error": message}


@router.post("/batch-upload-cpa")
async def batch_upload_accounts_to_cpa(request: BatchCPAUploadRequest):
    """批量上传账号到 CPA"""
    from ...core.cpa_upload import batch_upload_to_cpa

    proxy = request.proxy if request.proxy else get_settings().proxy_url

    # 解析指定的 CPA 服务
    cpa_api_url = None
    cpa_api_token = None
    if request.cpa_service_id:
        with get_db() as db:
            svc = crud.get_cpa_service_by_id(db, request.cpa_service_id)
            if not svc:
                raise HTTPException(status_code=404, detail="指定的 CPA 服务不存在")
            cpa_api_url = svc.api_url
            cpa_api_token = svc.api_token

    with get_db() as db:
        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )

    results = batch_upload_to_cpa(ids, proxy, api_url=cpa_api_url, api_token=cpa_api_token)
    return results
