from datetime import datetime
from typing import Optional

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("groups.id"))


class Person(Base):
    __tablename__ = "persons"
    __table_args__ = (CheckConstraint("status IN ('active','blocked')", name="persons_status_check"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    fio: Mapped[str] = mapped_column(Text)
    position: Mapped[Optional[str]] = mapped_column(Text)
    group_id: Mapped[Optional[int]] = mapped_column(ForeignKey("groups.id"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'active'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FaceTemplate(Base):
    __tablename__ = "face_templates"

    person_id: Mapped[int] = mapped_column(ForeignKey("persons.id", ondelete="CASCADE"), primary_key=True)
    embedding: Mapped[str] = mapped_column(Text)  # base64 float32 от модуля face, бэкенд не разбирает
    model_version: Mapped[str] = mapped_column(Text)
    reference_hash: Mapped[str] = mapped_column(Text)
    photo_path: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
