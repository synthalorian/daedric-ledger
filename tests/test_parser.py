import time

import pytest

from ledger.db import Ledger
from ledger.items import Item, ItemDB
from ledger.parser import Side, parse_trade
from ledger.pricing import compute_price_report

# --- parser -----------------------------------------------------------------


@pytest.mark.parametrize("msg,side,item,price,qty", [
    ("wts daedric greatsword 2.5k", Side.SELL, "daedric greatsword", 2500, 1),
    ("WTS Daedric Greatsword 2500", Side.SELL, "Daedric Greatsword", 2500, 1),
    ("wtb 3x ebony ingot 150 each", Side.BUY, "ebony ingot", 150, 3),
    ("selling troll skull x10 50g ea", Side.SELL, "troll skull", 50, 10),
    ("buying grand soul gem 200 per", Side.BUY, "grand soul gem", 200, 1),
    ("wts iron sword 500 obo", Side.SELL, "iron sword", 500, 1),
    ("wts flawless ruby 1.2m", Side.SELL, "flawless ruby", 1_200_000, 1),
    ("WTS: ebony bow 800g", Side.SELL, "ebony bow", 800, 1),
    ("lf steel plate armor 300", Side.BUY, "steel plate armor", 300, 1),
])
def test_parse_valid(msg, side, item, price, qty):
    o = parse_trade(msg)
    assert o is not None, f"failed to parse: {msg}"
    assert o.side == side
    assert o.item_text.lower() == item.lower()
    assert o.price == price
    assert o.quantity == qty


@pytest.mark.parametrize("msg", [
    "anyone know where bleak falls barrow is?",
    "wts daedric greatsword pm me offers",
    "lol nice",
    "wtt iron sword for steel sword",
    "",
    "!price iron sword",
])
def test_parse_rejects(msg):
    assert parse_trade(msg) is None


def test_per_unit_unit_price():
    o = parse_trade("wtb 3x ebony ingot 150 each")
    assert o.per_unit and o.unit_price == 150
    o2 = parse_trade("wts troll skull x10 500")
    assert not o2.per_unit and o2.unit_price == 50


# --- item matching ----------------------------------------------------------


@pytest.fixture
def db():
    return ItemDB([
        Item(name="Daedric Greatsword", type="WEAP", value=2500, editor_id="DaedricGreatsword", plugin="Skyrim.esm"),
        Item(name="Daedric Sword", type="WEAP", value=1250),
        Item(name="Ebony Ingot", type="MISC", value=150),
        Item(name="Iron Sword", type="WEAP", value=25),
        Item(name="Steel Plate Armor", type="ARMO", value=625),
    ])


def test_exact_match(db):
    m = db.match("Daedric Greatsword")
    assert m.item and m.item.name == "Daedric Greatsword" and m.score == 100


def test_fuzzy_match(db):
    m = db.match("daedric greatsowrd")
    assert m.item and m.item.name == "Daedric Greatsword"


def test_alias_match(db):
    m = db.match("daedric gs")
    assert m.item and m.item.name == "Daedric Greatsword"


def test_no_match_returns_candidates(db):
    m = db.match("xyzzy blade of nothing")
    assert m.item is None


# --- pricing ----------------------------------------------------------------


def _row(side, price, ts):
    return {"side": side, "unit_price": price, "quantity": 1, "ts": ts, "author_name": "t", "item_text": "x"}


def test_weighted_recent_counts_more():
    now = time.time()
    rows = [_row("sell", 1000, now - 30 * 86400), _row("sell", 2000, now - 3600)]
    rep = compute_price_report("X", rows, half_life_days=14)
    assert rep.sell_avg > 1500  # pulled toward the recent 2000


def test_outlier_rejection():
    now = time.time()
    rows = [_row("sell", p, now - 3600) for p in [1000, 1100, 950, 1050, 1020, 50_000]]
    rep = compute_price_report("X", rows)
    assert rep.sell_avg < 2000
    assert rep.rejected_outliers == 1


def test_midpoint_with_both_sides():
    now = time.time()
    rows = [_row("sell", 1200, now), _row("buy", 800, now)]
    rep = compute_price_report("X", rows)
    assert rep.midpoint == pytest.approx(1000)


def test_thin_data_flagged():
    now = time.time()
    rows = [_row("sell", 1000, now)]
    rep = compute_price_report("X", rows)
    assert not rep.reliable


# --- ledger -----------------------------------------------------------------


def test_ledger_dedupes(tmp_path):
    led = Ledger(tmp_path / "t.db")
    offer = parse_trade("wts iron sword 500")
    kw = dict(message_id=123, guild_id=1, channel_id=2, author_id=3,
              author_name="synth", offer=offer, item_name="Iron Sword", match_score=100.0)
    assert led.record(**kw) is True
    assert led.record(**kw) is False  # duplicate snowflake rejected
    led.close()


def test_official_prices_and_audit(tmp_path):
    led = Ledger(tmp_path / "t.db")
    led.set_price(1, "Guild A", "Iron Sword", 10, 25, "admin", 42)
    led.set_price(1, "Guild A", "Iron Sword", 12, 30, "admin", 42)
    led.set_price(2, "Guild B", "Iron Sword", 18, 40, "other", 7)

    avg = led.average_price("Iron Sword")
    assert avg["avg_buy"] == pytest.approx(15)   # (12 + 18) / 2 — one vote per server
    assert avg["avg_sell"] == pytest.approx(35)
    assert avg["n_servers"] == 2
    assert len(avg["servers"]) == 2

    # guild A only sees its own row
    rows = led.get_prices("Iron Sword")
    assert len(rows) == 2
    hist_a = led.price_history_for("Iron Sword", guild_id=1)
    assert len(hist_a) == 2 and hist_a[0]["buy"] == 12  # newest first, guild-scoped
    hist_all = led.price_history_for("Iron Sword")
    assert len(hist_all) == 3

    # removal is guild-scoped
    assert led.remove_price(1, "Iron Sword") is True
    assert led.average_price("Iron Sword")["n_servers"] == 1
    assert led.remove_price(1, "Iron Sword") is False

    # board aggregation
    led.set_price(1, "Guild A", "Ebony Ingot", 200, 392, "admin", 42)
    board = led.board_prices()
    names = {r["item_name"] for r in board}
    assert "Ebony Ingot" in names and "Iron Sword" in names

    # kv store
    led.kv_set("board_message_id", "999")
    assert led.kv_get("board_message_id") == "999"
    led.close()
