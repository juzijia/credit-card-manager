import json
import threading
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, selectinload

from urllib.parse import urlsplit
from app.core.notification_secrets import is_credential_bearing_url
from app.core.plain_secrets import get_secret_json
from app.models.card import Card
from app.models.manual_bill import ManualBillNotificationLog
from app.models.mixins import utc_now
from app.models.notification import NotificationLog, NotificationSetting
from app.models.statement import Statement
from app.notifications.channels import dispatch_notification
from app.services.statements import is_active_due_predicate, local_today, remaining_minor, status_for
from app.services.system_settings import get_history_cutoff_date
from app.services.manual_bills import ensure_current_payments


_BANK_NAMES = {
    "CGB": "广发银行",
    "CMB": "招商银行",
    "PAB": "平安银行",
    "CITIC": "中信银行",
    "ICBC": "中国工商银行",
    "CCB": "中国建设银行",
    "BOCOM": "交通银行",
    "ABC": "中国农业银行",
    "BOC": "中国银行",
}


# ponytail: the supported deployment has one process; use a cross-process lock
# only if that deployment contract changes.
_NOTIFICATION_LOCK = threading.Lock()


def _format_amount(amount_minor: int, currency: str) -> str:
    """Currency-aware amount rendering; '¥' is only valid for CNY."""
    symbol = "¥" if currency == "CNY" else ""
    suffix = "" if currency == "CNY" else f" {currency}"
    return f"{symbol}{amount_minor / 100:.2f}{suffix}"


@dataclass(frozen=True, slots=True)
class ProviderSchema:
    public_keys: frozenset[str]
    secret_keys: frozenset[str]


NOTIFICATION_URL_KEYS: frozenset[str] = frozenset(
    {"url", "server", "endpoint", "api_url", "proxy_url"}
)


def validate_http_endpoint(
    value: str,
    field_name: str = "url",
    allowed_schemes: tuple[str, ...] = ("http", "https"),
) -> str:
    clean = str(value or "").strip()
    if not clean:
        return clean
    parsed = urlsplit(clean)
    if parsed.scheme.lower() not in allowed_schemes or not parsed.netloc:
        schemes_str = " or ".join(allowed_schemes)
        raise ValueError(f"{field_name} must use {schemes_str} scheme with a host")
    return clean


def validate_provider_config_endpoints(provider: str, config: dict[str, Any]) -> None:
    provider_lower = provider.lower()
    for key in NOTIFICATION_URL_KEYS:
        if key in config and config[key]:
            if key == "proxy_url":
                validate_http_endpoint(
                    config[key],
                    field_name=f"{provider} {key}",
                    allowed_schemes=("http", "https", "socks5", "socks5h"),
                )
            else:
                validate_http_endpoint(config[key], field_name=f"{provider} {key}")
    if provider_lower == "feishu" and "key" in config:
        key_val = str(config["key"] or "").strip()
        if key_val.startswith("http:") or key_val.startswith("https:") or "://" in key_val:
            validate_http_endpoint(key_val, field_name="feishu key")


PROVIDER_SCHEMAS: dict[str, ProviderSchema] = {
    "bark": ProviderSchema(
        public_keys=frozenset({"server", "group", "icon", "sound"}),
        secret_keys=frozenset({"key"}),
    ),
    "qywx_am": ProviderSchema(
        public_keys=frozenset({"corpid", "touser", "agentid"}),
        secret_keys=frozenset({"corpsecret", "am"}),
    ),
    "ntfy": ProviderSchema(
        public_keys=frozenset({"server", "topic"}),
        secret_keys=frozenset({"token"}),
    ),
    "serverchan": ProviderSchema(
        public_keys=frozenset(),
        secret_keys=frozenset({"key"}),
    ),
    "pushplus": ProviderSchema(
        public_keys=frozenset({"template"}),
        secret_keys=frozenset({"token"}),
    ),
    "pushdeer": ProviderSchema(
        public_keys=frozenset({"server"}),
        secret_keys=frozenset({"key"}),
    ),
    "qywx": ProviderSchema(
        public_keys=frozenset(),
        secret_keys=frozenset({"key"}),
    ),
    "dingtalk": ProviderSchema(
        public_keys=frozenset(),
        secret_keys=frozenset({"token", "secret"}),
    ),
    "feishu": ProviderSchema(
        public_keys=frozenset(),
        secret_keys=frozenset({"key"}),
    ),
    "telegram": ProviderSchema(
        public_keys=frozenset({"chat_id", "api_url", "proxy_url"}),
        secret_keys=frozenset({"bot_token"}),
    ),
    "gotify": ProviderSchema(
        public_keys=frozenset({"url", "priority"}),
        secret_keys=frozenset({"token"}),
    ),
    "webhook": ProviderSchema(
        public_keys=frozenset({"url"}),
        secret_keys=frozenset({"headers"}),
    ),
}

