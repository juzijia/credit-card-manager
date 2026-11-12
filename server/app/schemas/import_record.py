from pydantic import BaseModel

from app.schemas.statement import StatementRead


class ImportResult(BaseModel):
    import_id: str
    bank_code: str
    parser_version: str
    deduplicated: bool
    corrected: bool = False
    statement: StatementRead
    statements: list[StatementRead]

