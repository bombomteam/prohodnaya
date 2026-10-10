from datetime import datetime
from typing import Optional

from sqlalchemy import REAL, BigInteger, CheckConstraint, DateTime, Double, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class SitePlan(Base):
    __tablename__ = "site_plan"
    __table_args__ = (CheckConstraint("id = 1", name="site_plan_id_check"),)  # один объект

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False, server_default=text("1"))
    image_path: Mapped[str] = mapped_column(Text)
    width: Mapped[int]
    height: Mapped[int]
    calibration: Mapped[Optional[list]] = mapped_column(JSONB)  # [{px,py,lat,lon} x3]


class Zone(Base):
    __tablename__ = "zones"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    polygon: Mapped[list] = mapped_column(JSONB)  # [[x,y],...] в пикселях плана


class LocationPoint(Base):
    __tablename__ = "location_points"
    __table_args__ = (
        Index("location_points_shift_id_ts_idx", "shift_id", "ts"),
        Index("location_points_ts_idx", "ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    shift_id: Mapped[int] = mapped_column(ForeignKey("shifts.id"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lat: Mapped[float] = mapped_column(Double)
    lon: Mapped[float] = mapped_column(Double)
    accuracy: Mapped[Optional[float]] = mapped_column(REAL)
    zone_id: Mapped[Optional[int]] = mapped_column(ForeignKey("zones.id", ondelete="SET NULL"))
