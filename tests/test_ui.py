"""Sidebar, navigation, icons and the SVG chart."""
import re
from datetime import timedelta

import pytest

from app.db import get_db
from app.navigation import NAV, initials
from app.services.charts import column_chart, nice_ceiling
from app.timeutil import to_text, utc_now
from conftest import login


# ---------- sidebar ----------

@pytest.mark.parametrize("email,role,own_link", [
    ("admin@x.com", "admin", "/admin/"),
    ("warden@x.com", "warden", "/warden/"),
    ("s1@x.com", "student", "/student/"),
])
def test_sidebar_lists_every_menu_entry_for_the_role(client, users, email, role, own_link):
    login(client, email)
    html = client.get(own_link).get_data(as_text=True)
    for label, _endpoint, _icon in NAV[role]:
        assert label in html
    assert f'href="{own_link}" class="nav-link-item active"' in html   # current page highlighted
    assert 'aria-current="page"' in html


def test_unfinished_pages_are_marked_not_linked(client, users, monkeypatch):
    """No page is unfinished any more; a pretend one shows how the menu would mark it (greyed, 'Soon', never a link)."""
    import app.navigation as navigation
    monkeypatch.setitem(navigation.NAV, "admin", [*navigation.NAV["admin"], ("Pretend Page", None, "grid")])
    login(client, "admin@x.com")
    html = client.get("/admin/").get_data(as_text=True)
    assert 'class="nav-link-item disabled"' in html and ">Soon<" in html
    assert not re.search(r'<a [^>]*class="nav-link-item disabled"', html)       # a disabled entry is a <span>, never a dead link


def test_sidebar_hides_unfinished_pages_when_switched_off(client, users, monkeypatch):
    """Every page now exists, so add a pretend unfinished entry to check the switch still works."""
    import app.navigation as navigation
    monkeypatch.setitem(navigation.NAV, "admin", [*navigation.NAV["admin"], ("Pretend Page", None, "grid")])
    login(client, "admin@x.com")
    shown = client.get("/admin/").get_data(as_text=True).split('class="sidebar-nav"')[1].split("</nav>")[0]
    assert "Pretend Page" in shown and ">Soon<" in shown
    monkeypatch.setattr("app.navigation.SHOW_UPCOMING", False)
    hidden = client.get("/admin/").get_data(as_text=True).split('class="sidebar-nav"')[1].split("</nav>")[0]
    assert "Pretend Page" not in hidden and ">Soon<" not in hidden and "Students" in hidden and "Dashboard" in hidden


def test_no_menu_entry_is_left_unfinished():
    """Phase 11: every sidebar entry leads to a real page."""
    import app.navigation as navigation
    assert [label for entries in navigation.NAV.values() for label, endpoint, _ in entries if endpoint is None] == []


def test_admin_menu_never_appears_for_students(client, users):
    login(client, "s1@x.com")
    html = client.get("/student/").get_data(as_text=True)
    assert "Biometric enrollment" not in html and "Settings" not in html


def test_sidebar_has_user_card_and_logout(client, users):
    login(client, "warden@x.com")
    html = client.get("/warden/").get_data(as_text=True)
    assert "Wendy Warden" in html and ">WW<" in html
    assert 'action="/logout"' in html


def test_initials():
    assert initials("Ayesha Khan") == "AK"
    assert initials("Madonna") == "M"
    assert initials("") == "?"


def test_login_page_has_no_sidebar_and_shows_branding(client):
    html = client.get("/login").get_data(as_text=True)
    assert 'id="sidebar"' not in html
    assert "scan-art" in html and "Welcome back" in html


def test_icons_are_inline_svg(client, users):
    login(client, "admin@x.com")
    html = client.get("/admin/").get_data(as_text=True)
    assert html.count('<svg class="icon"') > 10
    assert "fonts.googleapis" not in html and "cdn." not in html     # works offline


# ---------- chart geometry ----------

def _groups(*pairs):
    return [{"label": f"D{i}", "full": f"Day {i}", "values": list(p)} for i, p in enumerate(pairs)]


def test_nice_ceiling():
    assert [nice_ceiling(v) for v in (0, 3, 4, 5, 9, 13, 21, 1500)] == [4, 4, 4, 8, 12, 20, 40, 2000]


def test_chart_bars_follow_the_rules():
    chart = column_chart(_groups((3, 1), (0, 2), (5, 0)), ["Granted", "Denied"])
    assert chart["total"] == 11
    assert len(chart["bars"]) == 4                          # zero-value bars are not drawn
    assert [t["text"] for t in chart["ticks"]] == ["0", "4", "8"]
    tips = [b["tip"] for b in chart["bars"]]
    assert "Day 0: 3 granted" in tips and "Day 1: 2 denied" in tips


@pytest.mark.parametrize("n_groups", [1, 3, 7, 30])
def test_chart_bar_width_is_capped_and_positive(n_groups):
    chart = column_chart(_groups(*[(1, 1)] * n_groups), ["Granted", "Denied"])
    assert 0 < chart["bar_width"] <= 24


def test_chart_bars_stay_inside_the_plot_area():
    chart = column_chart(_groups(*[(8, 4)] * 7), ["Granted", "Denied"])
    for bar in chart["bars"]:
        for x in re.findall(r"[MHQV ]([\d.]+),", bar["path"]):
            assert chart["left"] <= float(x) <= chart["right"]
        assert " " in bar["path"] and bar["path"].endswith("Z")


def test_empty_chart_reports_no_data():
    chart = column_chart(_groups((0, 0), (0, 0)), ["Granted", "Denied"])
    assert chart["total"] == 0 and chart["bars"] == []


# ---------- chart on the dashboards ----------

def test_week_chart_counts_attempts_per_local_day(app, client, users):
    now = utc_now()
    with app.app_context():
        db = get_db()
        rows = [(now, "Granted"), (now, "Granted"), (now, "Denied"), (now - timedelta(days=2), "Granted"),
                (now - timedelta(days=20), "Granted")]            # last one is outside the 7-day window
        for when, result in rows:
            db.execute("INSERT INTO entry_log (attempted_at,method,result,door_status) VALUES (?,?,?,?)",
                       (to_text(when), "Face" if result == "Granted" else "None", result,
                        "Unlocked" if result == "Granted" else "Locked"))
        db.commit()
    login(client, "admin@x.com")
    html = client.get("/admin/").get_data(as_text=True)
    assert "No entry activity in the last 7 days" not in html
    assert "2 granted" in html and "1 denied" in html and "1 granted" in html
    assert html.count('class="bar s0"') == 2 and html.count('class="bar s1"') == 1
    assert "visually-hidden" in html                          # table version for screen readers


def test_week_chart_empty_state(client, users):
    login(client, "warden@x.com")
    assert b"No entry activity in the last 7 days" in client.get("/warden/").data


def test_attendance_meter_shows_counts(client, users):
    login(client, "admin@x.com")
    html = client.get("/admin/").get_data(as_text=True)
    assert "meter-seg seg-notyet" in html                     # 2 students, nobody has entered
    assert re.search(r'key-num">2</span>\s*Not yet entered', html)
