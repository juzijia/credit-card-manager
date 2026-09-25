from fastapi import APIRouter

from app.api.v1 import accounts, analytics, auth, backups, cards, catalog, dashboard, imap, imports, manual_bills, notifications, statements, transactions
from app.api.v1 import mortgage

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(accounts.router)
api_router.include_router(cards.router)
api_router.include_router(catalog.router)
api_router.include_router(manual_bills.router)
api_router.include_router(mortgage.router)
api_router.include_router(statements.router)
api_router.include_router(dashboard.router)
api_router.include_router(imports.router)
api_router.include_router(imap.router)
api_router.include_router(transactions.router)
api_router.include_router(notifications.router)
api_router.include_router(analytics.router)
api_router.include_router(backups.router)
