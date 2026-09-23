"""Role based access control for the control plane.

Roles are assigned by the system administrator and resolved on every request from the
active membership row. The browser and the Telegram bot receive the same permission set
from ``/v1/webapp/capabilities``; neither one is allowed to decide access on its own.
"""
from __future__ import annotations

ROLES = ("system_admin", "reseller_admin", "operator", "finance", "support", "viewer")

# Assignable roles are every role except the root administrator identity, which is derived
# from ROOT_TELEGRAM_ID and can never be granted through an API payload.
ASSIGNABLE_ROLES = ROLES[1:]

VIEW = {
    "view_dashboard", "view_finance", "view_audit", "export_reports",
    "create_support_ticket", "answer_support",
}
MONEY_REQUEST = {"request_funding", "request_adjustment"}
MONEY_DECIDE = {"decide_funding"}
INFRA = {"manage_servers", "node_control", "subscriber_control", "admin_limit"}
NETWORK = {"manage_resellers"}
# The usage coefficient turns panel bytes into money for the bound organization, so letting
# the account that is billed change it would be self-service pricing. Reserved for the root.
PRICING = {"change_billing_coefficient"}

# Correcting a settled balance is the one money operation that is never delegated: the
# requester and the approver must be two different people, and only the system
# administrator is allowed to approve.
MATRIX: dict[str, set[str]] = {
    "system_admin": VIEW | MONEY_REQUEST | MONEY_DECIDE | INFRA | NETWORK | PRICING | {"decide_adjustment"},
    "reseller_admin": VIEW | MONEY_REQUEST | MONEY_DECIDE | INFRA | NETWORK,
    "operator": {"view_dashboard", "view_audit", "export_reports", "manage_servers", "node_control", "subscriber_control", "admin_limit"},
    "finance": {"view_dashboard", "view_finance", "view_audit", "export_reports", "create_support_ticket"} | MONEY_REQUEST | MONEY_DECIDE,
    "support": {"view_dashboard", "view_finance", "create_support_ticket", "answer_support", "subscriber_control"},
    "viewer": {"view_dashboard", "view_finance", "export_reports"},
}

ALL_PERMISSIONS = sorted(VIEW | MONEY_REQUEST | MONEY_DECIDE | INFRA | NETWORK | PRICING | {"decide_adjustment"})

LABELS_FA = {
    "view_dashboard": "مشاهدهٔ داشبورد",
    "view_finance": "مشاهدهٔ مالی",
    "view_audit": "مشاهدهٔ گزارش‌های رسیدگی",
    "export_reports": "خروجی گزارش",
    "create_support_ticket": "ثبت درخواست پشتیبانی",
    "answer_support": "پاسخ به پشتیبانی",
    "request_funding": "درخواست افزایش اعتبار",
    "decide_funding": "تأیید یا رد درخواست مالی",
    "request_adjustment": "درخواست اصلاح حساب",
    "decide_adjustment": "تأیید اصلاح حساب",
    "manage_servers": "مدیریت سرورها",
    "node_control": "روشن/خاموش و بازنشانی نود",
    "subscriber_control": "مدیریت مشترکان",
    "admin_limit": "تغییر سقف پنل",
    "change_billing_coefficient": "تغییر ضریب صورتحساب",
    "manage_resellers": "مدیریت نمایندگان",
}


def normalize(role: str | None) -> str:
    """Map stored or supplied values onto a known role; unknown roles keep no privileges."""
    return role if role in MATRIX else "viewer"


def permissions_for(role: str | None) -> set[str]:
    return set(MATRIX[normalize(role)])


def can(role: str | None, permission: str) -> bool:
    return permission in permissions_for(role)


def permission_payload(role: str) -> dict[str, bool]:
    return {name: can(role, name) for name in ALL_PERMISSIONS}


def denial_fa(permission: str) -> str:
    return f"شما اجازهٔ «{LABELS_FA.get(permission, permission)}» را ندارید."


def role_fa(role: str | None) -> str:
    return {
        "system_admin": "مدیر کل سیستم",
        "reseller_admin": "مدیر نمایندگی",
        "operator": "اپراتور",
        "finance": "مدیر مالی",
        "support": "پشتیبان",
        "viewer": "مشاهده‌گر",
    }.get(normalize(role), str(role or "کاربر"))
