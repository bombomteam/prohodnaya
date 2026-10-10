"""Отчёты по смене и человеку (PRD §8.8): цифры берутся из repositories/queries."""
from sqlalchemy.orm import Session

from app.repositories import queries
from app.schemas.reports import PersonReportOut, ShiftReportOut
from app.services.analytics import check_range


def shift_report(db: Session, shift_id: int) -> ShiftReportOut:
    """Отчёт по смене; нет смены — 404 SHIFT_NOT_FOUND."""
    return ShiftReportOut(**queries.shift_report(db, shift_id))


def person_report(db: Session, person_id: int, date_from, date_to) -> PersonReportOut:
    """Отчёт по человеку за период; нет человека — 404 PERSON_NOT_FOUND."""
    check_range(date_from, date_to)
    return PersonReportOut(**queries.person_report(db, person_id, date_from, date_to))
