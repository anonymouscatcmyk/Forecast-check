"""Run with:  python -m pytest tests  (checks the parsers on saved copies of the pages)."""
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent / "scripts"))
import collect  # noqa: E402

FIX = HERE / "fixtures"


def test_city_table():
    t = collect.parse_forecast_table((FIX / "gradove.html").read_text(encoding="utf-8"))
    assert list(t)[:2] == ["2026-11-08", "2026-11-09"]
    assert t["2026-11-09"]["Видин"] == (1, 6)
    assert t["2026-11-09"]["Шумен"] == (-1, 10)
    assert t["2026-11-10"]["Бургас"] == (8, 13)


def test_mountain_table_skips_today_extra_columns():
    t = collect.parse_forecast_table((FIX / "planini.html").read_text(encoding="utf-8"))
    assert t["2026-11-08"]["вр. Ботев"] == (-8, -2)   # today, snow cover column skipped
    assert t["2026-11-09"]["вр. Мургаш"] == (3, 7)
    assert t["2026-11-10"]["вр. Мусала"] == (-8, -3)  # name with ** cleaned


def test_seaside_table_missing_third_day():
    t = collect.parse_forecast_table((FIX / "moreto.html").read_text(encoding="utf-8"))
    assert t["2026-11-09"]["н. Калиакра"] == (7, 12)
    assert "н. Емине" not in t["2026-11-10"]


def test_stringmeteo():
    o = collect.parse_stringmeteo((FIX / "stringmeteo.html").read_text(encoding="utf-8"))
    assert o["Видин"][7] == [-2.2, 9.0]
    assert o["Видин"][16] == [-0.4, 3.3]
    assert o["Черни вр."][6] == [-6.2, 0.6]
