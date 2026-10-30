#!/usr/bin/env python3
"""Tests des règles du desk, sans réseau."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import desk  # noqa: E402

UTC = timezone.utc
PARIS = desk.PARIS


def test_orderflow_thresholds():
    neutre = desk.orderflow_signal(54, 46)  # 54 % → écart 8
    assert neutre["signal"] == "NEUTRE" and neutre["conviction"] == "BASSE" and neutre["gap"] == 8
    edge = desk.orderflow_signal(55, 45)  # écart 10
    assert edge["signal"] == "ACHETEUR" and edge["conviction"] == "MODÉRÉE" and edge["gap"] == 10
    mid = desk.orderflow_signal(59, 41)  # écart 18
    assert mid["signal"] == "ACHETEUR" and mid["conviction"] == "MODÉRÉE"
    high = desk.orderflow_signal(60, 40)  # écart 20
    assert high["signal"] == "ACHETEUR" and high["conviction"] == "ÉLEVÉE" and high["gap"] == 20
    sell = desk.orderflow_signal(40, 60)
    assert sell["signal"] == "VENDEUR" and sell["conviction"] == "ÉLEVÉE"
    empty = desk.orderflow_signal(0, 0)
    assert empty["signal"] is None


def test_orderflow_from_ratio():
    # close au sommet → 100 % achat
    start = datetime(2026, 10, 1, tzinfo=UTC)
    candles = [
        {"t": start + timedelta(hours=h), "o": 90, "h": 110, "l": 90, "c": 110, "v": 10}
        for h in range(40)
    ]
    result = desk.compute_orderflow(candles)
    assert result["signal"] == "ACHETEUR"
    assert result["buy_pct"] == 100
    assert result["conviction"] == "ÉLEVÉE"
    assert result["complet"] is True
    assert abs(result["net_delta"] - 400) < 1e-6


def test_session_score_bearish_and_bounds():
    bear = desk.score_ohlc(100, 100.2, 99.4, 99.5, prev_high=101, prev_low=99.8)
    assert bear["biais"] == "baissière"
    assert bear["score"] == -4
    # ±0,15 % exact n'est pas encore directionnel ; 0,67 et 0,33 non plus
    flat = desk.score_ohlc(10000, 10020, 9980, 10015)  # +0,15 % pile
    assert flat["composantes"][0]["points"] == 0
    assert flat["variation_pct"] == 0.15
    middle = desk.score_ohlc(100, 101, 99, 100.10)  # +0,10 %, position centrale
    assert middle["composantes"][0]["points"] == 0
    assert middle["composantes"][2]["points"] == 0
    assert middle["biais"] == "neutre"
    on_67 = desk.score_ohlc(100, 100, 0, 67)
    assert on_67["position"] == 0.67 and on_67["composantes"][2]["points"] == 0
    just_above = desk.score_ohlc(100, 100, 0, 67.01)
    assert just_above["composantes"][2]["points"] == 1
    just_below = desk.score_ohlc(100, 100, 0, 32.9)
    assert just_below["composantes"][2]["points"] == -1
    at_33 = desk.score_ohlc(100, 100, 0, 33)
    assert at_33["composantes"][2]["points"] == 0
    bull = desk.score_ohlc(100, 101.2, 99.9, 101, prev_high=100.5, prev_low=99.5)
    assert bull["biais"] == "haussière" and bull["score"] == 4
    mixed = desk.score_ohlc(100, 101, 99, 100.5, prev_high=102, prev_low=98)
    assert mixed["structure"] == "mixte"
    assert mixed["composantes"][3]["points"] == 0


def test_rsi_extremes():
    up = list(range(1, 30))
    assert desk.rsi_wilder(up, 14) == 100
    down = list(range(30, 1, -1))
    assert desk.rsi_wilder(down, 14) == 0
    flat = [10.0] * 20
    assert desk.rsi_wilder(flat, 14) == 50
    assert desk.rsi_wilder([1, 2, 3], 14) is None
    assert desk.sma(list(range(10)), 20) is None
    assert desk.sma([1, 2, 3, 4], 4) == 2.5


def test_confidence_bounds():
    full = desk.confidence_score("VENDEUR", "ÉLEVÉE", 90, 100, 110, 30, "baissière", "BAISSIER", "Distribution")
    assert full["score"] == 100
    assert full["direction"] == "BAISSIER"
    assert full["label"] == "ÉLEVÉE"
    moderate = desk.confidence_score("ACHETEUR", "MODÉRÉE", 120, 100, 90, 60, "haussière", "HAUSSIER", "Accumulation")
    assert moderate["score"] == 88
    neutre = desk.confidence_score("NEUTRE", "BASSE", 100, 90, 80, 55, "haussière", "HAUSSIER", "Accumulation")
    assert neutre["score"] <= 40
    assert neutre["direction"] == "NEUTRE"
    missing = desk.confidence_score("ACHETEUR", "ÉLEVÉE", 100, None, None, None, None, None, None)
    assert missing["score"] == 30


def test_summary_title_and_slot():
    title = desk.summary_title("Londres", "baissière", "VENTE", "New York")
    assert title == "XAU/USD — Londres baissière → VENTE NY"
    assert desk.summary_title("New York", "haussière", "ACHAT", "Asie") == "XAU/USD — NY haussière → ACHAT Asie"
    assert desk.signal_title("NEUTRE") == "XAU/USD — signal NEUTRE"
    winter = datetime(2026, 10, 30, 16, 50, tzinfo=PARIS)
    slot = desk.matching_full_slot(winter)
    assert slot is not None and slot.hour == 16 and slot.minute == 55
    assert desk.matching_full_slot(datetime(2026, 10, 30, 17, 20, tzinfo=PARIS)) is None
    assert desk.matching_full_slot(datetime(2026, 10, 30, 21, 0, tzinfo=PARIS)).hour == 21
    assert desk.matching_full_slot(datetime(2026, 10, 30, 8, 55, tzinfo=PARIS)) is None
    # été : 06:55 Paris reste un créneau, quelle que soit l'heure UTC
    summer = datetime(2026, 7, 15, 6, 55, tzinfo=PARIS)
    assert summer.utcoffset() == timedelta(hours=2)
    assert desk.matching_full_slot(summer).hour == 6


def test_ntfy_click_header():
    raw = desk.build_ntfy_request(
        "sujet-test",
        "XAU/USD — Londres baissière → VENTE NY",
        "Corps\navec accent é",
        "https://geoffreygrandon-lang.github.io/xauusd-desk/",
        priority=3,
        tags="chart_with_downwards_trend",
    )
    text = raw.decode("utf-8")
    assert "Click: https://geoffreygrandon-lang.github.io/xauusd-desk/" in text
    assert "Title: XAU/USD — Londres baissière → VENTE NY" in text
    assert "Priority: 3" in text
    assert text.startswith("POST /sujet-test HTTP/1.1")
    try:
        desk.build_ntfy_request("x", "t", "m", "http://insecure.example")
        raise AssertionError("http doit être refusé")
    except ValueError:
        pass
    assert desk.topic_is_valid("xau-geo-7k2")
    assert not desk.topic_is_valid("a b")
    assert not desk.topic_is_valid("")


def _candle(moment, price, volume=2.0, span=1.0):
    return {
        "t": moment,
        "o": price,
        "h": price + span,
        "l": price - span,
        "c": price,
        "v": volume,
        "closed": True,
    }


def test_preferred_direction_follows_last_completed_session():
    # Vendredi 30 oct. 2026, 19:40 Paris (UTC+1) : Londres est finie, New York est en cours.
    now = datetime(2026, 10, 30, 19, 40, tzinfo=PARIS)
    candles = []
    moment = datetime(2026, 10, 28, 0, 0, tzinfo=UTC)
    while moment < datetime(2026, 10, 30, 7, 0, tzinfo=UTC):
        if desk.market_hour_open(moment):
            candles.append(_candle(moment, 3960))
        moment += timedelta(hours=1)
    # Londres 08:00–16:55 Paris = 07:00–15:55 UTC : baisse franche, sous l'Asie.
    london = datetime(2026, 10, 30, 7, 0, tzinfo=UTC)
    price = 3948
    while london <= datetime(2026, 10, 30, 15, 0, tzinfo=UTC):
        candles.append({
            "t": london,
            "o": price,
            "h": price + 0.4,
            "l": price - 6,
            "c": price - 5.5,
            "v": 3,
            "closed": True,
        })
        price -= 5.5
        london += timedelta(hours=1)
    # New York en cours, pas encore terminée.
    candles.append(_candle(datetime(2026, 10, 30, 16, 0, tzinfo=UTC), price, span=2))
    payload, sessions = desk.analyze(candles, {}, {}, now)
    assert payload["prefere"]["base_session"] == "Londres"
    assert payload["prefere"]["base_biais"] == "baissière"
    assert payload["prefere"]["direction"] == "VENTE"
    assert payload["prefere"]["phrase"] == "SENS PRIVILÉGIÉ POUR NEW YORK : VENTE"
    names = {item["nom"]: item["statut"] for item in payload["sessions"]["seances"]}
    assert names["Asie"] == "terminee"
    assert names["Londres"] == "terminee"
    assert names["New York"] == "en_cours"
    # 07:55 : l'Asie se termine à 08:00, la grâce de 15 min la compte comme finie.
    morning = datetime(2026, 10, 30, 7, 55, tzinfo=PARIS)
    last, nxt, imminent = desk.alert_session_pair(sessions, morning)
    assert last["nom"] == "Asie" and imminent is True
    assert nxt["nom"] == "Londres"
    title, _body, _tag = desk.summary_message(sessions, morning, payload["orderflow"], payload["price"])
    assert title.startswith("XAU/USD — Asie ")
    assert title.endswith("Londres")
    # 16:55 : Londres vient de se terminer → New York.
    close_london = datetime(2026, 10, 30, 16, 55, tzinfo=PARIS)
    title, body, _tag = desk.summary_message(sessions, close_london, {"signal": "VENDEUR", "conviction": "ÉLEVÉE", "buy_pct": 38, "sell_pct": 62}, payload["price"])
    assert title == "XAU/USD — Londres baissière → VENTE NY"
    assert "Orderflow : VENDEUR" in body
    assert "New York" in body
    # 21:00 : New York n'est pas fini (22:00), donc la séance terminée reste Londres.
    evening = datetime(2026, 10, 30, 21, 0, tzinfo=PARIS)
    title, _body, _tag = desk.summary_message(sessions, evening, payload["orderflow"], payload["price"])
    assert "Londres" in title and title.endswith("NY")
    # 06:55 : l'Asie n'est pas encore dans la grâce, la référence est New York de la veille.
    early = datetime(2026, 10, 30, 6, 55, tzinfo=PARIS)
    last, nxt, _imminent = desk.alert_session_pair(sessions, early)
    assert last["id"] == "ny"
    assert nxt["id"] == "asia"


def test_notification_plan_dedup_and_first_run():
    now = datetime(2026, 10, 30, 16, 55, tzinfo=PARIS)
    payload = {
        "orderflow": {"signal": "VENDEUR", "conviction": "ÉLEVÉE", "buy_pct": 38, "sell_pct": 62, "gap": 24},
        "price": {"last": 3900.0},
        "prefere": {"warnings": ["Zone neutre : éviter de trader"]},
    }
    sessions = []
    # Premier passage : on mémorise, on n'envoie pas de changement.
    notes, proposed = desk.plan_notifications(payload, sessions, {}, now, "full", True)
    assert all(note["id"] != "signal_change" for note in notes)
    assert proposed["signal"] == "VENDEUR"
    # Sans sujet : rien n'est prévu.
    notes, _proposed = desk.plan_notifications(payload, sessions, {"signal": "ACHETEUR"}, now, "full", False)
    assert notes == []
    # Changement de signal.
    notes, _proposed = desk.plan_notifications(payload, sessions, {"signal": "ACHETEUR"}, now, "orderflow", True)
    assert [note["id"] for note in notes] == ["signal_change"]
    assert notes[0]["title"] == "XAU/USD — signal VENDEUR"
    # Résumé de séance seulement en mode full, dans le créneau, une fois.
    notes, proposed = desk.plan_notifications(payload, sessions, {"signal": "VENDEUR"}, now, "full", True)
    assert [note["id"] for note in notes] == ["summary"]
    state = desk.apply_notification_state({"signal": "VENDEUR"}, proposed, notes, [(notes[0], True)])
    assert state["last_summary_slot"] == "2026-10-30 16:55"
    again, _proposed = desk.plan_notifications(payload, sessions, state, now, "full", True)
    assert again == []
    # Le contrôle horaire, même à 16:55, n'envoie pas le résumé.
    hourly, _proposed = desk.plan_notifications(payload, sessions, {"signal": "VENDEUR"}, now, "orderflow", True)
    assert hourly == []
    # Changement + résumé : si l'envoi échoue, on réessaiera les deux.
    both, proposed_both = desk.plan_notifications(payload, sessions, {"signal": "ACHETEUR"}, now, "full", True)
    assert [note["id"] for note in both] == ["signal_change", "summary"]
    failed = desk.apply_notification_state(
        {"signal": "ACHETEUR"},
        proposed_both,
        both,
        [(both[0], False), (both[1], False)],
    )
    assert failed.get("last_summary_slot") is None
    assert failed["signal"] == "ACHETEUR"
    partial = desk.apply_notification_state(
        {"signal": "ACHETEUR"},
        proposed_both,
        both,
        [(both[0], True), (both[1], False)],
    )
    assert partial["signal"] == "VENDEUR"
    assert partial.get("last_summary_slot") is None


def test_macro_never_invents_manual_impact():
    drivers = desk.drivers_from_sources(
        {"dff": {"value": 3.88, "date": "2026-10-28", "change": 0, "steps": 1}},
        {"derniere_mise_a_jour": None, "fed": {"impact": None, "libelle": "", "note": ""}},
    )
    fed = next(item for item in drivers if item["id"] == "fed")
    assert fed["impact"] is None
    assert "3,88" in fed["detail"]
    filled = desk.drivers_from_sources({}, {"fed": {"impact": "négatif", "libelle": "Fed restrictive", "note": ""}})
    fed = next(item for item in filled if item["id"] == "fed")
    assert fed["impact"] == "negatif"
    assert fed["etat"] == "Fed restrictive"
    bogus = desk.drivers_from_sources({}, {"geopolitique": {"impact": "peut-être", "libelle": "tensions"}})
    geo = next(item for item in bogus if item["id"] == "geo")
    assert geo["impact"] is None
    assert geo["etat"] == "tensions"


def test_heatmap_and_alignment():
    assert desk.heatmap_proxy(0.4, 10, 20) == "Accumulation"
    assert desk.heatmap_proxy(-0.4, -10, 20) == "Distribution"
    assert desk.heatmap_proxy(0.4, -10, 20) == "Divergence"
    assert desk.heatmap_proxy(0.0, 0.1, 2) == "Neutre"
    assert desk.alignment_label("HAUSSIER", "HAUSSIER", "HAUSSIER", "Accumulation") == "ALIGNEMENT COMPLET"
    assert desk.alignment_label("HAUSSIER", "HAUSSIER", "HAUSSIER", "Distribution") == "ALIGNEMENT PARTIEL"
    assert desk.alignment_label("HAUSSIER", "BAISSIER", "HAUSSIER", "Accumulation") == "DIVERGENCE"
    assert desk.alignment_label("NEUTRE", "NEUTRE", "NEUTRE", "Neutre") == "NEUTRE"


def test_brief_does_not_sign_an_absolute_percent():
    price = {"last": 3883.48, "change_24h": -139.0, "change_24h_pct": -3.46, "en_cours": False}
    orderflow = {"signal": "VENDEUR", "bougies": 40, "buy_pct": 39, "sell_pct": 61, "gap": 22, "conviction": "ÉLEVÉE", "net_delta": -21.19}
    preferred = {"base_session": "Londres", "base_biais": "baissière", "session": "New York", "direction": "VENTE", "warnings": []}
    text = desk.build_brief(price, orderflow, preferred, {"rsi14": 18.6, "vs_sma20": "en dessous", "tendance": "Baissière courte"}, {"biais": "HAUSSIER", "conviction": "MODÉRÉE", "drivers": []}, [])
    assert "en baisse de 3,46 %" in text
    assert "baisse de +" not in text
    assert "hausse de -" not in text


def test_dxy_rule_thresholds():
    cache = {
        "dxy": {"price": 101.5, "variation": -0.41, "span": 5, "as_of": "2026-10-30"},
        "vix": {"price": 22, "variation": 1.2, "span": 5, "as_of": "2026-10-30"},
        "dfii10": {"date": "2026-10-28", "value": 1.8, "change": -0.08, "steps": 5},
        "t10yie": {"date": "2026-10-29", "value": 2.3, "change": 0.08, "steps": 20},
    }
    drivers = desk.drivers_from_sources(cache, {})
    by_id = {item["id"]: item for item in drivers}
    assert by_id["dxy"]["impact"] == "positif"
    assert by_id["vix"]["impact"] == "positif"
    assert by_id["reels"]["impact"] == "positif"
    assert by_id["inflation"]["impact"] == "positif"
    biais, conviction = desk.macro_bias(drivers)
    assert biais == "HAUSSIER"
    assert conviction in ("HAUTE", "MODÉRÉE")
    # Variation inconnue → impact indisponible, pas un neutre inventé.
    unknown = desk.drivers_from_sources({"dxy": {"price": 100, "as_of": "2026-10-30"}}, {})
    dxy = next(item for item in unknown if item["id"] == "dxy")
    assert dxy["impact"] is None


if __name__ == "__main__":
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print("ok", test.__name__)
        except Exception as exc:
            failed += 1
            print("FAIL", test.__name__, type(exc).__name__, exc)
    if failed:
        raise SystemExit(1)
    print(f"{len(tests)} tests ok")
