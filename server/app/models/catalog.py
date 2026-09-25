import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin


class BankRegistry(TimestampMixin, Base):
    __tablename__ = "bank_registries"

    bank_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    bank_name: Mapped[str] = mapped_column(String(64), nullable=False)
    official_homepage: Mapped[str] = mapped_column(String(256), nullable=False)
    credit_card_catalog_url: Mapped[str] = mapped_column(String(512), nullable=False)
    fee_schedule_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(256), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean(), default=True, nullable=False)

    customer_service_phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vip_service_phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    overseas_phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    service_hours: Mapped[str | None] = mapped_column(String(64), nullable=True, default="7×24小时")
    service_info_source_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    service_info_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    products: Mapped[list["CatalogProduct"]] = relationship(
        back_populates="bank", cascade="all, delete-orphan"
    )


class CatalogProduct(TimestampMixin, Base):
    __tablename__ = "catalog_products"
    __table_args__ = (
        UniqueConstraint("bank_id", "canonical_name", name="uq_catalog_products_bank_name"),
        Index("ix_catalog_products_bank_id", "bank_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    bank_id: Mapped[str] = mapped_column(
        String(16), ForeignKey("bank_registries.bank_id", ondelete="CASCADE"), nullable=False
    )
    canonical_name: Mapped[str] = mapped_column(String(64), nullable=False)
    aliases_json: Mapped[str] = mapped_column(Text(), default="[]", nullable=False)
    networks_json: Mapped[str] = mapped_column(Text(), default="[]", nullable=False)
    card_level: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    annual_fee_primary_minor: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    annual_fee_supp_minor: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    annual_fee_waiver_desc: Mapped[str | None] = mapped_column(String(512), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    bank: Mapped["BankRegistry"] = relationship(back_populates="products")
    artworks: Mapped[list["CatalogArtwork"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )
    benefits: Mapped[list["CatalogBenefit"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )
    sources: Mapped[list["CatalogSource"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )


class CatalogArtwork(TimestampMixin, Base):
    __tablename__ = "catalog_artworks"
    __table_args__ = (
        Index("ix_catalog_artworks_product_id", "product_id"),
        Index("ix_catalog_artworks_hash", "image_hash"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    product_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("catalog_products.id", ondelete="CASCADE"), nullable=False
    )
    artwork_url: Mapped[str] = mapped_column(String(512), nullable=False)
    image_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    local_path: Mapped[str | None] = mapped_column(String(256), nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean(), default=False, nullable=False)

    product: Mapped["CatalogProduct"] = relationship(back_populates="artworks")


class CatalogBenefit(TimestampMixin, Base):
    __tablename__ = "catalog_benefits"
    __table_args__ = (
        Index("ix_catalog_benefits_product_id", "product_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    product_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("catalog_products.id", ondelete="CASCADE"), nullable=False
    )
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(String(1024), nullable=False)
    quota: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cycle: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    product: Mapped["CatalogProduct"] = relationship(back_populates="benefits")


class CatalogSource(TimestampMixin, Base):
    __tablename__ = "catalog_sources"
    __table_args__ = (
        Index("ix_catalog_sources_product_id", "product_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    product_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("catalog_products.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), default="official_site", nullable=False)
    tier: Mapped[str] = mapped_column(String(4), default="S", nullable=False)

    product: Mapped["CatalogProduct"] = relationship(back_populates="sources")


class CatalogScanRun(TimestampMixin, Base):
    __tablename__ = "catalog_scan_runs"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    target_bank_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    target_card_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="RUNNING", nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    triggered_by: Mapped[str] = mapped_column(String(64), default="manual", nullable=False)
    total_banks: Mapped[int] = mapped_column(Integer(), default=0, nullable=False)
    completed_banks: Mapped[int] = mapped_column(Integer(), default=0, nullable=False)
    failed_banks: Mapped[int] = mapped_column(Integer(), default=0, nullable=False)
    discovered_changes: Mapped[int] = mapped_column(Integer(), default=0, nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text(), nullable=True)

    items: Mapped[list["CatalogScanItem"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    change_requests: Mapped[list["DictionaryChangeRequest"]] = relationship(
        back_populates="run"
    )


class CatalogScanItem(TimestampMixin, Base):
    __tablename__ = "catalog_scan_items"
    __table_args__ = (
        Index("ix_catalog_scan_items_run_id", "run_id"),
        Index("ix_catalog_scan_items_bank_id", "bank_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("catalog_scan_runs.id", ondelete="CASCADE"), nullable=False
    )
    bank_id: Mapped[str] = mapped_column(String(16), nullable=False)
    card_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer(), default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text(), nullable=True)

    run: Mapped["CatalogScanRun"] = relationship(back_populates="items")


class DictionaryChangeRequest(TimestampMixin, Base):
    __tablename__ = "dictionary_change_requests"
    __table_args__ = (
        Index("ix_change_requests_status", "status"),
        Index("ix_change_requests_bank_id", "bank_id"),
        Index("ix_change_requests_run_id", "run_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("catalog_scan_runs.id", ondelete="SET NULL"), nullable=True
    )
    bank_id: Mapped[str] = mapped_column(String(16), nullable=False)
    card_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    change_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_product_name: Mapped[str] = mapped_column(String(64), nullable=False)
    old_value_json: Mapped[str | None] = mapped_column(Text(), nullable=True)
    new_value_json: Mapped[str | None] = mapped_column(Text(), nullable=True)
    source_url: Mapped[str] = mapped_column(String(512), nullable=False)
    source_title: Mapped[str] = mapped_column(String(128), nullable=False)
    evidence: Mapped[str] = mapped_column(Text(), nullable=False)
    confidence: Mapped[float] = mapped_column(Float(), default=1.0, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    run: Mapped["CatalogScanRun | None"] = relationship(back_populates="change_requests")

