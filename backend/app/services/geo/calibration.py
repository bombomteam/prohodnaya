"""Привязка плана к карте: аффинное преобразование (lat, lon) -> (px, py) по трём опорным точкам."""
from dataclasses import dataclass

import numpy as np

from app.core.exceptions import ApiError

# Минимальное отношение «удвоенная площадь / квадрат длинной стороны» (около 0.87 у равностороннего):
# меньше — точки почти на одной прямой и преобразование нельзя считать надёжным.
MIN_TRIANGLE_QUALITY = 0.01

_DEGENERATE = ("CALIBRATION_DEGENERATE", "Точки калибровки лежат почти на одной прямой, выберите другие", 422)


@dataclass(frozen=True)
class Calibration:
    """px = a*dlat + b*dlon + c, py = d*dlat + e*dlon + f, где dlat/dlon — отсчёт от первой опорной точки.

    Отсчёт от первой точки нужен для точности: сами градусы (55.75, 37.62) почти равны между собой,
    а разности малы.
    """

    lat0: float
    lon0: float
    coeffs: tuple[float, float, float, float, float, float]


def _quality(pts: np.ndarray) -> float:
    """Удвоенная площадь треугольника, делённая на квадрат его длинной стороны: 0 — все точки на прямой."""
    u, v = pts[1] - pts[0], pts[2] - pts[0]
    cross = abs(u[0] * v[1] - u[1] * v[0])
    longest = max(np.sum((pts[i] - pts[j]) ** 2) for i, j in ((0, 1), (1, 2), (0, 2)))
    return float(cross / longest) if longest > 0 else 0.0


def solve(points: list[dict]) -> Calibration:
    """Считает преобразование из трёх точек {px, py, lat, lon}: система из 6 линейных уравнений (numpy).

    Вырожденный треугольник (по координатам карты или по пикселям плана) даёт 422 CALIBRATION_DEGENERATE.
    """
    geo = np.array([[p["lat"], p["lon"]] for p in points], dtype=float)
    pix = np.array([[p["px"], p["py"]] for p in points], dtype=float)
    if not (np.isfinite(geo).all() and np.isfinite(pix).all()):
        raise ApiError(*_DEGENERATE)
    if not min(_quality(geo), _quality(pix)) >= MIN_TRIANGLE_QUALITY:  # not >= ловит и nan
        raise ApiError(*_DEGENERATE)
    d = geo - geo[0]
    a = np.zeros((6, 6))
    b = np.zeros(6)
    for i in range(3):
        a[2 * i, 0:3] = (d[i, 0], d[i, 1], 1)  # px
        a[2 * i + 1, 3:6] = (d[i, 0], d[i, 1], 1)  # py
        b[2 * i], b[2 * i + 1] = pix[i]
    try:
        coeffs = np.linalg.solve(a, b)
    except np.linalg.LinAlgError:
        raise ApiError(*_DEGENERATE)
    if not np.isfinite(coeffs).all():
        raise ApiError(*_DEGENERATE)
    return Calibration(float(geo[0, 0]), float(geo[0, 1]), tuple(float(c) for c in coeffs))


def to_pixels(cal: Calibration, lat, lon):
    """(lat, lon) -> (px, py) на плане. Принимает числа и numpy-массивы."""
    a, b, c, d, e, f = cal.coeffs
    dlat, dlon = np.asarray(lat) - cal.lat0, np.asarray(lon) - cal.lon0
    return a * dlat + b * dlon + c, d * dlat + e * dlon + f
