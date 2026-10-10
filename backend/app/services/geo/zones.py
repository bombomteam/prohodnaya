"""Зоны плана: проверка контуров и определение зоны точки."""
import math
from dataclasses import dataclass

import numpy as np
import shapely
from shapely.geometry import Polygon

from app.core.exceptions import ApiError


@dataclass(frozen=True)
class ZoneGeom:
    id: int
    polygon: Polygon
    area: float


def validate(zones: list[dict]) -> None:
    """Проверяет набор зон {name, polygon}: имена уникальны, каждый контур ≥ 3 точек и корректен в shapely.

    Контур корректен, если у него конечные координаты, нет самопересечений и площадь больше нуля.
    Иначе 422 ZONE_NAME_DUPLICATE / ZONE_POLYGON_INVALID.
    """
    seen = set()
    for z in zones:
        if z["name"] in seen:
            raise ApiError("ZONE_NAME_DUPLICATE", f"Зона «{z['name']}» повторяется, названия зон должны быть разными", 422)
        seen.add(z["name"])
        pts = z["polygon"]
        if len(pts) < 3 or not all(math.isfinite(v) for p in pts for v in p):
            raise ApiError("ZONE_POLYGON_INVALID", f"Контур зоны «{z['name']}» некорректен: нужно минимум 3 точки", 422)
        poly = Polygon(pts)
        if not poly.is_valid or poly.area <= 0:
            raise ApiError("ZONE_POLYGON_INVALID", f"Контур зоны «{z['name']}» некорректен: стороны пересекаются или площадь нулевая", 422)


def build(rows: list[tuple[int, list]]) -> list[ZoneGeom]:
    """Строит геометрии из строк (id, polygon) таблицы zones."""
    geoms = []
    for zone_id, pts in rows:
        poly = Polygon(pts)
        geoms.append(ZoneGeom(zone_id, poly, poly.area))
    return geoms


def zones_for_points(px, py, zones: list[ZoneGeom]) -> list[int | None]:
    """Зона для каждой точки плана (векторно); None — точка вне всех зон.

    Правило пересечения: если точка попала в несколько зон, берётся зона с МЕНЬШЕЙ площадью (вложенная
    зона точнее объемлющей); при равных площадях — с меньшим id. Граница контура считается внутри зоны.
    """
    px, py = np.atleast_1d(px).astype(float), np.atleast_1d(py).astype(float)
    result = np.full(len(px), -1)
    # от больших к малым, при равных площадях — по убыванию id: последняя подошедшая зона перезаписывает
    for z in sorted(zones, key=lambda z: (-z.area, -z.id)):
        result[shapely.intersects_xy(z.polygon, px, py)] = z.id
    return [int(r) if r >= 0 else None for r in result]


def zone_for_point(px: float, py: float, zones: list[ZoneGeom]) -> int | None:
    """Зона одной точки плана (см. правило в zones_for_points)."""
    return zones_for_points([px], [py], zones)[0]
