from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field, field_validator


class AnnualFeeSchema(BaseModel):
    primary: int | None = None
    supplementary: int | None = None
    waiver_description: str | None = None


class BenefitExtractSchema(BaseModel):
    category: str
    title: str
    description: str
    quota: str | None = None
    cycle: str | None = None
    source_url: str = ""

    @field_validator("source_url")
    @classmethod
    def validate_benefit_source_url(cls, value: str) -> str:
        clean = str(value or "").strip()
        if not clean:
            return ""
        parsed = urlsplit(clean)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
            raise ValueError("Benefit source_url must use http or https scheme with a host")
        return clean


class ArtworkExtractSchema(BaseModel):
    artwork_url: str
    image_hash: str = ""
    is_primary: bool = False


class SourceExtractSchema(BaseModel):
    title: str
    url: str
    source_type: str = "official_site"
    tier: Literal["S", "A", "B", "C"] = "S"

    @field_validator("url")
    @classmethod
    def validate_source_url(cls, value: str) -> str:
        clean = str(value or "").strip()
        parsed = urlsplit(clean)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
            raise ValueError("Source URL must use http or https scheme with a host")
        return clean


class CardExtractCandidate(BaseModel):
    bank_id: str
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    networks: list[str] = Field(default_factory=list)
    card_level: str | None = None
    status: str = "active"
    annual_fee: AnnualFeeSchema = Field(default_factory=AnnualFeeSchema)
    benefits: list[BenefitExtractSchema] = Field(default_factory=list)
    artworks: list[ArtworkExtractSchema] = Field(default_factory=list)
    sources: list[SourceExtractSchema] = Field(default_factory=list)
    confidence: float = 1.0


# --- API / Database Representation Schemas ---

class BankRegistryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    bank_id: str
    bank_name: str
    official_homepage: str
    credit_card_catalog_url: str
    fee_schedule_url: str | None = None
    notes: str | None = None
    enabled: bool
    customer_service_phone: str | None = None
    vip_service_phone: str | None = None
    overseas_phone: str | None = None
    service_hours: str | None = None
    service_info_source_url: str | None = None
    service_info_verified_at: datetime | None = None


class CatalogBenefitRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    category: str
    title: str
    description: str
    quota: str | None = None
    cycle: str | None = None
    source_url: str | None = None


class CatalogArtworkRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    artwork_url: str
    image_hash: str
    local_path: str | None = None
    is_primary: bool


class CatalogSourceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    title: str
    url: str
    source_type: str
    tier: str


class CatalogProductRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    bank_id: str
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    networks: list[str] = Field(default_factory=list)
    card_level: str | None = None
    status: str
    annual_fee_primary_minor: int | None = None
    annual_fee_supp_minor: int | None = None
    annual_fee_waiver_desc: str | None = None
    verified_at: datetime | None = None
    benefits: list[CatalogBenefitRead] = Field(default_factory=list)
    artworks: list[CatalogArtworkRead] = Field(default_factory=list)
    sources: list[CatalogSourceRead] = Field(default_factory=list)


class DictionaryChangeRequestRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    run_id: str | None = None
    bank_id: str
    card_id: str | None = None
    change_type: str
    target_product_name: str
    old_value: Any = None
    new_value: Any = None
    source_url: str
    source_title: str
    evidence: str
    confidence: float
    status: str
    created_at: datetime
    reviewed_at: datetime | None = None


class CatalogScanRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    scope: str
    target_bank_id: str | None = None
    target_card_id: str | None = None
    status: str
    started_at: datetime
    completed_at: datetime | None = None
    triggered_by: str
    total_banks: int
    completed_banks: int
    failed_banks: int
    discovered_changes: int
    error_summary: str | None = None


class CatalogStatsRead(BaseModel):
    total_products: int
    total_banks: int
    total_artworks: int
    products_with_annual_fee: int
    products_with_benefits: int
    last_verified_at: datetime | None = None
    next_scheduled_at: str | None = None
    pending_changes_count: int
    changes_by_type: dict[str, int] = Field(default_factory=dict)


class ScanTriggerRequest(BaseModel):
    scope: Literal["FULL", "BANK", "CARD"] = "FULL"
    bank_id: str | None = None
    card_id: str | None = None


class BatchChangeApprovalRequest(BaseModel):
    request_ids: list[str]
    action: Literal["APPROVE", "REJECT"]