def get_default_setting(session: Session) -> NotificationSetting | None:
    return session.scalar(
        select(NotificationSetting).where(NotificationSetting.card_id.is_(None))
    )


def get_or_create_default_setting(session: Session) -> NotificationSetting:
    setting = get_default_setting(session)
    if setting is None:
        setting = create_default_setting(session)
    return setting


def create_default_setting(session: Session) -> NotificationSetting:
    setting = NotificationSetting(
        card_id=None,
        enabled=False,
        offset_days_json="[2, 1, 0, -1, -2]",
        push_time="09:00",
        provider="disabled",
        public_config_json="{}",
        only_if_unpaid=True,
    )
    session.add(setting)
    session.commit()
    session.refresh(setting)
    return setting


def separate_provider_config(provider: str, config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    schema = PROVIDER_SCHEMAS.get(provider.lower())
    public_config: dict[str, Any] = {}
    secret_config: dict[str, Any] = {}

    for k, v in config.items():
        if schema is not None:
            if k in schema.public_keys and not is_credential_bearing_url(v):
                public_config[k] = v
            else:
                # Explicit secret key or unknown key defaults to secret
                secret_config[k] = v
        else:
            # Unknown provider: fail-safe by treating all keys as secret
            secret_config[k] = v

    return public_config, secret_config


def assemble_provider_config(setting: NotificationSetting) -> dict[str, Any]:
    try:
        config = json.loads(setting.public_config_json or "{}")
    except Exception:
        config = {}

    secrets_dict = get_secret_json("notification", setting.id)
    if secrets_dict:
        config.update(secrets_dict)
    return config


def test_notification(
    provider: str, config: dict[str, Any]
) -> tuple[bool, str]:
    title = "【私人信用卡管家】通道测试"
    content = "恭喜！通知渠道已联通成功。信用卡管家将在账单到期前按设定的提醒规则向你发送还款通知。"
    return dispatch_notification(provider, config, title, content)


def _describe_due(days_left: int) -> tuple[str, str, str]:
    if days_left < 0:
        overdue_days = abs(days_left)
        return (
            f"🚨【逾期警报】已逾期 {overdue_days} 天",
            f"⚠️ 已逾期 {overdue_days} 天",
            "🚨 该账单已发生逾期！请立即处理还款，避免影响个人征信并产生滞纳金与罚息。",
        )
    if days_left == 0:
        return (
            "⏰【今日到期】最后还款日",
            "⏰ 今日到期",
            "⚡ 今日为最后还款日，请及时还款避免逾期。",
        )
    return (
        f"📅【还款提醒】剩 {days_left} 天到期",
        f"⏳ 还剩 {days_left} 天到期",
        "💡 请及时安排还款，避免逾期产生额外利息。",
    )


def check_and_send_due_reminders(
    session: Session, today: date | None = None
) -> list[NotificationLog | ManualBillNotificationLog]:
    with _NOTIFICATION_LOCK:
        return _check_and_send_due_reminders(session, today)


def _check_and_send_due_reminders(
    session: Session, today: date | None = None
) -> list[NotificationLog | ManualBillNotificationLog]:
    if today is None:
        today = local_today(session.info.get("timezone", "Asia/Shanghai"))

    setting = get_default_setting(session)
    if not setting or not setting.enabled:
        return []

    try:
        offsets = json.loads(setting.offset_days_json)
    except Exception:
        offsets = [2, 1, 0, -1, -2]

    config = assemble_provider_config(setting)

    cutoff = get_history_cutoff_date(session)
    if today is not None and today < cutoff:
        cutoff = today - timedelta(days=3)
    statements = session.scalars(
        select(Statement)
        .outerjoin(Statement.card)
        .where(
            or_(
                Statement.card_id.is_(None),
                and_(
                    Card.deleted_at.is_(None),
                    Card.status == "active",
                    Card.reminder_enabled.is_(True),
                ),
            ),
            is_active_due_predicate(cutoff),
        )
        .options(selectinload(Statement.repayments), selectinload(Statement.card))
    ).all()

    pending_cards: list[tuple[Statement, int, int]] = []
    for statement in statements:
        remaining = remaining_minor(statement)
        if setting.only_if_unpaid and remaining <= 0:
            continue

        days_left = (statement.due_date - today).days
        if days_left not in offsets:
            continue

        # Idempotency check: has this (statement_id, offset_days, provider) already been sent?
        existing = session.scalar(
            select(NotificationLog).where(
                NotificationLog.statement_id == statement.id,
                NotificationLog.offset_days == days_left,
                NotificationLog.provider == setting.provider,
                NotificationLog.status == "success",
            )
        )
        if existing:
            continue

        pending_cards.append((statement, remaining, days_left))

    pending_manual: list[tuple[Any, Any, int]] = []
    for bill, payment in ensure_current_payments(session, today):
        if not bill.reminder_enabled or payment.paid_at is not None:
            continue
        days_left = (payment.due_date - today).days
        if days_left not in offsets:
            continue
        existing = session.scalar(
            select(ManualBillNotificationLog).where(
                ManualBillNotificationLog.manual_bill_payment_id == payment.id,
                ManualBillNotificationLog.offset_days == days_left,
                ManualBillNotificationLog.provider == setting.provider,
                ManualBillNotificationLog.status == "success",
            )
        )
        if existing:
            continue
        pending_manual.append((bill, payment, days_left))

    if not pending_cards and not pending_manual:
        return []

    total_count = len(pending_cards) + len(pending_manual)
    # Cross-currency arithmetic is prohibited: per-currency totals only, and
    # manual bills are implicitly CNY. A mixed grand total is never computed.
    totals_by_currency: dict[str, int] = {}
    for _st, remaining, _days in pending_cards:
        currency = _st.currency
        totals_by_currency[currency] = totals_by_currency.get(currency, 0) + remaining
    manual_total = sum(item[1].amount_minor for item in pending_manual)
    if manual_total:
        totals_by_currency["CNY"] = totals_by_currency.get("CNY", 0) + manual_total
    if len(totals_by_currency) == 1:
        only_currency = next(iter(totals_by_currency))
        total_text = _format_amount(totals_by_currency[only_currency], only_currency)
    else:
        total_text = "、".join(
            _format_amount(amount, code) for code, amount in sorted(totals_by_currency.items())
        )

    pending_cards.sort(key=lambda x: x[2])
    pending_manual.sort(key=lambda x: x[2])

    if total_count == 1:
        if pending_cards:
            st, remaining, days_left = pending_cards[0]
            card = st.card
            badge, days_desc, tip = _describe_due(days_left)
            bank_name = card.bank_name if card else _BANK_NAMES.get(st.bank_code, st.bank_code)
            card_label = f"{bank_name}(****{card.last4})" if card else f"{bank_name}(账户账单)"
            display_name = card.display_name if card else f"{bank_name}账户账单"
            title = f"{badge} {card_label} 待还 {_format_amount(remaining, st.currency)}"
            status_text = f"⚠️ 已逾期 {abs(days_left)} 天" if days_left < 0 else ("⏰ 今日到期" if days_left == 0 else "⏳ 待还款")
            content = chr(10).join([
                f"💳 卡片名称: {display_name}",
                f"📆 到期还款日: {st.due_date} ({days_desc})",
                f"💰 本期剩余待还: {_format_amount(remaining, st.currency)}",
                f"🎯 最低还款额: {_format_amount(st.minimum_due_minor or 0, st.currency)}",
                f"📌 当前状态: {status_text}",
                "",
                tip,
            ])
        else:
            bill, payment, days_left = pending_manual[0]
            badge, days_desc, tip = _describe_due(days_left)
            title = f"{badge} {bill.name} 待还 {_format_amount(payment.amount_minor, 'CNY')}"
            status_text = f"⚠️ 已逾期 {abs(days_left)} 天" if days_left < 0 else ("⏰ 今日到期" if days_left == 0 else "⏳ 待还款")
            content = chr(10).join([
                f"📑 账单名称: {bill.name}",
                f"🏷️ 账单类型: {bill.bill_type}",
                f"📆 到期还款日: {payment.due_date} ({days_desc})",
                f"💰 本月待还金额: {_format_amount(payment.amount_minor, 'CNY')}",
                f"📌 当前状态: {status_text}",
                "",
                tip,
            ])
    else:
        overdue_count = sum(1 for _, _, d in pending_cards if d < 0) + sum(1 for _, _, d in pending_manual if d < 0)
        today_count = sum(1 for _, _, d in pending_cards if d == 0) + sum(1 for _, _, d in pending_manual if d == 0)

        if overdue_count > 0:
            title = f"🚨【逾期警报】{overdue_count} 笔已逾期！共 {total_count} 笔待还 {total_text}"
            summary_header = f"🚨 待还汇总：共 {total_count} 笔账单（⚠️ 其中 {overdue_count} 笔已逾期！），合计待还 {total_text}。"
        elif today_count > 0:
            title = f"⏰【还款提醒】{today_count} 笔今日到期！共 {total_count} 笔待还 {total_text}"
            summary_header = f"⏰ 待还汇总：共 {total_count} 笔账单（⚡ 含今日到期 {today_count} 笔），合计待还 {total_text}。"
        else:
            title = f"📅【还款提醒】今日有 {total_count} 笔待还款账单，合计 {total_text}"
            summary_header = f"📊 待还汇总：共 {total_count} 笔账单，合计待还 {total_text}。"

        lines = [summary_header, ""]

        if pending_cards:
            lines.append("💳 信用卡待还清单：")
            for idx, (st, remaining, days_left) in enumerate(pending_cards, 1):
                card = st.card
                bank_name = card.bank_name if card else _BANK_NAMES.get(st.bank_code, st.bank_code)
                card_ident = f"{bank_name} (****{card.last4})" if card else f"{bank_name} (账户账单)"
                if days_left < 0:
                    status_badge = f"⚠️ [已逾期 {abs(days_left)} 天]"
                elif days_left == 0:
                    status_badge = "⏰ [今日到期]"
                else:
                    status_badge = f"⏳ [剩 {days_left} 天]"
                lines.append(f"{idx}. {status_badge} {card_ident}")
                lines.append(f"   • 待还: {_format_amount(remaining, st.currency)} | 到期: {st.due_date}")
            lines.append("")

        if pending_manual:
            lines.append("🏠 手动常规账单：")
            for idx, (bill, payment, days_left) in enumerate(pending_manual, 1):
                if days_left < 0:
                    status_badge = f"⚠️ [已逾期 {abs(days_left)} 天]"
                elif days_left == 0:
                    status_badge = "⏰ [今日到期]"
                else:
                    status_badge = f"⏳ [剩 {days_left} 天]"
                lines.append(f"{idx}. {status_badge} {bill.name} ({bill.bill_type})")
                lines.append(f"   • 待还: {_format_amount(payment.amount_minor, 'CNY')} | 到期: {payment.due_date}")
            lines.append("")

        if overdue_count > 0:
            lines.append("🚨 警告：存在逾期账单，请立即处理以避免产生罚息与影响个人征信！")
        else:
            lines.append("💡 请留意账户余额并及时还款，避免逾期产生利息。")
        content = chr(10).join(lines).strip()

    success, msg = dispatch_notification(setting.provider, config, title, content)

    sent_logs: list[NotificationLog | ManualBillNotificationLog] = []
    status = "success" if success else "failed"
    for st, _, days_left in pending_cards:
        log = session.scalar(
            select(NotificationLog).where(
                NotificationLog.statement_id == st.id,
                NotificationLog.offset_days == days_left,
                NotificationLog.provider == setting.provider,
            )
        )
        if log:
            log.status = status
            log.response_text = msg
            log.sent_at = utc_now()
        else:
            log = NotificationLog(
                statement_id=st.id,
                offset_days=days_left,
                provider=setting.provider,
                status=status,
                response_text=msg,
            )
            session.add(log)
        sent_logs.append(log)

    for _, payment, days_left in pending_manual:
        log = session.scalar(
            select(ManualBillNotificationLog).where(
                ManualBillNotificationLog.manual_bill_payment_id == payment.id,
                ManualBillNotificationLog.offset_days == days_left,
                ManualBillNotificationLog.provider == setting.provider,
            )
        )
        if log:
            log.status = status
            log.response_text = msg
            log.sent_at = utc_now()
        else:
            log = ManualBillNotificationLog(
                manual_bill_payment_id=payment.id,
                offset_days=days_left,
                provider=setting.provider,
                status=status,
                response_text=msg,
            )
            session.add(log)
        sent_logs.append(log)

    session.commit()
    return sent_logs
