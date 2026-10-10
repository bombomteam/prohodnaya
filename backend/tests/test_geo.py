"""Геометрия без БД: калибровка (lat, lon) -> пиксели и выбор зоны точки."""
import pytest

from app.core.exceptions import ApiError
from app.services.geo import calibration as cal
from app.services.geo import zones
from helpers import BIG, CAL_POINTS, SMALL


def test_calibration_known_transform():
    c = cal.solve(CAL_POINTS)
    px, py = cal.to_pixels(c, 54.995, 37.005)
    assert (round(float(px), 6), round(float(py), 6)) == (500, 400)
    # повернутое и масштабированное преобразование: px = 2*lat_d + 3*lon_d + 5, py = -lat_d + 4*lon_d + 7 (от первой точки)
    pts = [{"lat": 10 + a, "lon": 20 + b, "px": 2 * a + 3 * b + 5, "py": -a + 4 * b + 7} for a, b in ((0, 0), (1, 0), (0, 1))]
    px, py = cal.to_pixels(cal.solve(pts), 10 + 2.5, 20 - 1.5)
    assert (round(float(px), 6), round(float(py), 6)) == (2 * 2.5 - 3 * 1.5 + 5, -2.5 - 4 * 1.5 + 7)


def test_calibration_works_on_arrays():
    px, py = cal.to_pixels(cal.solve(CAL_POINTS), [54.995, 55.0], [37.005, 37.01])
    assert [round(float(v), 6) for v in px] == [500, 1000] and [round(float(v), 6) for v in py] == [400, 0]


@pytest.mark.parametrize("points", [
    [{"px": 0, "py": 0, "lat": 55.0, "lon": 37.0}, {"px": 100, "py": 0, "lat": 55.0, "lon": 37.01}, {"px": 200, "py": 0, "lat": 55.0, "lon": 37.02}],  # на прямой
    [{"px": 0, "py": 0, "lat": 55.0, "lon": 37.0}] * 3,  # одна точка
    [{"px": 0, "py": 0, "lat": 55, "lon": 37}, {"px": 1, "py": 1, "lat": 56, "lon": 37}, {"px": 2, "py": 2, "lat": 55, "lon": 38}],  # прямая на плане
    [{"px": 0, "py": 0, "lat": 55.0, "lon": 37.0}, {"px": 100, "py": 0, "lat": 56.0, "lon": 37.0}, {"px": 0, "py": 100, "lat": 55.5, "lon": 37.0000001}],  # почти прямая
])
def test_calibration_degenerate(points):
    with pytest.raises(ApiError) as e:
        cal.solve(points)
    assert (e.value.code, e.value.http_status) == ("CALIBRATION_DEGENERATE", 422)
    assert e.value.message == "Точки калибровки лежат почти на одной прямой, выберите другие"


def test_zone_for_point_smaller_area_wins():
    z = zones.build([(1, BIG), (2, SMALL)])
    assert zones.zone_for_point(500, 400, z) == 2  # в обеих — меньшая
    assert zones.zone_for_point(100, 100, z) == 1
    assert zones.zone_for_point(1500, 100, z) is None
    assert zones.zone_for_point(400, 300, z) == 2  # граница внутри
    assert zones.zone_for_point(500, 400, list(reversed(z))) == 2  # порядок списка не важен
    assert zones.zone_for_point(500, 400, []) is None


def test_zone_for_point_equal_area_lowest_id():
    z = zones.build([(7, SMALL), (3, SMALL)])
    assert zones.zone_for_point(500, 400, z) == 3


def test_validate_zones():
    zones.validate([{"name": "A", "polygon": BIG}, {"name": "B", "polygon": SMALL}])
    for bad, code in (
        ([{"name": "A", "polygon": BIG}, {"name": "A", "polygon": SMALL}], "ZONE_NAME_DUPLICATE"),
        ([{"name": "A", "polygon": [[0, 0], [10, 10], [10, 0], [0, 10]]}], "ZONE_POLYGON_INVALID"),  # «бабочка»
        ([{"name": "A", "polygon": [[0, 0], [1, 1], [2, 2]]}], "ZONE_POLYGON_INVALID"),  # нулевая площадь
        ([{"name": "A", "polygon": [[0, 0], [1, 0]]}], "ZONE_POLYGON_INVALID"),
        ([{"name": "A", "polygon": [[0, 0], [float("nan"), 0], [1, 1]]}], "ZONE_POLYGON_INVALID"),
    ):
        with pytest.raises(ApiError) as e:
            zones.validate(bad)
        assert (e.value.code, e.value.http_status) == (code, 422)
