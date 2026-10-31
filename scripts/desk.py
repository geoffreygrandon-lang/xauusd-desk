#!/usr/bin/env python3
"""Tableau de bord XAU/USD : données Dukascopy, macro, séances, alertes ntfy.

Aucune dépendance hors de la bibliothèque standard. Le script écrit data/latest.json.
"""

from __future__ import annotations

import argparse
import json
import lzma
import os
import re
import socket
import ssl
import struct
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")
UTC = timezone.utc

DEFAULT_PAGES_URL = "https://geoffreygrandon-lang.github.io/xauusd-desk/"
USER_AGENT = "Mozilla/5.0 (compatible; MacroDesk/1.0; +https://github.com/geoffreygrandon-lang/xauusd-desk)"
FULL_SLOTS = ((6, 40), (7, 40), (13, 40), (16, 40), (20, 45))
SLOT_WINDOW_MIN = 20
SESSION_GRACE = timedelta(minutes=15)

SESSION_DEFS = (
    ("asia", "Asie", "23:00 – 09:00"),
    ("london", "Londres", "09:00 – 15:30"),
    ("ny", "New York", "14:30 – 22:00"),
)
DAY_CLOSE = (22, 0)
NEXT_SESSION = {"asia": "london", "london": "ny", "ny": "asia"}

JOURS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
MOIS = (
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
)


# ---------------------------------------------------------------------------
# Formatage
# ---------------------------------------------------------------------------

def fr_num(value, nd=2, signed=False):
    if value is None:
        return "indisponible"
    neg = value < 0
    text = f"{abs(value):,.{nd}f}".replace(",", " ").replace(".", ",")
    if neg:
        return "-" + text
    if signed and value > 0:
        return "+" + text
    return text


def fr_pct(value, nd=2):
    if value is None:
        return "indisponible"
    return fr_num(value, nd, signed=True) + " %"


def round2(value):
    """Arrondi commercial au centime (4021,515 → 4021,52)."""
    from decimal import Decimal, ROUND_HALF_UP
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def fr_price(value):
    if value is None:
        return "indisponible"
    return fr_num(value, 2) + " $"


def human_paris(dt):
    dt = dt.astimezone(PARIS)
    return f"{JOURS[dt.weekday()]} {dt.day} {MOIS[dt.month - 1]} {dt.year}, {dt:%H:%M}"


def iso_z(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def hour_key(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:00:00Z")


def parse_time(value):
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


# ---------------------------------------------------------------------------
# Règles pures (testées)
# ---------------------------------------------------------------------------

def score_ohlc(open_, high, low, close, prev_high=None, prev_low=None):
    """Quatre composantes de -1 à +1. <= -2 baissière, >= +2 haussière."""
    pct = round((close - open_) / open_ * 100, 4) if open_ else 0.0
    p_var = 1 if pct > 0.15 else -1 if pct < -0.15 else 0
    p_close = 1 if close > open_ else -1 if close < open_ else 0
    if high > low:
        pos = round((close - low) / (high - low), 4)
    else:
        pos = 0.5
    p_pos = 1 if pos > 0.67 else -1 if pos < 0.33 else 0
    if prev_high is None or prev_low is None:
        p_struct = 0
        structure = "sans référence"
    elif high > prev_high and low > prev_low:
        p_struct = 1
        structure = "plus hauts"
    elif high < prev_high and low < prev_low:
        p_struct = -1
        structure = "plus bas"
    else:
        p_struct = 0
        structure = "mixte"
    total = p_var + p_close + p_pos + p_struct
    if total >= 2:
        biais = "haussière"
    elif total <= -2:
        biais = "baissière"
    else:
        biais = "neutre"
    parts = [
        {"nom": "Variation", "points": p_var, "detail": f"{fr_pct(pct)} (seuil ±0,15 %)"},
        {"nom": "Clôture vs ouverture", "points": p_close, "detail": f"{fr_price(close)} contre {fr_price(open_)}"},
        {"nom": "Position dans le range", "points": p_pos, "detail": f"{fr_num(pos * 100, 0)} % du range (seuils 33 % / 67 %)"},
        {"nom": "Structure", "points": p_struct, "detail": structure},
    ]
    return {
        "biais": biais,
        "score": total,
        "variation_pct": pct,
        "range": high - low,
        "position": pos,
        "structure": structure,
        "composantes": parts,
    }


def orderflow_signal(buy_volume, sell_volume):
    total = buy_volume + sell_volume
    if total <= 0:
        return {
            "signal": None,
            "conviction": None,
            "buy_pct": None,
            "sell_pct": None,
            "gap": None,
        }
    buy_pct = int(round(100 * buy_volume / total))
    sell_pct = 100 - buy_pct
    gap = abs(buy_pct - sell_pct)
    if gap < 10:
        signal, conviction = "NEUTRE", "BASSE"
    else:
        signal = "ACHETEUR" if buy_pct > sell_pct else "VENDEUR"
        conviction = "MODÉRÉE" if gap < 20 else "ÉLEVÉE"
    return {
        "signal": signal,
        "conviction": conviction,
        "buy_pct": buy_pct,
        "sell_pct": sell_pct,
        "gap": gap,
    }


def rsi_wilder(closes, period=14):
    if closes is None or len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas[:period]]
    losses = [max(-d, 0.0) for d in deltas[:period]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    for delta in deltas[period:]:
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_gain == 0 and avg_loss == 0:
        return 50.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def sma(values, length):
    if values is None or len(values) < length:
        return None
    window = values[-length:]
    return sum(window) / length


def direction_from_biais(biais):
    return {"haussière": "ACHAT", "baissière": "VENTE", "neutre": "ATTENDRE"}.get(biais, "ATTENDRE")


def session_short(name):
    return "NY" if name == "New York" else name


def summary_title(session_nom, biais, direction, next_nom):
    word = biais if biais in ("haussière", "baissière", "neutre") else "indisponible"
    direc = direction if direction in ("ACHAT", "VENTE", "ATTENDRE") else "ATTENDRE"
    return f"XAU/USD — {session_short(session_nom)} {word} → {direc} {session_short(next_nom)}"


def signal_title(signal):
    return f"XAU/USD — signal {signal}"


def matching_full_slot(now_paris, window_min=SLOT_WINDOW_MIN):
    """Créneau plein le plus proche si on est dans la fenêtre, sinon None."""
    best = None
    best_delta = None
    for hour, minute in FULL_SLOTS:
        target = now_paris.replace(hour=hour, minute=minute, second=0, microsecond=0)
        delta = abs((now_paris - target).total_seconds())
        if delta <= window_min * 60 and (best_delta is None or delta < best_delta):
            best = target
            best_delta = delta
    return best


def parse_impact(value):
    if value is None:
        return None
    key = str(value).strip().lower()
    mapping = {
        "positif": "positif",
        "positive": "positif",
        "haussier": "positif",
        "+": "positif",
        "negatif": "negatif",
        "négatif": "negatif",
        "negative": "negatif",
        "baissier": "negatif",
        "-": "negatif",
        "neutre": "neutre",
        "neutral": "neutre",
        "0": "neutre",
    }
    return mapping.get(key)


def heatmap_proxy(price_pct, net_delta, gap):
    if price_pct is None or net_delta is None or gap is None:
        return None
    delta_flat = gap < 5
    price_up = price_pct > 0.05
    price_down = price_pct < -0.05
    delta_up = (not delta_flat) and net_delta > 0
    delta_down = (not delta_flat) and net_delta < 0
    if price_up and delta_up:
        return "Accumulation"
    if price_down and delta_down:
        return "Distribution"
    if (price_up and delta_down) or (price_down and delta_up):
        return "Divergence"
    if (delta_up or delta_down) and not price_up and not price_down:
        return "Divergence"
    return "Neutre"


def alignment_label(macro, biais, orderflow, heatmap):
    trio = (macro, biais, orderflow)
    if macro == biais == orderflow and macro in ("HAUSSIER", "BAISSIER"):
        heat_ok = (
            (macro == "HAUSSIER" and heatmap == "Accumulation")
            or (macro == "BAISSIER" and heatmap == "Distribution")
        )
        return "ALIGNEMENT COMPLET" if heat_ok else "ALIGNEMENT PARTIEL"
    if all(item == "NEUTRE" for item in trio):
        return "NEUTRE"
    directional = [item for item in trio if item in ("HAUSSIER", "BAISSIER")]
    if len(set(directional)) > 1:
        return "DIVERGENCE"
    return "ALIGNEMENT PARTIEL"


def confidence_score(signal, conviction, price, sma20, sma100, rsi, session_biais, macro_biais, heatmap):
    parts = []
    if signal not in ("ACHETEUR", "VENDEUR"):
        parts.append({"label": "Orderflow neutre ou indisponible", "points": 10, "actif": True})
        parts.append({
            "label": "Plafond à 40 tant que l'orderflow n'a pas de sens",
            "points": 0,
            "actif": False,
        })
        return {"score": 10, "label": "FAIBLE", "direction": "NEUTRE", "parts": parts}

    bullish = signal == "ACHETEUR"
    direction = "HAUSSIER" if bullish else "BAISSIER"
    score = 0
    conv_points = 30 if conviction == "ÉLEVÉE" else 18 if conviction == "MODÉRÉE" else 0
    score += conv_points
    parts.append({"label": f"Conviction orderflow {conviction or 'indisponible'}", "points": conv_points, "actif": True})

    def add_ma(ma_value, name, points):
        nonlocal score
        if ma_value is None or price is None:
            parts.append({"label": f"{name} indisponible", "points": 0, "actif": False})
            return
        above = price > ma_value
        agrees = above if bullish else (not above)
        awarded = points if agrees else 0
        score += awarded
        where = "au-dessus" if above else "en dessous"
        parts.append({"label": f"Prix {where} de {name}", "points": awarded, "actif": agrees})

    add_ma(sma20, "M20", 15)
    add_ma(sma100, "M100", 10)

    if rsi is None:
        parts.append({"label": "RSI indisponible", "points": 0, "actif": False})
    else:
        agrees = (bullish and rsi >= 55) or ((not bullish) and rsi <= 45)
        awarded = 10 if agrees else 0
        score += awarded
        parts.append({"label": f"RSI {fr_num(rsi, 1)} {'confirme' if agrees else 'ne confirme pas'}", "points": awarded, "actif": agrees})

    mapped = {"haussière": "HAUSSIER", "baissière": "BAISSIER", "neutre": "NEUTRE"}.get(session_biais)
    if mapped is None:
        parts.append({"label": "Séance de référence indisponible", "points": 0, "actif": False})
    else:
        agrees = mapped == direction
        awarded = 15 if agrees else 0
        score += awarded
        parts.append({"label": f"Dernière séance {session_biais}", "points": awarded, "actif": agrees})

    if macro_biais not in ("HAUSSIER", "BAISSIER", "NEUTRE"):
        parts.append({"label": "Macro indisponible", "points": 0, "actif": False})
    else:
        agrees = macro_biais == direction
        awarded = 10 if agrees else 0
        score += awarded
        parts.append({"label": f"Biais macro {macro_biais.lower()}", "points": awarded, "actif": agrees})

    if heatmap is None:
        parts.append({"label": "Proxy flux indisponible", "points": 0, "actif": False})
    else:
        agrees = (bullish and heatmap == "Accumulation") or ((not bullish) and heatmap == "Distribution")
        awarded = 10 if agrees else 0
        score += awarded
        parts.append({"label": f"Proxy flux : {heatmap}", "points": awarded, "actif": agrees})

    score = max(0, min(100, int(round(score))))
    return {"score": score, "label": gauge_label(score), "direction": direction, "parts": parts}


def gauge_label(score):
    """Libellé de la jauge, distinct de la conviction macro."""
    if score >= 80:
        return "EXTRÊME"
    if score >= 50:
        return "FORTE"
    return "FAIBLE"


def banner_for(direction, conviction):
    titles = {
        "HAUSSIER": "HAUSSIER — pression acheteuse dominante",
        "BAISSIER": "BAISSIER — pression vendeuse dominante",
        "NEUTRE": "NEUTRE — pressions équilibrées",
    }
    return {
        "direction": direction if direction in titles else "NEUTRE",
        "titre": titles.get(direction, titles["NEUTRE"]),
        "conviction": conviction,
    }


def tag_for(word):
    if word in ("ACHAT", "HAUSSIER", "haussière", "ACHETEUR"):
        return "chart_with_upwards_trend"
    if word in ("VENTE", "BAISSIER", "baissière", "VENDEUR"):
        return "chart_with_downwards_trend"
    return "warning"


# ---------------------------------------------------------------------------
# Séances
# ---------------------------------------------------------------------------

def _paris_at(day, hour, minute=0):
    return datetime.combine(day, datetime.min.time(), tzinfo=PARIS).replace(hour=hour, minute=minute)


def previous_weekday(day):
    cursor = day - timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor -= timedelta(days=1)
    return cursor


def next_weekday(day):
    cursor = day + timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor += timedelta(days=1)
    return cursor


def trading_day_for(now_paris):
    """Journée qui commence à 23h00 la veille et se termine à 22h00.

    Le week-end, on reste sur le vendredi tant que le marché n'a pas rouvert
    (dimanche 23h00, heure de Paris).
    """
    now_paris = now_paris.astimezone(PARIS)
    if now_paris.weekday() == 5:
        return now_paris.date() - timedelta(days=1)
    if now_paris.weekday() == 6 and now_paris.hour < 23:
        return now_paris.date() - timedelta(days=2)
    if now_paris.hour >= 23:
        return (now_paris + timedelta(days=1)).date()
    return now_paris.date()


def day_bounds(day):
    """23h00 la veille → 22h00 le jour même (clôture, pause Dukascopy)."""
    return _paris_at(day - timedelta(days=1), 23, 0), _paris_at(day, 22, 0)


def week_start(day):
    """Dimanche d'ouverture de la semaine du jour de trading."""
    return day - timedelta(days=(day.weekday() + 1) % 7)


def session_bounds(day, session_id):
    if session_id == "asia":
        start = _paris_at(day - timedelta(days=1), 23, 0)
        end = _paris_at(day, 9, 0)
    elif session_id == "london":
        start = _paris_at(day, 9, 0)
        end = _paris_at(day, 15, 30)
    elif session_id == "ny":
        start = _paris_at(day, 14, 30)
        end = _paris_at(day, 22, 0)
    else:
        raise KeyError(session_id)
    return start, end


def candles_overlapping(candles, start, end):
    chosen = []
    for candle in candles:
        c0 = candle["t"].astimezone(PARIS)
        c1 = c0 + timedelta(hours=1)
        overlap = min(c1, end) - max(c0, start)
        if overlap >= timedelta(minutes=30):
            chosen.append(candle)
    chosen.sort(key=lambda item: item["t"])
    return chosen


def ohlc_of(candles):
    if not candles:
        return None
    return {
        "o": candles[0]["o"],
        "h": max(item["h"] for item in candles),
        "l": min(item["l"] for item in candles),
        "c": candles[-1]["c"],
    }


def session_status(now_paris, start, end):
    if now_paris < start:
        return "a_venir"
    if now_paris < end:
        return "en_cours"
    return "terminee"


def build_sessions(candles, now_paris):
    now_paris = now_paris.astimezone(PARIS)
    day = trading_day_for(now_paris)
    days = []
    cursor = previous_weekday(day)
    while cursor <= next_weekday(day):
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    sessions = []
    for current in days:
        for session_id, name, window in SESSION_DEFS:
            start, end = session_bounds(current, session_id)
            bars = candles_overlapping(candles, start, end)
            ohlc = ohlc_of(bars)
            sessions.append({
                "id": session_id,
                "nom": name,
                "fenetre": window,
                "jour": current.isoformat(),
                "start": start,
                "end": end,
                "statut": session_status(now_paris, start, end),
                "ohlc": ohlc,
                "bougies": len(bars),
            })
    index = {(item["jour"], item["id"]): item for item in sessions}
    for item in sessions:
        prev = _previous_session(index, item)
        if item["ohlc"] is None:
            item["score"] = None
            continue
        prev_h = prev["ohlc"]["h"] if prev and prev.get("ohlc") else None
        prev_l = prev["ohlc"]["l"] if prev and prev.get("ohlc") else None
        ohlc = item["ohlc"]
        item["score"] = score_ohlc(ohlc["o"], ohlc["h"], ohlc["l"], ohlc["c"], prev_h, prev_l)
    return sessions


def _previous_session(index, item):
    day = datetime.fromisoformat(item["jour"]).date()
    if item["id"] == "asia":
        key = (previous_weekday(day).isoformat(), "ny")
    elif item["id"] == "london":
        key = (item["jour"], "asia")
    else:
        key = (item["jour"], "london")
    return index.get(key)


def _next_session(index, item):
    day = datetime.fromisoformat(item["jour"]).date()
    nxt = NEXT_SESSION[item["id"]]
    if nxt == "asia":
        key = (next_weekday(day).isoformat(), "asia")
    else:
        key = (item["jour"], nxt)
    return index.get(key)


def public_session(item):
    score = item.get("score") or {}
    show_biais = item["statut"] == "terminee"
    return {
        "id": item["id"],
        "nom": item["nom"],
        "fenetre": item["fenetre"],
        "jour": item["jour"],
        "statut": item["statut"],
        "debut": item["start"].isoformat(),
        "fin": item["end"].isoformat(),
        "biais": score.get("biais") if show_biais else None,
        "biais_provisoire": score.get("biais") if item["statut"] == "en_cours" else None,
        "score": score.get("score") if show_biais else None,
        "variation_pct": score.get("variation_pct") if item["statut"] != "a_venir" else None,
        "range": score.get("range") if item["statut"] != "a_venir" else None,
        "structure": score.get("structure") if item["statut"] != "a_venir" and score else None,
        "composantes": score.get("composantes") if show_biais else [],
        "bougies": item["bougies"],
        "open": None if not item.get("ohlc") else item["ohlc"]["o"],
        "high": None if not item.get("ohlc") else item["ohlc"]["h"],
        "low": None if not item.get("ohlc") else item["ohlc"]["l"],
        "close": None if not item.get("ohlc") else item["ohlc"]["c"],
    }


def preferred_from_sessions(sessions, now_paris):
    now_paris = now_paris.astimezone(PARIS)
    index = {(item["jour"], item["id"]): item for item in sessions}
    done = [item for item in sessions if item["statut"] == "terminee" and item["end"] <= now_paris]
    if not done:
        return {
            "phrase": "SENS PRIVILÉGIÉ : INDISPONIBLE",
            "session": None,
            "direction": "ATTENDRE",
            "base_session": None,
            "base_biais": None,
            "warnings": [],
        }
    last = max(done, key=lambda item: item["end"])
    target = _next_session(index, last)
    biais = (last.get("score") or {}).get("biais")
    direction = direction_from_biais(biais) if biais else "ATTENDRE"
    if target is None:
        phrase = "SENS PRIVILÉGIÉ : INDISPONIBLE"
        target_name = None
    elif not biais:
        target_name = target["nom"]
        phrase = f"SENS PRIVILÉGIÉ POUR {target_name.upper()} : INDISPONIBLE"
    else:
        target_name = target["nom"]
        phrase = f"SENS PRIVILÉGIÉ POUR {target_name.upper()} : {direction}"
    return {
        "phrase": phrase,
        "session": target_name,
        "direction": direction if biais else "ATTENDRE",
        "base_session": last["nom"],
        "base_biais": biais,
        "warnings": [],
    }


def alert_session_pair(sessions, now_paris):
    """Séance terminée (ou à moins de 15 min de l'être) et la séance qui la suit."""
    now_paris = now_paris.astimezone(PARIS)
    index = {(item["jour"], item["id"]): item for item in sessions}
    candidates = [
        item for item in sessions
        if now_paris >= item["start"] and now_paris >= item["end"] - SESSION_GRACE
    ]
    if not candidates:
        return None, None, False
    last = max(candidates, key=lambda item: item["end"])
    return last, _next_session(index, last), now_paris < last["end"]


# ---------------------------------------------------------------------------
# Orderflow, technique, niveaux
# ---------------------------------------------------------------------------

def split_candle(candle):
    high, low, close, volume = candle["h"], candle["l"], candle["c"], candle["v"]
    ratio = ((close - low) / (high - low)) if high > low else 0.5
    buy = ratio * volume
    sell = volume - buy
    return buy, sell, buy - sell


def compute_orderflow(closed):
    window = closed[-40:]
    buys, sells, deltas = [], [], []
    for candle in window:
        buy, sell, delta = split_candle(candle)
        buys.append(buy)
        sells.append(sell)
        deltas.append(delta)
    stats = orderflow_signal(sum(buys), sum(sells)) if window else orderflow_signal(0, 0)
    cumulative = 0.0
    bars = []
    for candle, buy, sell, delta in zip(window, buys, sells, deltas):
        cumulative += delta
        local = candle["t"].astimezone(PARIS)
        bars.append({
            "t": iso_z(candle["t"]),
            "label": f"{local.day:02d}/{local.month:02d} {local.hour:02d}h",
            "buy": round(buy, 4),
            "sell": round(sell, 4),
            "delta": round(delta, 4),
            "cum_delta": round(cumulative, 4),
            "c": candle["c"],
        })
    net = round(sum(deltas), 4) if deltas else None
    price_pct = None
    if len(window) >= 2 and window[0]["o"]:
        price_pct = (window[-1]["c"] - window[0]["o"]) / window[0]["o"] * 100
    complet = len(window) >= 40 and stats["signal"] is not None
    # Moins de 40 bougies : on montre le graphe, pas un signal officiel.
    if not complet:
        stats = {**stats, "signal": None, "conviction": None}
    return {
        "signal": stats["signal"],
        "conviction": stats["conviction"],
        "buy_pct": stats["buy_pct"],
        "sell_pct": stats["sell_pct"],
        "gap": stats["gap"] if complet else None,
        "net_delta": net,
        "buy_volume": None if not buys else round(sum(buys), 4),
        "sell_volume": None if not sells else round(sum(sells), 4),
        "bougies": len(window),
        "complet": complet,
        "price_pct": price_pct if complet else None,
        "candles": bars,
        "heatmap": heatmap_proxy(price_pct, net, stats["gap"]) if complet else None,
    }


def technicals(closed, last_price):
    closes = [candle["c"] for candle in closed]
    sma20 = sma(closes, 20)
    sma100 = sma(closes, 100)
    rsi = rsi_wilder(closes, 14)
    if last_price is None or sma20 is None:
        trend = "indisponible"
        biais = "NEUTRE"
        vs20 = None
    else:
        vs20 = "au-dessus" if last_price > sma20 else "en dessous" if last_price < sma20 else "sur la moyenne"
        vs100 = None
        if sma100 is not None:
            vs100 = "au-dessus" if last_price > sma100 else "en dessous" if last_price < sma100 else "sur la moyenne"
        if sma100 is not None and last_price > sma20 > sma100:
            trend, biais = "Haussière", "HAUSSIER"
        elif sma100 is not None and last_price < sma20 < sma100:
            trend, biais = "Baissière", "BAISSIER"
        elif last_price > sma20 and (sma100 is None or last_price > sma100):
            trend, biais = "Haussière courte", "HAUSSIER"
        elif last_price < sma20 and (sma100 is None or last_price < sma100):
            trend, biais = "Baissière courte", "BAISSIER"
        else:
            trend, biais = "Neutre", "NEUTRE"
        return {
            "sma20": None if sma20 is None else round(sma20, 3),
            "sma100": None if sma100 is None else round(sma100, 3),
            "rsi14": None if rsi is None else round(rsi, 2),
            "vs_sma20": vs20,
            "vs_sma100": vs100,
            "tendance": trend,
            "biais": biais,
        }
    return {
        "sma20": None if sma20 is None else round(sma20, 3),
        "sma100": None if sma100 is None else round(sma100, 3),
        "rsi14": None if rsi is None else round(rsi, 2),
        "vs_sma20": None,
        "vs_sma100": None,
        "tendance": trend,
        "biais": biais,
    }


def round_levels(price):
    anchor = int(round(price / 10.0)) * 10
    levels = set(range(anchor - 30, anchor + 40, 10))
    fifty = int(price // 50) * 50
    hundred = int(price // 100) * 100
    levels.update((fifty, fifty + 50, hundred, hundred + 100))
    return sorted(level for level in levels if level > 0)


def level_kind(level):
    if level % 100 == 0:
        return "centaine"
    if level % 50 == 0:
        return "cinquantaine"
    return "dizaine"


def build_levels(closed, sessions_today, last_price):
    levels = []

    def add(name, value, kind="marche"):
        if value is None or last_price is None:
            return
        distance = value - last_price
        levels.append({
            "nom": name,
            "prix": round(value, 2),
            "distance": round(distance, 2),
            "distance_pct": round(distance / last_price * 100, 2) if last_price else None,
            "type": kind,
        })

    if closed:
        last24 = closed[-24:]
        add("Plus haut 24 h", max(item["h"] for item in last24))
        add("Plus bas 24 h", min(item["l"] for item in last24))
        by_day = {}
        for candle in closed:
            key = candle["t"].astimezone(PARIS).date().isoformat()
            by_day.setdefault(key, []).append(candle)
        days = sorted(by_day)
        if days:
            recent = days[-5:]
            subset = [candle for key in recent for candle in by_day[key]]
            label = "5 jours" if len(recent) >= 5 else f"{len(recent)} jour{'s' if len(recent) > 1 else ''}"
            add(f"Plus haut {label}", max(item["h"] for item in subset))
            add(f"Plus bas {label}", min(item["l"] for item in subset))
            recent20 = days[-20:]
            subset20 = [candle for key in recent20 for candle in by_day[key]]
            label20 = "20 jours" if len(recent20) >= 20 else f"{len(recent20)} jours disponibles"
            add(f"Plus haut {label20}", max(item["h"] for item in subset20))
            add(f"Plus bas {label20}", min(item["l"] for item in subset20))
    for item in sessions_today:
        if item.get("high") is not None and item["statut"] != "a_venir":
            add(f"{item['nom']} plus haut", item["high"], "seance")
            add(f"{item['nom']} plus bas", item["low"], "seance")
    if last_price is not None:
        for level in round_levels(last_price):
            add(f"Nombre rond {level_kind(level)}", float(level), level_kind(level))
    return levels


def market_status(now_paris, last_candle):
    now_utc = now_paris.astimezone(UTC)
    weekday = now_utc.weekday()
    hour = now_utc.hour
    if weekday == 5 or (weekday == 6 and hour < 22) or (weekday == 4 and hour >= 22):
        return {"ouvert": False, "libelle": "Marché fermé (week-end)"}
    if hour == 21:
        return {"ouvert": False, "libelle": "Pause quotidienne (vers 22:00–23:00 heure de Paris)"}
    if last_candle is not None:
        age = now_utc - last_candle["t"]
        if age > timedelta(hours=3):
            return {"ouvert": True, "libelle": "Marché censé être ouvert, données en retard"}
    return {"ouvert": True, "libelle": "Marché ouvert"}


def macro_bias(drivers):
    known = [item for item in drivers if item.get("impact") in ("positif", "negatif", "neutre")]
    positive = sum(1 for item in known if item["impact"] == "positif")
    negative = sum(1 for item in known if item["impact"] == "negatif")
    diff = positive - negative
    if len(known) < 2:
        return "NEUTRE", "INSUFFISANTE"
    if diff >= 2:
        biais = "HAUSSIER"
    elif diff <= -2:
        biais = "BAISSIER"
    else:
        biais = "NEUTRE"
    if abs(diff) >= 3:
        conviction = "HAUTE"
    elif abs(diff) >= 2:
        conviction = "MODÉRÉE"
    else:
        conviction = "FAIBLE"
    return biais, conviction


def build_brief(price, orderflow, preferred, tech, macro, levels):
    sentences = []
    if price and price.get("last") is not None:
        chunk = f"L'or spot XAU/USD cote {fr_price(price['last'])}"
        if price.get("change_24h_pct") is not None:
            sens = "en hausse" if price["change_24h"] > 0 else "en baisse" if price["change_24h"] < 0 else "stable"
            chunk += f", {sens} de {fr_num(abs(price['change_24h_pct']), 2)} % sur 24 h" if price["change_24h"] else ", stable sur 24 h"
        if price.get("en_cours"):
            chunk += " (dernière heure encore en cours)"
        sentences.append(chunk + ".")
    if orderflow and orderflow.get("signal"):
        sentences.append(
            f"L'orderflow estimé sur {orderflow['bougies']} bougies horaires est {orderflow['signal']} : "
            f"achat {orderflow['buy_pct']} % contre vente {orderflow['sell_pct']} %, "
            f"écart {orderflow['gap']} pts, conviction {orderflow['conviction']}, "
            f"delta net {fr_num(orderflow['net_delta'], 2, signed=True)}."
        )
    elif orderflow:
        sentences.append(
            f"L'orderflow n'est pas calculé : {orderflow.get('bougies', 0)} bougie(s) horaire(s) en mémoire, il en faut 40."
        )
    if preferred and preferred.get("base_session") and preferred.get("base_biais") and preferred.get("session"):
        sentences.append(
            f"La séance de {preferred['base_session']} est {preferred['base_biais']}. "
            f"Le sens privilégié pour {preferred['session']} est { _article_direction(preferred['direction']) }."
        )
    elif preferred and preferred.get("phrase"):
        sentences.append(preferred["phrase"].capitalize() + ".")
    if tech and tech.get("rsi14") is not None:
        bits = [f"Le RSI 14 est à {fr_num(tech['rsi14'], 1)}"]
        if tech.get("vs_sma20"):
            bits.append(f"le prix est {tech['vs_sma20']} de la moyenne 20")
        if tech.get("vs_sma100"):
            bits.append(f"{tech['vs_sma100']} de la moyenne 100")
        sentences.append(", ".join(bits) + f". Tendance : {tech.get('tendance', 'indisponible')}.")
    if macro and macro.get("biais"):
        known = [d for d in macro.get("drivers", []) if d.get("impact") in ("positif", "negatif", "neutre")]
        detail = ""
        if known:
            names = ", ".join(f"{d['nom']} { _impact_word(d['impact']) }" for d in known[:4])
            detail = f" ({names})"
        sentences.append(f"Le biais macro est {macro['biais'].lower()}, conviction {macro.get('conviction', 'indisponible').lower()}{detail}.")
    if preferred:
        for warning in preferred.get("warnings") or []:
            sentences.append(warning + ".")
    highs = [item for item in levels or [] if item["nom"].startswith("Plus haut 24")]
    lows = [item for item in levels or [] if item["nom"].startswith("Plus bas 24")]
    if highs and lows and price and price.get("last") is not None:
        sentences.append(
            f"Sur 24 h, le plus haut est {fr_price(highs[0]['prix'])} et le plus bas {fr_price(lows[0]['prix'])}."
        )
    return " ".join(sentences) if sentences else "Pas assez de données pour rédiger le brief."


def _article_direction(direction):
    return {"ACHAT": "l'ACHAT", "VENTE": "la VENTE", "ATTENDRE": "d'ATTENDRE"}.get(direction, direction)


def _impact_word(impact):
    return {"positif": "positif", "negatif": "négatif", "neutre": "neutre"}.get(impact, "indisponible")


def build_alerts(orderflow, tech, macro, preferred, market, manual_note):
    alerts = []

    def add(level, text):
        alerts.append({"niveau": level, "texte": text})

    if not market.get("ouvert"):
        add("info", market.get("libelle") or "Marché fermé.")
    elif "retard" in (market.get("libelle") or ""):
        add("attention", market["libelle"] + ".")
    if orderflow and orderflow.get("signal") == "NEUTRE":
        add("important", "Orderflow NEUTRE : zone neutre, éviter de trader.")
    elif orderflow and orderflow.get("signal"):
        add("info", f"Orderflow {orderflow['signal']}, conviction {orderflow['conviction']}, écart {orderflow['gap']} pts.")
    elif orderflow and not orderflow.get("complet"):
        add("attention", "Historique Dukascopy incomplet : le signal orderflow n'est pas encore affiché.")
    if tech and tech.get("rsi14") is not None:
        if tech["rsi14"] < 30:
            add("important", f"RSI 14 en survente ({fr_num(tech['rsi14'], 1)} < 30).")
        elif tech["rsi14"] > 70:
            add("important", f"RSI 14 en surachat ({fr_num(tech['rsi14'], 1)} > 70).")
    if preferred and preferred.get("base_biais") and orderflow and orderflow.get("signal") in ("ACHETEUR", "VENDEUR"):
        want = "ACHETEUR" if preferred["base_biais"] == "haussière" else "VENDEUR" if preferred["base_biais"] == "baissière" else None
        if want and want != orderflow["signal"]:
            add("attention", "La séance de référence et l'orderflow ne vont pas dans le même sens.")
    if macro and orderflow and orderflow.get("signal") in ("ACHETEUR", "VENDEUR"):
        of_dir = "HAUSSIER" if orderflow["signal"] == "ACHETEUR" else "BAISSIER"
        if macro.get("biais") in ("HAUSSIER", "BAISSIER") and macro["biais"] != of_dir:
            add("attention", "Divergence entre le biais macro et l'orderflow.")
    for driver in (macro or {}).get("drivers", []):
        if driver["id"] == "vix" and driver.get("valeur") is not None and driver["valeur"] >= 25:
            add("important", f"VIX élevé ({fr_num(driver['valeur'], 1)}) : aversion au risque.")
        if driver["id"] == "dxy" and driver.get("variation") is not None and abs(driver["variation"]) >= 0.5:
            add("attention", f"DXY en mouvement de {fr_pct(driver['variation'])} sur 5 séances.")
        if driver["id"] == "reels" and driver.get("variation") is not None and abs(driver["variation"]) >= 0.10:
            add("attention", f"Taux réels US 10 ans : variation de {fr_num(driver['variation'] * 100, 0, signed=True)} points de base.")
    if manual_note:
        add(manual_note[0], manual_note[1])
    if not alerts:
        add("info", "Aucune alerte prioritaire.")
    return alerts


# ---------------------------------------------------------------------------
# Macro : Yahoo, FRED, fichier manuel
# ---------------------------------------------------------------------------

def _request(url, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def fetch_json(url):
    try:
        return json.loads(_request(url).decode("utf-8"))
    except Exception as exc:
        print(f"  échec JSON {url.split('?')[0][-48:]} : {type(exc).__name__}")
        return None


def fetch_text(url):
    try:
        return _request(url).decode("utf-8", "replace")
    except Exception as exc:
        print(f"  échec texte {url[-48:]} : {type(exc).__name__}")
        return None


def yahoo_daily(symbol):
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(symbol)
        + "?range=1mo&interval=1d"
    )
    payload = fetch_json(url)
    try:
        result = payload["chart"]["result"][0]
        quote = result["indicators"]["quote"][0]
        closes = []
        for stamp, close in zip(result.get("timestamp") or [], quote.get("close") or []):
            if close is not None:
                closes.append({"t": int(stamp), "c": float(close)})
        price = result.get("meta", {}).get("regularMarketPrice")
        return {"price": None if price is None else float(price), "closes": closes}
    except Exception:
        return None


def change_over(closes, sessions=5):
    if not closes or len(closes) < 2:
        return None, 0
    if len(closes) < sessions + 1:
        old = closes[0]["c"]
        new = closes[-1]["c"]
        span = len(closes) - 1
    else:
        old = closes[-(sessions + 1)]["c"]
        new = closes[-1]["c"]
        span = sessions
    if not old:
        return None, 0
    return (new - old) / old * 100, span


def fred_series(series_id):
    text = fetch_text(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}")
    if not text:
        return None
    rows = []
    for line in text.splitlines()[1:]:
        if "," not in line:
            continue
        date, raw = line.split(",", 1)
        raw = raw.strip()
        if not raw or raw == ".":
            continue
        try:
            rows.append({"date": date, "value": float(raw)})
        except ValueError:
            continue
    return rows or None


def _delta(rows, steps):
    if not rows or len(rows) <= steps:
        return None
    return rows[-1]["value"] - rows[-(steps + 1)]["value"]


def _obs_change(rows, steps):
    if not rows:
        return None
    last = rows[-1]
    return {
        "date": last["date"],
        "value": last["value"],
        "change": _delta(rows, steps),
        "change_jour": _delta(rows, 1),
        "change_mois": _delta(rows, 21),
        "steps": steps if len(rows) > steps else 0,
    }


def cpi_yoy(rows):
    if not rows or len(rows) < 14:
        return None
    latest, year_ago = rows[-1], rows[-13]
    previous, previous_year = rows[-2], rows[-14]
    if not year_ago["value"] or not previous_year["value"]:
        return None
    yoy = (latest["value"] / year_ago["value"] - 1) * 100
    yoy_prev = (previous["value"] / previous_year["value"] - 1) * 100
    return {"date": latest["date"], "value": yoy, "change": yoy - yoy_prev}


def refresh_macro_cache(cache):
    cache = dict(cache or {})
    dxy = yahoo_daily("DX-Y.NYB")
    if dxy and dxy["closes"]:
        change, span = change_over(dxy["closes"], 5)
        jour, _ = change_over(dxy["closes"], 1)
        mois, _ = change_over(dxy["closes"], 21)
        cache["dxy"] = {
            "price": dxy["price"] if dxy["price"] is not None else dxy["closes"][-1]["c"],
            "variation": change,
            "change_jour": jour,
            "change_mois": mois,
            "span": span,
            "as_of": datetime.fromtimestamp(dxy["closes"][-1]["t"], UTC).date().isoformat(),
        }
        print(f"  DXY {cache['dxy']['price']}")
    vix = yahoo_daily("^VIX")
    if vix and vix["closes"]:
        change, span = change_over(vix["closes"], 5)
        jour, _ = change_over(vix["closes"], 1)
        mois, _ = change_over(vix["closes"], 21)
        cache["vix"] = {
            "price": vix["price"] if vix["price"] is not None else vix["closes"][-1]["c"],
            "variation": change,
            "change_jour": jour,
            "change_mois": mois,
            "span": span,
            "as_of": datetime.fromtimestamp(vix["closes"][-1]["t"], UTC).date().isoformat(),
        }
        print(f"  VIX {cache['vix']['price']}")
    tnx = yahoo_daily("^TNX")
    if tnx and (tnx["price"] is not None or tnx["closes"]):
        raw = tnx["price"] if tnx["price"] is not None else tnx["closes"][-1]["c"]
        yield_pct = raw / 10.0 if raw > 20 else raw
        as_of = None
        if tnx["closes"]:
            as_of = datetime.fromtimestamp(tnx["closes"][-1]["t"], UTC).date().isoformat()
        cache["tnx"] = {"price": yield_pct, "as_of": as_of}
        print(f"  TNX {yield_pct}")
    for series_id, steps, key in (("DFII10", 5, "dfii10"), ("T10YIE", 20, "t10yie"), ("DFF", 1, "dff")):
        rows = fred_series(series_id)
        if rows:
            cache[key] = _obs_change(rows, steps)
            print(f"  {series_id} {cache[key]['value']} ({cache[key]['date']})")
    if "t10yie" not in cache or cache.get("t10yie", {}).get("value") is None:
        cpi = fred_series("CPIAUCSL")
        parsed = cpi_yoy(cpi) if cpi else None
        if parsed:
            cache["cpi"] = parsed
            print(f"  CPI YoY {parsed['value']:.2f}")
    return cache


def _inflation_score(change, threshold):
    if change is None:
        return None
    return 1 if change > threshold else 0


def _signed_score(change, threshold, invert=False):
    if change is None:
        return None
    if abs(change) <= threshold:
        return 0
    sign = 1 if change > 0 else -1
    return -sign if invert else sign


def _vix_score(level, change):
    if level is None or change is None:
        return None
    if level >= 20 and change > 0:
        return 1
    if level <= 16 and change < 0:
        return -1
    return 0


def _impact_from_scores(jour, mois, fallback):
    known = [score for score in (jour, mois) if score is not None]
    if not known:
        return fallback
    total = sum(known)
    if total > 0:
        return "positif"
    if total < 0:
        return "negatif"
    return "neutre"


def _driver(driver_id, name, state, detail, impact, source, as_of, rule, value=None, variation=None, score_jour=None, score_mois=None):
    return {
        "id": driver_id,
        "nom": name,
        "etat": state,
        "detail": detail,
        "impact": _impact_from_scores(score_jour, score_mois, impact),
        "score_jour": score_jour,
        "score_mois": score_mois,
        "source": source,
        "as_of": as_of,
        "regle": rule,
        "valeur": value,
        "variation": variation,
    }


def drivers_from_sources(cache, manual):
    cache = cache or {}
    drivers = []

    dxy = cache.get("dxy") or {}
    dxy_rule = "Jour et mois : mouvement du DXY inférieur à 0,25 % = 0. Au-delà, baisse = +1 pour l'or, hausse = −1. L'impact est le signe de la somme."
    if dxy.get("price") is None or (dxy.get("variation") is None and dxy.get("change_jour") is None):
        drivers.append(_driver(
            "dxy", "USD (DXY)", "indisponible", "Cours ou variation indisponible",
            None, "Yahoo Finance DX-Y.NYB", dxy.get("as_of"), dxy_rule,
        ))
    else:
        change = dxy.get("variation")
        jour_change = dxy["change_jour"] if "change_jour" in dxy else change
        if change is None:
            state, impact = "Niveau seul", None
        elif change < -0.25:
            state, impact = "En repli", "positif"
        elif change > 0.25:
            state, impact = "En hausse", "negatif"
        else:
            state, impact = "Stable", "neutre"
        detail = fr_num(dxy["price"], 2)
        if change is not None:
            detail += f" · {fr_pct(change)} / {dxy.get('span') or 5} j"
        drivers.append(_driver(
            "dxy", "USD (DXY)", state, detail,
            impact, "Yahoo Finance DX-Y.NYB", dxy.get("as_of"), dxy_rule,
            value=dxy["price"], variation=change,
            score_jour=_signed_score(jour_change, 0.25, invert=True),
            score_mois=_signed_score(dxy.get("change_mois"), 0.25, invert=True),
        ))

    vix = cache.get("vix") or {}
    if vix.get("price") is None:
        drivers.append(_driver(
            "vix", "Sentiment de risque", "indisponible", "VIX indisponible",
            None, "Yahoo Finance ^VIX", None,
            "VIX ≥ 20 et en hausse sur 5 séances → risk off → positif. VIX ≤ 16 et en baisse → risk on → négatif. Sinon neutre.",
        ))
    else:
        change = vix.get("variation")
        level = vix["price"]
        if change is None:
            state, impact = "Niveau seul", None
        elif level >= 20 and change > 0:
            state, impact = "Aversion (risk off)", "positif"
        elif level <= 16 and change < 0:
            state, impact = "Appétit (risk on)", "negatif"
        else:
            state, impact = "Stable", "neutre"
        detail = fr_num(level, 2)
        if change is not None:
            detail += f" · {fr_pct(change)} / {vix.get('span') or 5} j"
        jour_change = vix["change_jour"] if "change_jour" in vix else change
        drivers.append(_driver(
            "vix", "Sentiment de risque", state, detail, impact, "Yahoo Finance ^VIX", vix.get("as_of"),
            "Jour et mois : VIX ≥ 20 et en hausse → +1 (risk off, positif pour l'or). VIX ≤ 16 et en baisse → −1. Sinon 0. L'impact est le signe de la somme.",
            value=level, variation=change,
            score_jour=_vix_score(level, jour_change),
            score_mois=_vix_score(level, vix.get("change_mois")),
        ))

    real = cache.get("dfii10") or {}
    nominal = cache.get("tnx") or {}
    if real.get("value") is None:
        drivers.append(_driver(
            "reels", "Taux réels US 10 ans", "indisponible", "DFII10 indisponible",
            None, "FRED DFII10", None,
            "Baisse > 5 points de base sur 5 observations → positif. Hausse > 5 pb → négatif. Sinon neutre.",
        ))
    else:
        change = real.get("change")
        nominal_bit = f" · nominal {fr_num(nominal['price'], 2)} %" if nominal.get("price") is not None else ""
        if change is None:
            state, impact = "Niveau seul", None
            move = ""
        elif change < -0.05:
            state, impact = "En repli", "positif"
            move = f" · {fr_num(change * 100, 0, signed=True)} pb"
        elif change > 0.05:
            state, impact = "En hausse", "negatif"
            move = f" · {fr_num(change * 100, 0, signed=True)} pb"
        else:
            state, impact = "Stable", "neutre"
            move = f" · {fr_num(change * 100, 0, signed=True)} pb"
        jour_change = real["change_jour"] if "change_jour" in real else change
        drivers.append(_driver(
            "reels", "Taux réels US 10 ans", state,
            f"{fr_num(real['value'], 2)} %{move}{nominal_bit}",
            impact, "FRED DFII10" + (" + Yahoo ^TNX" if nominal.get("price") is not None else ""),
            real.get("date"),
            "Jour : ±5 pb (baisse des taux réels = +1). Mois : ±10 pb. L'impact est le signe de la somme.",
            value=real["value"], variation=change,
            score_jour=_signed_score(jour_change, 0.05, invert=True),
            score_mois=_signed_score(real.get("change_mois"), 0.10, invert=True),
        ))

    fed_rate = (cache.get("dff") or {}).get("value")
    fed_date = (cache.get("dff") or {}).get("date")
    drivers.append(_manual_driver(
        "fed", "Fed", manual.get("fed") if isinstance(manual, dict) else None,
        rate_bit=(f"Fed funds {fr_num(fed_rate, 2)} %" if fed_rate is not None else ""),
        rate_source="FRED DFF" if fed_rate is not None else "",
        rate_date=fed_date,
        rule="L'impact (positif, neutre, négatif) vient uniquement de data/macro.json. Le taux affiché est le Fed funds effectif (FRED), sans déduction automatique.",
    ))

    breakeven = cache.get("t10yie") or {}
    if breakeven.get("value") is not None:
        change = breakeven.get("change")
        if change is None:
            state, impact = "Niveau seul", None
            move = ""
        elif change > 0.05:
            state, impact = "Anticipations en hausse", "positif"
            move = f" · {fr_num(change * 100, 0, signed=True)} pb"
        elif change < -0.05:
            state, impact = "En repli", "neutre"
            move = f" · {fr_num(change * 100, 0, signed=True)} pb"
        else:
            state, impact = "Stables", "neutre"
            move = f" · {fr_num(change * 100, 0, signed=True)} pb"
        jour_change = breakeven["change_jour"] if "change_jour" in breakeven else change
        drivers.append(_driver(
            "inflation", "Inflation", state,
            f"Point mort 10 ans {fr_num(breakeven['value'], 2)} %{move}",
            impact, "FRED T10YIE", breakeven.get("date"),
            "Jour : hausse du point mort > 5 pb → +1, sinon 0 (un repli reste neutre). Mois : seuil 10 pb. L'impact est le signe de la somme.",
            value=breakeven["value"], variation=change,
            score_jour=_inflation_score(jour_change, 0.05),
            score_mois=_inflation_score(breakeven.get("change_mois"), 0.10),
        ))
    elif cache.get("cpi"):
        cpi = cache["cpi"]
        change = cpi.get("change")
        if change is None:
            state, impact = "Niveau seul", None
        elif change > 0.1:
            state, impact = "CPI en accélération", "positif"
        elif change < -0.1:
            state, impact = "CPI en repli", "neutre"
        else:
            state, impact = "CPI stable", "neutre"
        drivers.append(_driver(
            "inflation", "Inflation", state,
            f"CPI US {fr_num(cpi['value'], 2)} % sur un an",
            impact, "FRED CPIAUCSL", cpi.get("date"),
            "Secours si le point mort est absent. Accélération du glissement annuel > 0,1 pt → positif. Ralentissement → neutre.",
            value=cpi["value"], variation=change,
        ))
    else:
        drivers.append(_driver(
            "inflation", "Inflation", "indisponible", "T10YIE et CPI indisponibles",
            None, "FRED", None,
            "Hausse du point mort 10 ans > 5 pb → positif. Baisse > 5 pb → neutre.",
        ))

    drivers.append(_manual_driver(
        "banques", "Banques centrales", manual.get("banques_centrales") if isinstance(manual, dict) else None,
        rule="Impact renseigné à la main dans data/macro.json (achats officiels, etc.). Rien n'est inventé.",
    ))
    drivers.append(_manual_driver(
        "geo", "Géopolitique", manual.get("geopolitique") if isinstance(manual, dict) else None,
        rule="Impact renseigné à la main dans data/macro.json. Rien n'est inventé.",
    ))
    return drivers


def _manual_driver(driver_id, name, block, rule, rate_bit="", rate_source="", rate_date=None):
    block = block or {}
    impact = parse_impact(block.get("impact"))
    label = (block.get("libelle") or "").strip()
    note = (block.get("note") or "").strip()
    if label:
        state = label
    elif impact:
        state = "Renseigné à la main"
    elif rate_bit:
        state = "Taux seul, impact non renseigné"
    else:
        state = "non renseigné"
    detail_parts = [part for part in (rate_bit, note) if part]
    source_parts = [part for part in ("data/macro.json", rate_source) if part]
    mapped = {"positif": 1, "negatif": -1, "neutre": 0}.get(impact)
    return _driver(
        driver_id, name, state, " · ".join(detail_parts) if detail_parts else "À renseigner dans data/macro.json",
        impact, " + ".join(source_parts), rate_date, rule,
        value=None, variation=None,
        score_jour=mapped, score_mois=mapped,
    )


def manual_freshness(manual, today):
    if not isinstance(manual, dict):
        return ("info", "Fed, banques centrales et géopolitique : à compléter dans data/macro.json.")
    impacts = [
        parse_impact((manual.get(key) or {}).get("impact"))
        for key in ("fed", "banques_centrales", "geopolitique")
    ]
    raw_date = manual.get("derniere_mise_a_jour")
    if all(item is None for item in impacts):
        return ("info", "Fed, banques centrales et géopolitique : impact non renseigné dans data/macro.json. Ils restent indisponibles.")
    if not raw_date:
        return ("attention", "data/macro.json a des impacts mais pas de date « derniere_mise_a_jour ». Ajoutez-la (AAAA-MM-JJ).")
    try:
        updated = datetime.strptime(str(raw_date), "%Y-%m-%d").date()
    except ValueError:
        return ("attention", "La date de data/macro.json n'est pas au format AAAA-MM-JJ.")
    age = (today - updated).days
    if age > 21:
        return ("attention", f"Les saisies manuelles datent du {raw_date} (plus de 21 jours). Pensez à les revoir.")
    return None


# ---------------------------------------------------------------------------
# Assemblage du JSON public
# ---------------------------------------------------------------------------

def closed_candles(candles, now_utc):
    return [candle for candle in candles if candle["t"] + timedelta(hours=1) <= now_utc]


def vwap_of(bars):
    numerator = denominator = 0.0
    for bar in bars:
        typical = (bar["h"] + bar["l"] + bar["c"]) / 3.0
        numerator += typical * bar["v"]
        denominator += bar["v"]
    if denominator <= 0:
        return None
    return numerator / denominator


def bars_between(bars, start, end):
    chosen = []
    for bar in bars:
        moment = bar["t"].astimezone(PARIS)
        if start <= moment < end:
            chosen.append(bar)
    chosen.sort(key=lambda item: item["t"])
    return chosen


def volume_profile(bars, price):
    """Profil $0,50 puis bacs de 5 $. X = part du volume traité au-dessus du prix."""
    if not bars or price is None:
        return None
    halves = {}
    total = 0.0
    above = 0.0
    for bar in bars:
        low, high, volume = bar["l"], bar["h"], bar["v"]
        if volume <= 0:
            continue
        total += volume
        if high <= price:
            pass
        elif low >= price:
            above += volume
        else:
            span = high - low
            above += volume * ((high - price) / span) if span else 0.0
        if high < low:
            low, high = high, low
        step = round(int(low / 0.5) * 0.5, 2)
        steps = []
        guard = 0
        while step <= high + 1e-9 and guard < 5000:
            steps.append(step)
            step = round(step + 0.5, 2)
            guard += 1
        if not steps:
            steps = [round(low, 2)]
        share = volume / len(steps)
        for level in steps:
            halves[level] = halves.get(level, 0.0) + share
    if total <= 0:
        return None
    bins = {}
    for level, volume in halves.items():
        start = int(level // 5) * 5
        bins[start] = bins.get(start, 0.0) + volume
    poc = max(bins, key=bins.get)
    ordered = sorted(bins.items(), key=lambda item: item[1], reverse=True)
    covered = 0.0
    chosen = []
    bin_total = sum(bins.values()) or 1.0
    for start, volume in ordered:
        chosen.append(start)
        covered += volume
        if covered >= 0.70 * bin_total:
            break
    x_pct = 100.0 * above / total
    if x_pct >= 65:
        state = "Distribution"
        direction = "BAISSIER"
    elif x_pct <= 35:
        state = "Accumulation"
        direction = "HAUSSIER"
    else:
        state = "Neutre"
        direction = "NEUTRE"
    published = []
    for start in sorted(bins):
        published.append({
            "bas": start,
            "haut": start + 5,
            "volume": round(bins[start], 4),
            "poc": start == poc,
            "va": start in chosen,
        })
    return {
        "poc_bas": poc,
        "poc_haut": poc + 5,
        "poc": round(poc + 2.5, 2),
        "va_bas": min(chosen),
        "va_haut": max(chosen) + 5,
        "x_pct": round(x_pct, 1),
        "etat": state,
        "direction": direction,
        "force": min(100, round(abs(x_pct - 50) * 2)),
        "bins": published,
    }


def _extreme(bars):
    if not bars:
        return None, None
    return max(bar["h"] for bar in bars), min(bar["l"] for bar in bars)


def merge_key_levels(candidates, price, gap=1.0):
    """Regroupe les niveaux à moins d'un point. Offre au-dessus, demande en dessous."""
    if price is None:
        return [], []
    ordered = sorted((float(level), label) for level, label in candidates if level is not None)
    groups = []
    for level, label in ordered:
        if groups and abs(level - groups[-1]["anchor"]) <= gap:
            group = groups[-1]
            group["prices"].append(level)
            if label not in group["labels"]:
                group["labels"].append(label)
            group["anchor"] = sum(group["prices"]) / len(group["prices"])
        else:
            groups.append({"anchor": level, "prices": [level], "labels": [label]})
    supply, demand = [], []
    for group in groups:
        level = group["anchor"]
        if abs(level - price) <= 0.05:
            continue
        row = {
            "prix": round(level, 2),
            "labels": group["labels"],
            "label": ", ".join(group["labels"]),
            "distance_pct": round((level - price) / price * 100, 2),
        }
        if level > price:
            supply.append(row)
        else:
            demand.append(row)
    supply.sort(key=lambda row: row["prix"])
    demand.sort(key=lambda row: row["prix"], reverse=True)
    return supply, demand


def build_desk_levels(price, day_bars, week_bars, prev_bars, profile, trading_day):
    if price is None or not day_bars:
        return None
    day_high, day_low = _extreme(day_bars)
    week_high, week_low = _extreme(week_bars)
    prev_high, prev_low = _extreme(prev_bars)
    vwap_day = vwap_of(day_bars)
    ny_start = _paris_at(trading_day, 14, 30)
    ny_bars = [bar for bar in day_bars if bar["t"].astimezone(PARIS) >= ny_start]
    vwap_ny = vwap_of(ny_bars)
    candidates = []

    def add(level, label):
        if level is not None:
            candidates.append((level, label))

    add(week_high, "Haut semaine")
    add(week_low, "Bas semaine")
    add(prev_high, "Haut veille")
    add(prev_low, "Bas veille")
    add(day_high, "Haut jour")
    add(day_low, "Bas jour")
    for session_id, name in (("asia", "Asie"), ("london", "Londres"), ("ny", "NY")):
        sess_start, sess_end = session_bounds(trading_day, session_id)
        chunk = bars_between(day_bars, sess_start, sess_end)
        high, low = _extreme(chunk)
        add(high, f"Haut {name}")
        add(low, f"Bas {name}")
    add(vwap_day, "VWAP jour")
    add(vwap_ny, "VWAP NY")
    if profile and profile.get("poc") is not None:
        add(profile["poc"], "POC")
    low_bound = price * 0.98
    high_bound = price * 1.02
    rung = int(low_bound // 25) * 25
    while rung <= high_bound + 1e-9:
        if rung >= low_bound:
            add(float(rung), f"Rond {int(rung)}")
        rung += 25
    supply, demand = merge_key_levels(candidates, price)
    return {
        "vwap_jour": None if vwap_day is None else round(vwap_day, 2),
        "vwap_ny": None if vwap_ny is None else round(vwap_ny, 2),
        "ouverture": round2(day_bars[0]["o"]),
        "offre": supply,
        "demande": demand,
        "prix": round(price, 2),
    }


def alignment_five(tiles):
    """Cinq tuiles. Total si les 5 vont ensemble, partiel dès 3, sinon aucun."""
    directions = [tile["direction"] for tile in tiles]
    bulls = sum(1 for direction in directions if direction == "HAUSSIER")
    bears = sum(1 for direction in directions if direction == "BAISSIER")
    if bulls == 5 or bears == 5:
        label = "ALIGNEMENT TOTAL"
    elif max(bulls, bears) >= 3:
        label = "ALIGNEMENT PARTIEL"
    else:
        label = "AUCUN"
    macro = next(tile for tile in tiles if tile["id"] == "macro")
    others = [tile for tile in tiles if tile["id"] != "macro"]
    technical = sum(tile["force"] for tile in others) / len(others)
    score = round(0.5 * macro["force"] + 0.5 * technical)
    score = max(0, min(100, score))
    if score >= 70:
        conviction = "ÉLEVÉE"
    elif score >= 50:
        conviction = "MODÉRÉE"
    else:
        conviction = "FAIBLE"
    if bulls > bears:
        direction = "HAUSSIER"
    elif bears > bulls:
        direction = "BAISSIER"
    else:
        direction = "NEUTRE"
    return {
        "tuiles": tiles,
        "label": label,
        "confiance": score,
        "conviction": conviction,
        "jauge": gauge_label(score),
        "direction": direction,
        "force_macro": macro["force"],
        "moyenne_technique": round(technical, 1),
    }


def macro_strength(drivers):
    scores = []
    for driver in drivers:
        for key in ("score_jour", "score_mois"):
            if driver.get(key) is not None:
                scores.append(driver[key])
    if not scores:
        return 0
    return min(100, round(abs(sum(scores)) / len(scores) * 100))


def build_alignment(macro, tech, orderflow, profile, levels, price):
    macro_dir = macro.get("biais") or "NEUTRE"
    if macro_dir not in ("HAUSSIER", "BAISSIER", "NEUTRE"):
        macro_dir = "NEUTRE"
    vwap_day = None if not levels else levels.get("vwap_jour")
    vwap_ny = None if not levels else levels.get("vwap_ny")
    if price is None or (vwap_day is None and vwap_ny is None):
        biais_dir, biais_force = "NEUTRE", 0
    else:
        votes = []
        if vwap_day is not None:
            votes.append(price > vwap_day)
        if vwap_ny is not None:
            votes.append(price > vwap_ny)
        if votes and all(votes):
            biais_dir = "HAUSSIER"
        elif votes and not any(votes):
            biais_dir = "BAISSIER"
        else:
            biais_dir = "NEUTRE"
        anchor = vwap_day if vwap_day is not None else vwap_ny
        distance = abs(price - anchor) / price * 100 if price else 0
        biais_force = min(100, round(distance / 2.0 * 100))
        if biais_dir == "NEUTRE":
            biais_force = min(biais_force, 25)
    signal = orderflow.get("signal")
    gap = orderflow.get("gap") or 0
    if signal == "ACHETEUR":
        mom_dir = "HAUSSIER"
    elif signal == "VENDEUR":
        mom_dir = "BAISSIER"
    else:
        mom_dir = "NEUTRE"
    mom_force = min(100, round(gap * 100 / 40)) if signal in ("ACHETEUR", "VENDEUR") else min(20, gap)
    trend = tech.get("biais") or "NEUTRE"
    if trend not in ("HAUSSIER", "BAISSIER", "NEUTRE"):
        trend = "NEUTRE"
    label = tech.get("tendance") or ""
    if trend == "NEUTRE":
        trend_force = 0
    elif "courte" in label:
        trend_force = 60
    else:
        trend_force = 100
    if profile:
        heat_dir = profile["direction"]
        heat_force = profile["force"]
        heat_state = profile["etat"]
        heat_x = profile["x_pct"]
    else:
        heat_dir, heat_force, heat_state, heat_x = "NEUTRE", 0, "Neutre", None
    tiles = [
        {"id": "macro", "nom": "Macro", "direction": macro_dir, "force": macro_strength(macro.get("drivers") or [])},
        {"id": "biais", "nom": "Biais", "direction": biais_dir, "force": biais_force},
        {"id": "momentum", "nom": "Momentum", "direction": mom_dir, "force": mom_force},
        {"id": "tendance", "nom": "Tendance", "direction": trend, "force": trend_force},
        {"id": "heatmap", "nom": "Heatmap", "direction": heat_dir, "force": heat_force, "etat": heat_state, "x_pct": heat_x},
    ]
    board = alignment_five(tiles)
    board["macro"] = macro_dir
    board["biais"] = biais_dir
    board["orderflow"] = mom_dir
    board["heatmap"] = heat_state
    board["tendance"] = trend
    return board


def build_gameplan(price, preferred, levels, profile, orderflow, rsi):
    if price is None or not levels:
        return "Le gameplan attend les bougies minute du jour. Tant qu'elles manquent, pas de zone d'entrée."
    direction = (preferred or {}).get("direction") or "ATTENDRE"
    supply = levels.get("offre") or []
    demand = levels.get("demande") or []
    nearest_supply = supply[0] if supply else None
    nearest_demand = demand[0] if demand else None

    def dist_sentence():
        bits = []
        if nearest_demand:
            bits.append(f"la demande la plus proche est à {fr_price(nearest_demand['prix'])} ({fr_pct(nearest_demand['distance_pct'])}, {nearest_demand['label']})")
        if nearest_supply:
            bits.append(f"l'offre la plus proche est à {fr_price(nearest_supply['prix'])} ({fr_pct(nearest_supply['distance_pct'])}, {nearest_supply['label']})")
        if not bits:
            return "Aucun niveau minute exploitable autour du prix."
        return "Autour du prix, " + " et ".join(bits) + "."

    va = ""
    if profile and profile.get("va_bas") is not None:
        va = (
            f" Zone de valeur {fr_price(profile['va_bas'])} – {fr_price(profile['va_haut'])}, "
            f"POC {fr_price(profile['poc_bas'])} – {fr_price(profile['poc_haut'])}."
        )
    paragraphs = [dist_sentence() + va]
    signal = (orderflow or {}).get("signal")
    if direction == "VENTE" and nearest_supply:
        targets = demand[:3]
        invalid = supply[1] if len(supply) > 1 else None
        target_txt = ", ".join(fr_price(row["prix"]) for row in targets) if targets else "les demandes sous le prix"
        invalid_txt = fr_price(invalid["prix"]) if invalid else "le prochain niveau d'offre au-dessus de la zone"
        paragraphs.append(
            f"Sens privilégié VENTE. Zone d'entrée : le premier paquet d'offre, {nearest_supply['label']} vers {fr_price(nearest_supply['prix'])}. "
            f"Confirmation : rejet du niveau, puis clôture horaire qui repasse sous la zone, avec un signal orderflow VENDEUR"
            f"{' (actuellement ' + signal + ')' if signal else ''}. "
            f"Objectifs : {target_txt}. Rester à plat si la clôture horaire revient dans la zone de valeur. "
            f"Invalidation : clôture horaire au-delà de {invalid_txt}."
        )
    elif direction == "ACHAT" and nearest_demand:
        targets = supply[:3]
        invalid = demand[1] if len(demand) > 1 else None
        target_txt = ", ".join(fr_price(row["prix"]) for row in targets) if targets else "les offres au-dessus du prix"
        invalid_txt = fr_price(invalid["prix"]) if invalid else "le prochain niveau de demande sous la zone"
        paragraphs.append(
            f"Sens privilégié ACHAT. Zone d'entrée : le premier paquet de demande, {nearest_demand['label']} vers {fr_price(nearest_demand['prix'])}. "
            f"Confirmation : rejet du niveau, puis clôture horaire qui repasse au-dessus de la zone, avec un signal orderflow ACHETEUR"
            f"{' (actuellement ' + signal + ')' if signal else ''}. "
            f"Objectifs : {target_txt}. Rester à plat si la clôture horaire revient dans la zone de valeur. "
            f"Invalidation : clôture horaire au-delà de {invalid_txt}."
        )
    else:
        paragraphs.append(
            "Pas de plan directionnel : le sens privilégié est d'attendre, ou la zone d'entrée n'est pas côté prix. "
            "On reste à plat tant qu'une clôture horaire ne sort pas de la zone de valeur avec un orderflow qui confirme."
        )
    caution = []
    if rsi is not None and rsi < 30:
        caution.append("RSI sous 30 : ne pas vendre le plus bas.")
    if rsi is not None and rsi > 70:
        caution.append("RSI au-dessus de 70 : ne pas acheter le plus haut.")
    if caution:
        paragraphs.append(" ".join(caution))
    return "\n\n".join(paragraphs)


def weekend_gap_warning(now_paris):
    now_paris = now_paris.astimezone(PARIS)
    if now_paris.weekday() == 4 and (now_paris.hour > 22 or (now_paris.hour == 22 and now_paris.minute >= 0)):
        return True
    if now_paris.weekday() == 5:
        return True
    if now_paris.weekday() == 6 and now_paris.hour < 23:
        return True
    return False


FF_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FED_RATE_TITLES = ("federal funds rate",)
FOREIGN_RATE_TITLES = {
    "EUR": ("main refinancing rate", "deposit facility rate"),
    "GBP": ("official bank rate",),
    "JPY": ("boj policy rate",),
    "CHF": ("snb policy rate", "policy rate"),
    "CAD": ("overnight rate",),
    "AUD": ("cash rate",),
    "NZD": ("official cash rate",),
    "CNY": ("loan prime rate",),
}
FOREIGN_EVENT_HINTS = {
    "EUR": ("monetary policy statement", "ecb press", "main refinancing", "deposit facility"),
    "GBP": ("official bank rate", "boe ", "mpc "),
    "JPY": ("boj ", "boj policy"),
    "CHF": ("snb",),
    "CAD": ("boc ", "overnight rate", "boc monetary"),
    "AUD": ("rba ", "cash rate"),
    "NZD": ("rbnz", "official cash rate"),
    "CNY": ("pboc", "loan prime"),
}
NON_ECONOMIC_HINTS = ("election", "referendum", "midterm", "inauguration", "ballot")


def _parse_rate(text):
    if text is None:
        return None
    match = re.search(r"-?\d+(?:[.,]\d+)?", str(text))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return None


def _event_moment(item):
    raw = item.get("date")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw)).astimezone(PARIS)
    except ValueError:
        return None


def _paris_short(moment):
    return f"{JOURS[moment.weekday()][:3]}. {moment.day} {MOIS[moment.month - 1][:3]}. {moment:%H:%M}"


def _rate_surprise(event):
    """hawkish / dovish / inline, ou None si le flux n'a pas d'actual."""
    actual = _parse_rate(event.get("actual"))
    if actual is None:
        return None
    reference = _parse_rate(event.get("forecast"))
    if reference is None:
        reference = _parse_rate(event.get("previous"))
    if reference is None:
        return None
    if actual > reference + 0.001:
        return "hawkish"
    if actual < reference - 0.001:
        return "dovish"
    return "inline"


def _is_fed_event(event):
    if (event.get("country") or "").upper() != "USD":
        return False
    title = (event.get("title") or "").lower()
    return any(hint in title for hint in ("federal funds", "fomc", "fed chair", "powell"))


def _is_foreign_cb(event):
    country = (event.get("country") or "").upper()
    hints = FOREIGN_EVENT_HINTS.get(country)
    if not hints:
        return False
    title = (event.get("title") or "").lower()
    return any(hint in title for hint in hints)


def _is_rate_decision(event, titles):
    title = (event.get("title") or "").lower()
    return any(hint in title for hint in titles)


def _clamp_score(value):
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def _impact_word_from_score(score):
    if score is None:
        return None
    if score > 0:
        return "positif"
    if score < 0:
        return "negatif"
    return "neutre"


def load_ff_week(path):
    """Un seul appel au flux Forex Factory. Un 429 ou une erreur garde le cache."""
    cached = _read_json(path, None)
    cached_events = None
    if isinstance(cached, dict) and isinstance(cached.get("events"), list):
        cached_events = cached["events"]
    request = urllib.request.Request(
        FF_CALENDAR_URL,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status == 429:
                raise urllib.error.HTTPError(FF_CALENDAR_URL, 429, "Too Many Requests", response.headers, None)
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"  calendrier HTTP {exc.code}, cache {'conservé' if cached_events is not None else 'absent'}")
        if cached_events is not None:
            return cached_events, True
        return [], False
    except Exception as exc:
        print(f"  calendrier {type(exc).__name__}, cache {'conservé' if cached_events is not None else 'absent'}")
        if cached_events is not None:
            return cached_events, True
        return [], False
    if not isinstance(payload, list):
        if cached_events is not None:
            return cached_events, True
        return [], False
    _write_json(path, {
        "fetched_at": datetime.now(PARIS).isoformat(timespec="seconds"),
        "url": FF_CALENDAR_URL,
        "events": payload,
    })
    return payload, True


def upcoming_from_calendar(raw_events, now_paris):
    now_paris = now_paris.astimezone(PARIS)
    events = []
    for item in raw_events or []:
        if not isinstance(item, dict):
            continue
        country = (item.get("country") or "").upper()
        if country not in ("USD", "EUR"):
            continue
        impact = (item.get("impact") or "").lower()
        if impact not in ("high", "medium"):
            continue
        moment = _event_moment(item)
        if moment is None:
            continue
        if moment < now_paris - timedelta(hours=6) or moment > now_paris + timedelta(days=6):
            continue
        events.append({
            "t": moment.isoformat(timespec="minutes"),
            "heure": f"{JOURS[moment.weekday()][:3]} {moment.day} {MOIS[moment.month - 1][:4]} {moment:%H:%M}",
            "titre": item.get("title") or "Événement",
            "pays": country,
            "impact": "haut" if impact == "high" else "moyen",
            "fort": impact == "high",
        })
    events.sort(key=lambda item: item["t"])
    unique = []
    seen = set()
    for event in events:
        key = (event["t"], event["titre"], event["pays"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(event)
    return unique[:8]


NEWS_FEEDS = (
    ("Forex", "https://fr.investing.com/rss/news_1.rss"),
    ("Matières premières", "https://fr.investing.com/rss/news_11.rss"),
)
NEWS_WINDOW = timedelta(hours=12)


def _parse_rss_time(text):
    if not text:
        return None
    raw = str(text).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S GMT"):
        try:
            moment = datetime.strptime(raw, fmt)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)
            return moment.astimezone(PARIS)
        except ValueError:
            continue
    try:
        moment = datetime.fromisoformat(raw)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment.astimezone(PARIS)
    except ValueError:
        return None


def _news_relevant(title):
    text = title.lower()
    if re.search(r"\bor\b", text) or "l'or" in text or "l’or" in text or "gold" in text or "xau" in text:
        return True
    return any(token in text for token in (
        "fed", "fomc", "powell", "bce", "ecb", "dollar", "guerre", "tension",
        "sanction", "élection", "election", "tarif", "boe", "boj", "banque centrale",
    ))


def _news_tone(title):
    """Ton simple et affiché. +1 soutient l'or, −1 le pèse."""
    text = title.lower()
    fed = any(token in text for token in ("fed", "fomc", "powell"))
    foreign = any(token in text for token in ("bce", "ecb", "boe", "boj", "banque d'angleterre", "banque du japon"))
    hawk = any(token in text for token in ("hawkish", "resserrement", "relève ses taux", "relève les taux", "hausse de taux", "hausses de taux"))
    dove = any(token in text for token in ("dovish", "assoupl", "baisse de taux", "baisses de taux"))
    tension = any(token in text for token in ("guerre", "tension", "sanction", "militaire", "conflit", "élection", "election", "tarif", "droits de douane"))
    if fed and hawk and not dove:
        return "fed", -1
    if fed and dove and not hawk:
        return "fed", 1
    if foreign and hawk and not dove:
        return "banques", 1
    if foreign and dove and not hawk:
        return "banques", -1
    if tension:
        return "geo", 1
    return None, 0


def _parse_rss(raw, source):
    root = ET.fromstring(raw)
    items = []
    for node in root.findall("./channel/item"):
        title = (node.findtext("title") or "").strip()
        if not title or not _news_relevant(title):
            continue
        moment = _parse_rss_time(node.findtext("pubDate"))
        if moment is None:
            continue
        bucket, tone = _news_tone(title)
        items.append({
            "titre": title,
            "lien": (node.findtext("link") or "").strip(),
            "source": source,
            "t": moment.isoformat(timespec="minutes"),
            "heure": _paris_short(moment),
            "ton": tone,
            "ton_bucket": bucket,
        })
    return items


def _fetch_bytes(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml"})
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            if response.status == 429:
                return None, 429
            return response.read(), response.status
    except urllib.error.HTTPError as exc:
        return None, exc.code
    except Exception as exc:
        print(f"  rss {type(exc).__name__} {url.split('/')[-1]}")
        return None, 0


def load_news(path, now_paris, fetch=True):
    """Deux flux Investing, une fois par run complet. 429 ou erreur : ce flux garde son cache."""
    now_paris = now_paris.astimezone(PARIS)
    cached = _read_json(path, None)
    cached_items = cached.get("items") if isinstance(cached, dict) else []
    if not isinstance(cached_items, list):
        cached_items = []
    by_source = {}
    for item in cached_items:
        if isinstance(item, dict) and item.get("source"):
            by_source.setdefault(item["source"], []).append(item)
    if fetch:
        for source, url in NEWS_FEEDS:
            raw, status = _fetch_bytes(url)
            if raw is None or status == 429:
                print(f"  {source} indisponible ({status}), cache conservé")
                continue
            try:
                by_source[source] = _parse_rss(raw, source)
            except Exception as exc:
                print(f"  rss illisible {source}: {type(exc).__name__}")
        cached_items = [item for rows in by_source.values() for item in rows]
        _write_json(path, {"fetched_at": now_paris.isoformat(timespec="seconds"), "items": cached_items})
    window = []
    for item in cached_items:
        try:
            moment = datetime.fromisoformat(item["t"])
        except (KeyError, TypeError, ValueError):
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=PARIS)
        if now_paris - NEWS_WINDOW <= moment.astimezone(PARIS) <= now_paris + timedelta(minutes=5):
            window.append(item)
    window.sort(key=lambda item: item.get("t") or "", reverse=True)
    return window[:12]


def _blend_news_score(base, news_score):
    base = 0 if base is None else base
    if not news_score:
        return base
    if base == 0:
        return news_score
    if base == news_score:
        return base
    return 0


def apply_news_drivers(drivers, headlines):
    grouped = {"fed": [], "banques": [], "geo": []}
    for item in headlines or []:
        bucket = item.get("ton_bucket")
        if bucket in grouped and item.get("ton"):
            grouped[bucket].append(item)
    by_id = {driver["id"]: driver for driver in drivers}
    for driver_id, bucket in (("fed", "fed"), ("banques", "banques"), ("geo", "geo")):
        driver = by_id.get(driver_id)
        hits = grouped[bucket]
        if driver is None or not hits:
            continue
        news_score = _clamp_score(sum(item["ton"] for item in hits))
        jour = _blend_news_score(driver.get("score_jour"), news_score)
        driver["score_jour"] = jour
        driver["impact"] = _impact_from_scores(jour, driver.get("score_mois"), "neutre")
        titles = " · ".join(item["titre"] for item in hits[:2])
        driver["etat"] = f"{driver.get('etat') or ''} · dépêche : {titles}".strip(" ·")
        driver["news"] = [item["titre"] for item in hits[:3]]
    return drivers


def fetch_calendar(now_paris):
    """Compatibilité : un appel réseau. Le pipeline préfère load_ff_week (cache + 429)."""
    events, loaded = load_ff_week(os.path.join("data", "ff_calendar.json"))
    return upcoming_from_calendar(events, now_paris), loaded


def _dated_events(raw_events):
    dated = []
    for item in raw_events or []:
        if not isinstance(item, dict):
            continue
        moment = _event_moment(item)
        if moment is None:
            continue
        dated.append({
            "t": moment,
            "title": item.get("title") or "Événement",
            "country": (item.get("country") or "").upper(),
            "impact": (item.get("impact") or "").lower(),
            "forecast": item.get("forecast") or "",
            "previous": item.get("previous") or "",
            "actual": item.get("actual"),
        })
    dated.sort(key=lambda event: event["t"])
    return dated


def _describe_unscored(event, waiting):
    when = _paris_short(event["t"])
    forecast = _parse_rate(event.get("forecast"))
    previous = _parse_rate(event.get("previous"))
    extra = ""
    if forecast is not None and previous is not None:
        extra = f", prévu {fr_num(forecast, 2)} % (précédent {fr_num(previous, 2)} %)"
    elif forecast is not None:
        extra = f", prévu {fr_num(forecast, 2)} %"
    if waiting:
        return f"en attente : {event['title']} {when}{extra}"
    return f"{event['title']} tenu le {when}, résultat non publié{extra}"


def _surprise_line(event, kind, gold_score):
    when = _paris_short(event["t"])
    actual = _parse_rate(event.get("actual"))
    reference = _parse_rate(event.get("forecast"))
    if reference is None:
        reference = _parse_rate(event.get("previous"))
    if kind == "hawkish":
        tone = "plus haut que prévu"
    elif kind == "dovish":
        tone = "plus bas que prévu"
    else:
        tone = "en ligne avec le prévu"
    return (
        f"{event['title']} {when} : {fr_num(actual, 2)} % {tone}"
        f" ({fr_num(reference, 2)} %) · score or {gold_score:+d}"
    )


def score_fed_driver(events, now_paris, cache):
    """USD banque centrale. Sans « actual », score 0 et Neutre, jamais Indisponible."""
    now_paris = now_paris.astimezone(PARIS)
    fed_events = [event for event in events if _is_fed_event(event)]
    rate_events = [event for event in fed_events if _is_rate_decision(event, FED_RATE_TITLES)]
    scored = []
    for event in rate_events:
        kind = _rate_surprise(event)
        if kind is None:
            continue
        gold = {"hawkish": -1, "dovish": 1, "inline": 0}[kind]
        scored.append((event, kind, gold))
    dff = (cache or {}).get("dff") or {}
    month_change = dff["change_mois"] if "change_mois" in dff else dff.get("change")
    month_score = _signed_score(month_change, 0.01, invert=True)
    rate_bit = f"Fed funds {fr_num(dff['value'], 2)} %" if dff.get("value") is not None else ""
    if scored:
        event, kind, gold = scored[-1]
        jour = gold
        etat = _surprise_line(event, kind, gold)
    else:
        jour = 0
        upcoming = [event for event in (rate_events or fed_events) if event["t"] >= now_paris]
        past = [event for event in (rate_events or fed_events) if event["t"] < now_paris]
        if upcoming:
            etat = _describe_unscored(upcoming[0], waiting=True)
        elif past:
            etat = _describe_unscored(past[-1], waiting=False)
        else:
            etat = "aucun événement Fed dans le calendrier de la semaine"
        if month_score is None:
            month_score = 0
    detail_parts = [rate_bit, "Forex Factory, calendrier de la semaine"]
    if not scored and fed_events:
        names = []
        for event in fed_events:
            if event["title"] not in names:
                names.append(event["title"])
        if len(names) > 1:
            detail_parts.append("aussi : " + ", ".join(names[:4]))
    return _driver(
        "fed", "Fed", etat, " · ".join(part for part in detail_parts if part),
        _impact_word_from_score(_clamp_score((jour or 0) + (month_score or 0))),
        "Forex Factory + FRED DFF",
        None,
        "Décision de taux : actual > prévu = hausse, −1 pour l'or ; actual < prévu = baisse, +1. Discours sans chiffre = 0. Le mois reprend le Fed funds FRED (hausse = −1). Sans actual publié : 0, impact neutre.",
        value=dff.get("value"),
        score_jour=jour,
        score_mois=month_score if month_score is not None else 0,
    )


def score_foreign_cb_driver(events, now_paris):
    now_paris = now_paris.astimezone(PARIS)
    foreign = [event for event in events if _is_foreign_cb(event)]
    rate_events = []
    for event in foreign:
        titles = FOREIGN_RATE_TITLES.get(event["country"], ())
        if titles and _is_rate_decision(event, titles):
            rate_events.append(event)
    surprises = []
    for event in rate_events:
        kind = _rate_surprise(event)
        if kind is None:
            continue
        gold = {"hawkish": 1, "dovish": -1, "inline": 0}[kind]
        surprises.append((event, kind, gold))
    if surprises:
        total = _clamp_score(sum(item[2] for item in surprises))
        bits = [_surprise_line(event, kind, gold) for event, kind, gold in surprises[:3]]
        etat = " ; ".join(bits)
        jour = total
    else:
        jour = 0
        pool = rate_events or foreign
        upcoming = [event for event in pool if event["t"] >= now_paris]
        past = [event for event in pool if event["t"] < now_paris]
        if upcoming:
            etat = _describe_unscored(upcoming[0], waiting=True)
        elif past:
            banks = []
            for event in past:
                name = event["title"]
                if name not in banks:
                    banks.append(name)
            etat = "tenues, résultat non publié : " + ", ".join(banks[:4])
            etat += " · " + _paris_short(past[-1]["t"])
        else:
            etat = "aucune décision BCE, BoE, BoJ, SNB, BoC, RBA, RBNZ ou PBoC cette semaine"
    return _driver(
        "banques", "Banques centrales", etat, "Forex Factory · surprise dovish à l'étranger = −1 (dollar plus ferme), hawkish = +1",
        _impact_word_from_score(jour),
        "Forex Factory",
        None,
        "Banques hors Fed : actual plus haut que prévu = hawkish, dollar plus faible, +1 pour l'or. Actual plus bas = dovish, −1. Sans chiffre publié : 0, impact neutre.",
        score_jour=jour,
        score_mois=None,
    )


def score_geopolitics_driver(events, vix_level, vix_change, gold_pct, dxy_pct, vix_month_change=None):
    """Proxy honnête : le calendrier n'a pas de fil géopolitique."""
    vix_score = _vix_score(vix_level, vix_change)
    refuge = 0
    if gold_pct is not None and dxy_pct is not None and gold_pct > 0.15 and dxy_pct > 0.15:
        refuge = 1
    month_score = _vix_score(vix_level, vix_month_change) if vix_month_change is not None else None
    if vix_score is None and gold_pct is None and dxy_pct is None:
        return None
    if refuge == 1 and (vix_score is None or vix_score >= 0):
        jour = 1
    elif vix_score == 1 and refuge == 0:
        jour = 1
    elif vix_score == -1 and refuge == 0:
        jour = -1
    else:
        jour = 0
    vix_txt = "VIX indisponible" if vix_level is None else f"VIX {fr_num(vix_level, 1)}"
    if vix_change is not None:
        vix_txt += f" ({fr_pct(vix_change)} jour)"
    if refuge == 1:
        move = "or et dollar montent ensemble : prime de refuge"
    elif gold_pct is None or dxy_pct is None:
        move = "écart or / dollar indisponible"
    else:
        move = f"or {fr_pct(gold_pct)}, DXY {fr_pct(dxy_pct)} : pas d'achat refuge"
    political = []
    for event in events:
        title = (event.get("title") or "").lower()
        if event.get("impact") not in ("high", "medium"):
            continue
        if any(hint in title for hint in NON_ECONOMIC_HINTS):
            political.append(f"{event['title']} {_paris_short(event['t'])}")
    etat = f"proxy (VIX/refuge) : {vix_txt}. {move}."
    detail = "Forex Factory n'a pas de fil géopolitique."
    if political:
        detail += " Événements politiques du calendrier : " + " ; ".join(political[:3])
    return _driver(
        "geo", "Géopolitique", etat, detail,
        _impact_word_from_score(jour),
        "proxy VIX + or/DXY",
        None,
        "Proxy, pas un fil d'actualité. +1 si le VIX est en aversion (niveau ≥ 20 et en hausse, ou ≤ 16 et en baisse = −1) ou si l'or monte en même temps que le DXY. Sinon 0.",
        value=vix_level,
        variation=vix_change,
        score_jour=jour,
        score_mois=month_score,
    )


def apply_calendar_drivers(drivers, raw_events, now_paris, cache, gold_pct):
    dated = _dated_events(raw_events)
    vix = (cache or {}).get("vix") or {}
    dxy = (cache or {}).get("dxy") or {}
    dxy_pct = dxy["change_jour"] if "change_jour" in dxy else dxy.get("variation")
    vix_change = vix["change_jour"] if "change_jour" in vix else vix.get("variation")
    replacements = {
        "fed": score_fed_driver(dated, now_paris, cache),
        "banques": score_foreign_cb_driver(dated, now_paris),
    }
    geo = score_geopolitics_driver(
        dated, vix.get("price"), vix_change, gold_pct, dxy_pct, vix.get("change_mois"),
    )
    if geo is not None:
        replacements["geo"] = geo
    merged = []
    for driver in drivers:
        merged.append(replacements.get(driver["id"], driver))
    return merged


def build_risque(levels, price, now_paris, events):
    opening = None if not levels else levels.get("ouverture")
    last = price
    change = None if opening is None or last is None else last - opening
    change_pct = None if not opening or change is None else change / opening * 100
    gap = weekend_gap_warning(now_paris)
    return {
        "ouverture": None if opening is None else round(opening, 2),
        "dernier": None if last is None else round(last, 2),
        "variation": None if change is None else round(change, 2),
        "variation_pct": None if change_pct is None else round(change_pct, 2),
        "evenements": events,
        "gap_weekend": gap,
        "gap_texte": "Week-end : le marché est fermé jusqu'à dimanche 23h00 (Paris). Un gap est possible à la réouverture." if gap else None,
    }


def minute_url(day):
    return (
        "https://datafeed.dukascopy.com/datafeed/XAUUSD/"
        f"{day.year}/{day.month - 1:02d}/{day.day:02d}/BID_candles_min_1.bi5"
    )


def parse_minute_bi5(raw, day):
    if not raw:
        return []
    decoded = lzma.decompress(raw)
    base = datetime(day.year, day.month, day.day, tzinfo=UTC)
    bars = []
    count = len(decoded) // 24
    for index in range(count):
        second, open_, close, low, high, volume = struct.unpack_from(">IIIIIf", decoded, index * 24)
        if volume != volume or volume <= 0:
            continue
        bars.append({
            "t": base + timedelta(seconds=int(second)),
            "o": open_ / 1000.0,
            "h": high / 1000.0,
            "l": low / 1000.0,
            "c": close / 1000.0,
            "v": float(volume),
        })
    return bars


def minute_dates_for(trading_day):
    sunday = week_start(trading_day)
    previous = previous_weekday(trading_day)
    first = min(sunday, previous - timedelta(days=1))
    dates = []
    cursor = first
    while cursor <= trading_day:
        dates.append(cursor)
        cursor += timedelta(days=1)
    return dates


def _minute_refresh(day, now_utc):
    if day >= now_utc.date():
        return True
    if day == now_utc.date() - timedelta(days=1) and now_utc.weekday() < 5:
        return True
    return False


def load_minutes(path):
    payload = _read_json(path, {"days": {}})
    bars = []
    for rows in (payload.get("days") or {}).values():
        for row in rows:
            try:
                bars.append({
                    "t": parse_time(row["t"]),
                    "o": float(row["o"]),
                    "h": float(row["h"]),
                    "l": float(row["l"]),
                    "c": float(row["c"]),
                    "v": float(row["v"]),
                })
            except (KeyError, TypeError, ValueError):
                continue
    bars.sort(key=lambda item: item["t"])
    return bars, payload.get("days") or {}


def save_minutes(path, days):
    _write_json(path, {"days": days})


def update_minutes(path, trading_day, now_utc):
    bars, days = load_minutes(path)
    fetched = 0
    for day in minute_dates_for(trading_day):
        key = day.isoformat()
        if key in days and not _minute_refresh(day, now_utc):
            continue
        print(f"  minutes {key} …", flush=True)
        url = minute_url(day)
        got = None
        for attempt in range(3):
            try:
                raw = download_to_temp(url)
                if not raw:
                    print("    téléchargement vide, nouvel essai")
                    time.sleep(10)
                    continue
                parsed = parse_minute_bi5(raw, day)
                if not parsed:
                    print("    fichier sans volume, nouvel essai")
                    time.sleep(10)
                    continue
                got = parsed
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    got = []
                    break
                print(f"    http {exc.code}, pause 10s")
                time.sleep(10)
            except Exception as exc:
                print(f"    {type(exc).__name__}, pause 10s")
                time.sleep(10)
        time.sleep(10)
        if got is None:
            print("    échec, cache inchangé")
            continue
        days[key] = [
            {
                "t": iso_z(bar["t"]),
                "o": round(bar["o"], 3),
                "h": round(bar["h"], 3),
                "l": round(bar["l"], 3),
                "c": round(bar["c"], 3),
                "v": round(bar["v"], 6),
            }
            for bar in got
        ]
        fetched += 1
        print(f"    {len(got)} minutes")
        save_minutes(path, days)
    bars, _days = load_minutes(path)
    return bars, {"fichiers": fetched}


def desk_context(minutes, trading_day, price):
    if not minutes or price is None:
        return None, None
    day_start, day_end = day_bounds(trading_day)
    day_bars = bars_between(minutes, day_start, day_end)
    if not day_bars:
        return None, None
    week_open = _paris_at(week_start(trading_day), 23, 0)
    week_bars = bars_between(minutes, week_open, day_end)
    previous = previous_weekday(trading_day)
    prev_bars = bars_between(minutes, *day_bounds(previous))
    profile = volume_profile(day_bars, price)
    levels = build_desk_levels(price, day_bars, week_bars, prev_bars, profile, trading_day)
    return levels, profile


def analyze(candles, manual, cache, now_paris, minutes=None, events=None, ff_events=None, calendar_ok=False, headlines=None):
    now_paris = now_paris.astimezone(PARIS)
    now_utc = now_paris.astimezone(UTC)
    candles = sorted(candles, key=lambda item: item["t"])
    closed = closed_candles(candles, now_utc)
    partial = None
    if candles and candles[-1]["t"] + timedelta(hours=1) > now_utc:
        partial = candles[-1]
    last = partial or (closed[-1] if closed else None)
    last_price = None if last is None else last["c"]

    orderflow = compute_orderflow(closed)
    tech = technicals(closed, last_price)
    sessions = build_sessions(candles, now_paris)
    day = trading_day_for(now_paris)
    today_public = [public_session(item) for item in sessions if item["jour"] == day.isoformat()]
    preferred = preferred_from_sessions(sessions, now_paris)
    warnings = []
    if orderflow.get("signal") == "NEUTRE":
        warnings.append("Zone neutre : éviter de trader")
    if tech.get("rsi14") is not None and tech["rsi14"] < 30:
        warnings.append("RSI en survente (< 30) : extrême, prudence")
    if tech.get("rsi14") is not None and tech["rsi14"] > 70:
        warnings.append("RSI en surachat (> 70) : extrême, prudence")
    preferred["warnings"] = warnings

    drivers = drivers_from_sources(cache, manual)
    biais, conviction = macro_bias(drivers)
    macro = {
        "biais": biais,
        "conviction": conviction,
        "derniere_mise_a_jour_manuelle": (manual or {}).get("derniere_mise_a_jour") if isinstance(manual, dict) else None,
        "drivers": drivers,
    }
    of_dir = {"ACHETEUR": "HAUSSIER", "VENDEUR": "BAISSIER", "NEUTRE": "NEUTRE"}.get(orderflow.get("signal"), "NEUTRE")
    price = _price_block(closed, partial, last_price, sessions, day, now_paris)
    levels = build_levels(candles, today_public, last_price)
    desk_levels, profile = desk_context(minutes or [], day, last_price)
    if desk_levels and desk_levels.get("ouverture") is not None and last_price is not None:
        opening = desk_levels["ouverture"]
        price["change_jour"] = round(last_price - opening, 3)
        price["change_jour_pct"] = round((last_price - opening) / opening * 100, 3) if opening else None
        price["ouverture_jour"] = opening
    if calendar_ok:
        drivers = apply_calendar_drivers(
            drivers, ff_events or [], now_paris, cache, price.get("change_jour_pct"),
        )
        macro["calendrier"] = "Forex Factory"
    if headlines:
        drivers = apply_news_drivers(drivers, headlines)
    if calendar_ok or headlines:
        biais, conviction = macro_bias(drivers)
        macro["biais"] = biais
        macro["conviction"] = conviction
        macro["drivers"] = drivers
    board = build_alignment(macro, tech, orderflow, profile, desk_levels, last_price)
    conf = {
        "score": board["confiance"],
        "label": board["jauge"],
        "conviction": board["conviction"],
        "direction": board["direction"],
        "parts": [
            {"label": "Force macro (tous les moteurs, jour et mois)", "points": board["force_macro"], "actif": True},
            {"label": "Moyenne technique (biais, momentum, tendance, heatmap)", "points": board["moyenne_technique"], "actif": True},
            {"label": "Confiance = 0,5 × macro + 0,5 × moyenne technique", "points": board["confiance"], "actif": True},
        ],
    }
    market = market_status(now_paris, last)
    manual_note = None if calendar_ok else manual_freshness(manual, now_paris.date())
    brief = build_brief(price, orderflow, preferred, tech, macro, levels)
    if headlines:
        titles = " ; ".join(f"« {item['titre']} »" for item in headlines[:2])
        brief = f"{brief} Dépêches Investing (12 h) : {titles}."
    alerts = build_alerts(orderflow, tech, macro, preferred, market, manual_note)
    gameplan = build_gameplan(last_price, preferred, desk_levels, profile, orderflow, tech.get("rsi14"))
    if events is None and minutes is not None:
        events = []
    risque = build_risque(desk_levels, last_price, now_paris, events or [])
    if orderflow.get("signal") in ("ACHETEUR", "VENDEUR", "NEUTRE"):
        banner = banner_for(
            of_dir if of_dir in ("HAUSSIER", "BAISSIER") else "NEUTRE",
            orderflow.get("conviction") if orderflow.get("signal") in ("ACHETEUR", "VENDEUR") else "BASSE",
        )
    else:
        banner = {
            "direction": "NEUTRE",
            "titre": "SIGNAL INDISPONIBLE — 40 bougies requises",
            "conviction": None,
        }

    payload = {
        "version": 1,
        "instrument": "XAU/USD",
        "generated_at": now_paris.isoformat(timespec="seconds"),
        "generated_label": human_paris(now_paris),
        "timezone": "Europe/Paris",
        "market": market,
        "price": price,
        "banner": banner,
        "confidence": conf,
        "jauge": {
            "score": conf["score"],
            "label": conf["label"],
            "conviction": conf["conviction"],
            "direction": conf["direction"],
        },
        "sessions": {
            "jour": day.isoformat(),
            "jour_label": f"{JOURS[day.weekday()]} {day.day} {MOIS[day.month - 1]}",
            "seances": today_public,
        },
        "prefere": preferred,
        "orderflow": {key: value for key, value in orderflow.items() if key != "price_pct"},
        "technicals": tech,
        "macro": macro,
        "alignment": board,
        "levels": levels,
        "niveaux": desk_levels,
        "profil": profile,
        "gameplan": gameplan,
        "risque": risque,
        "brief": brief,
        "news": headlines or [],
        "alertes": alerts,
        "sources": {
            "prix": "Dukascopy XAUUSD spot, volume tick côté bid",
            "orderflow": "Estimation (close-low)/(high-low) × volume, 40 bougies horaires clôturées",
            "macro": "Yahoo Finance (DXY, VIX, US10Y nominal) et FRED (DFII10, T10YIE, DFF). Fed, banques centrales et géopolitique : data/macro.json",
        },
    }
    return payload, sessions


def _price_block(closed, partial, last_price, sessions, day, now_paris):
    if last_price is None:
        return {"last": None, "time": None, "en_cours": False, "change_24h": None, "change_24h_pct": None, "change_jour": None, "change_jour_pct": None}
    source = partial or closed[-1]
    target = now_paris.astimezone(UTC) - timedelta(hours=24)
    prior = [candle for candle in closed if candle["t"] <= target]
    ref = prior[-1]["c"] if prior else None
    change = None if ref is None else last_price - ref
    change_pct = None if not ref else change / ref * 100
    asia = next((item for item in sessions if item["jour"] == day.isoformat() and item["id"] == "asia"), None)
    day_open = None if not asia or not asia.get("ohlc") else asia["ohlc"]["o"]
    day_change = None if day_open is None else last_price - day_open
    day_pct = None if not day_open else day_change / day_open * 100
    local = source["t"].astimezone(PARIS)
    return {
        "last": round(last_price, 3),
        "time": local.isoformat(timespec="minutes"),
        "time_label": f"{local.day:02d}/{local.month:02d} {local:%H:%M}",
        "en_cours": partial is not None,
        "change_24h": None if change is None else round(change, 3),
        "change_24h_pct": None if change_pct is None else round(change_pct, 3),
        "change_jour": None if day_change is None else round(day_change, 3),
        "change_jour_pct": None if day_pct is None else round(day_pct, 3),
    }


# ---------------------------------------------------------------------------
# Notifications ntfy
# ---------------------------------------------------------------------------

def summary_message(sessions, now_paris, orderflow, price):
    last, target, imminent = alert_session_pair(sessions, now_paris)
    if last is None or target is None:
        title = "XAU/USD — séance indisponible"
        body = "Mise à jour terminée, mais la séance de référence n'a pas pu être déterminée."
        if orderflow and orderflow.get("signal"):
            body += f"\nOrderflow : {orderflow['signal']}."
        return title, body, "warning"
    biais = (last.get("score") or {}).get("biais")
    direction = direction_from_biais(biais) if biais else "ATTENDRE"
    title = summary_title(last["nom"], biais, direction, target["nom"])
    if biais and last.get("score"):
        var = fr_pct(last["score"]["variation_pct"])
        state = "clôture imminente" if imminent else "terminée"
        line = f"{last['nom']} {state} : {biais} ({var})."
    else:
        line = f"{last['nom']} : données de séance indisponibles."
    lines = [
        line,
        f"Sens privilégié pour {target['nom']} : {direction}.",
    ]
    if orderflow and orderflow.get("signal"):
        lines.append(
            f"Orderflow : {orderflow['signal']}, conviction {orderflow['conviction']} "
            f"(achat {orderflow['buy_pct']} % / vente {orderflow['sell_pct']} %)."
        )
    else:
        lines.append("Orderflow : indisponible.")
    if price and price.get("last") is not None:
        lines.append(f"Prix : {fr_price(price['last'])}.")
    # Warnings are attached later by the caller via preferred, duplicated here from orderflow/RSI
    # only for the neutre case which is the critical one. RSI is added by plan_notifications.
    return title, "\n".join(lines), tag_for(biais or direction)


def signal_message(previous, orderflow, price):
    signal = orderflow.get("signal")
    title = signal_title(signal)
    lines = [f"Le signal orderflow passe de {previous} à {signal}."]
    if orderflow.get("buy_pct") is not None:
        lines.append(
            f"Conviction {orderflow.get('conviction')}, achat {orderflow['buy_pct']} % / vente {orderflow['sell_pct']} %."
        )
    if price and price.get("last") is not None:
        lines.append(f"Prix : {fr_price(price['last'])}.")
    return title, "\n".join(lines), tag_for(signal)


def plan_notifications(payload, sessions, state, now_paris, mode, topic_set, force_summary=False):
    """Décide quoi envoyer. Ne contacte pas ntfy."""
    state = dict(state or {})
    notes = []
    orderflow = payload.get("orderflow") or {}
    signal = orderflow.get("signal")
    previous = state.get("signal")
    if topic_set and previous and signal and previous != signal:
        title, body, tag = signal_message(previous, orderflow, payload.get("price"))
        notes.append({
            "id": "signal_change",
            "title": title,
            "message": body,
            "tags": tag,
            "priority": 4,
        })
    slot = matching_full_slot(now_paris)
    slot_key = slot.strftime("%Y-%m-%d %H:%M") if slot else None
    if force_summary:
        slot_key = now_paris.astimezone(PARIS).strftime("manuel-%Y-%m-%d %H:%M")
    send_summary = topic_set and (force_summary or (mode == "full" and slot_key is not None))
    if send_summary and state.get("last_summary_slot") != slot_key:
        title, body, tag = summary_message(sessions, now_paris, orderflow, payload.get("price"))
        extra = payload.get("prefere", {}).get("warnings") or []
        if extra:
            body = body + "\n" + "\n".join(extra)
        notes.append({
            "id": "summary",
            "title": title,
            "message": body,
            "tags": tag,
            "priority": 3,
            "slot": slot_key,
        })
    proposed = {
        "signal": signal or previous,
        "last_summary_slot": slot_key if any(note["id"] == "summary" for note in notes) else state.get("last_summary_slot"),
    }
    return notes, proposed


def apply_notification_state(previous, proposed, notes, results):
    state = dict(previous or {})
    by_id = {note["id"]: ok for note, ok in results}
    if "signal_change" not in by_id or by_id.get("signal_change"):
        if proposed.get("signal"):
            state["signal"] = proposed["signal"]
    if by_id.get("summary"):
        slot = next(note.get("slot") for note in notes if note["id"] == "summary")
        state["last_summary_slot"] = slot
    elif proposed.get("last_summary_slot") and "summary" not in by_id:
        state["last_summary_slot"] = previous.get("last_summary_slot") if previous else None
    state["updated"] = datetime.now(PARIS).isoformat(timespec="seconds")
    return state


def build_ntfy_request(topic, title, message, click, priority=3, tags="bell"):
    def clean(value):
        return str(value).replace("\r", " ").replace("\n", " ").strip()

    title = clean(title)
    click = clean(click)
    tags = clean(tags)
    if not click.startswith("https://"):
        raise ValueError("L'URL Click doit commencer par https://")
    body = message.encode("utf-8")
    path = "/" + urllib.parse.quote(topic, safe="")
    header = "\r\n".join([
        f"POST {path} HTTP/1.1",
        "Host: ntfy.sh",
        f"User-Agent: {USER_AGENT}",
        "Content-Type: text/plain; charset=utf-8",
        f"Content-Length: {len(body)}",
        f"Title: {title}",
        f"Click: {click}",
        f"Priority: {int(priority)}",
        f"Tags: {tags}",
        "Connection: close",
        "",
        "",
    ])
    return header.encode("utf-8") + body


def send_ntfy(topic, title, message, click, priority=3, tags="bell"):
    raw = build_ntfy_request(topic, title, message, click, priority, tags)
    context = ssl.create_default_context()
    try:
        with socket.create_connection(("ntfy.sh", 443), timeout=20) as sock:
            with context.wrap_socket(sock, server_hostname="ntfy.sh") as secure:
                secure.settimeout(20)
                secure.sendall(raw)
                chunks = []
                while sum(len(part) for part in chunks) < 20000:
                    try:
                        piece = secure.recv(4096)
                    except socket.timeout:
                        break
                    if not piece:
                        break
                    chunks.append(piece)
        response = b"".join(chunks)
        status = response.split(b"\r\n", 1)[0].decode("iso-8859-1", "replace")
        ok = " 200 " in f" {status} " or status.endswith(" 200") or " 200 " in status
        return ok, status[:120]
    except Exception as exc:
        text = str(exc).replace(topic, "***")
        return False, text[:200]


def topic_is_valid(topic):
    if not topic:
        return False
    if len(topic) < 3 or len(topic) > 64:
        return False
    return all(char.isalnum() or char in "-_" for char in topic)


# ---------------------------------------------------------------------------
# Dukascopy
# ---------------------------------------------------------------------------

def market_hour_open(moment):
    moment = moment.astimezone(UTC)
    weekday = moment.weekday()
    hour = moment.hour
    if weekday == 5:
        return False
    if weekday == 6 and hour < 22:
        return False
    if weekday == 4 and hour >= 22:
        return False
    if hour == 21:
        return False
    return True


def bi5_url(moment):
    moment = moment.astimezone(UTC)
    return (
        "https://datafeed.dukascopy.com/datafeed/XAUUSD/"
        f"{moment.year}/{moment.month - 1:02d}/{moment.day:02d}/{moment.hour:02d}h_ticks.bi5"
    )


def download_to_temp(url):
    """Télécharge dans un fichier temporaire, puis relit ce fichier."""
    handle, path = tempfile.mkstemp(prefix="xau_", suffix=".bi5")
    os.close(handle)
    try:
        raw = _request(url, timeout=50)
        with open(path, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        with open(path, "rb") as handle:
            return handle.read()
    finally:
        if os.path.exists(path):
            os.remove(path)


def parse_bi5(raw):
    if not raw or len(raw) < 16:
        return None
    decoded = lzma.decompress(raw)
    count = len(decoded) // 20
    if count <= 0:
        return None
    first = high = low = last = None
    volume = 0.0
    for index in range(count):
        _ms, _ask, bid, _ask_vol, bid_vol = struct.unpack_from(">IIIff", decoded, index * 20)
        price = bid / 1000.0
        if price <= 0 or price > 100000:
            continue
        if first is None:
            first = high = low = price
        else:
            if price > high:
                high = price
            if price < low:
                low = price
        last = price
        if bid_vol == bid_vol and bid_vol > 0:
            volume += float(bid_vol)
    if first is None or last is None:
        return None
    return {
        "o": round(first, 3),
        "h": round(high, 3),
        "l": round(low, 3),
        "c": round(last, 3),
        "v": round(volume, 4),
    }


def fetch_hour(moment):
    url = bi5_url(moment)
    delay = 8
    last_error = "echec"
    for _attempt in range(4):
        try:
            raw = download_to_temp(url)
            bar = parse_bi5(raw)
            time.sleep(0.8)
            if bar is None:
                return None, "vide"
            return bar, None
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                time.sleep(0.4)
                return None, "absent"
            last_error = f"http {exc.code}"
            print(f"    {last_error}, pause {delay}s")
            time.sleep(delay)
            delay = min(delay * 2, 60)
        except Exception as exc:
            last_error = type(exc).__name__
            print(f"    {last_error}, pause {delay}s")
            time.sleep(delay)
            delay = min(delay * 2, 60)
    return None, last_error


def hours_to_fetch(now_utc, existing, max_hours, absent=None):
    """Heures manquantes, les plus récentes d'abord.

    `absent` mémorise les heures déjà confirmées sans fichier, pour ne pas
    les redemander à chaque passage (Dukascopy répond lentement aux 404).
    """
    absent = absent or {}
    end = now_utc.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=28)
    missing = []
    moment = end
    while moment >= start:
        if market_hour_open(moment):
            key = hour_key(moment)
            record = existing.get(key)
            closed = now_utc >= moment + timedelta(hours=1)
            if key in absent and closed:
                try:
                    marked = parse_time(absent[key])
                except (TypeError, ValueError):
                    marked = now_utc
                if now_utc - marked < timedelta(hours=18):
                    moment -= timedelta(hours=1)
                    continue
            if record is None or (not closed) or (not record.get("closed")):
                missing.append(moment)
        moment -= timedelta(hours=1)
    return missing[:max_hours]


def load_candles(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    candles = []
    for item in payload.get("candles", []):
        try:
            candles.append({
                "t": parse_time(item["t"]),
                "o": float(item["o"]),
                "h": float(item["h"]),
                "l": float(item["l"]),
                "c": float(item["c"]),
                "v": float(item["v"]),
                "closed": bool(item.get("closed", True)),
            })
        except (KeyError, TypeError, ValueError):
            continue
    candles.sort(key=lambda item: item["t"])
    return candles


def save_candles(path, candles):
    payload = {
        "candles": [
            {
                "t": hour_key(item["t"]),
                "o": item["o"],
                "h": item["h"],
                "l": item["l"],
                "c": item["c"],
                "v": item["v"],
                "closed": bool(item.get("closed", True)),
            }
            for item in sorted(candles, key=lambda item: item["t"])
        ]
    }
    _write_json(path, payload)


def update_candles(candles, now_utc, max_hours, candles_path=None, absent_path=None, on_progress=None):
    existing = {hour_key(item["t"]): item for item in candles}
    absent = _read_json(absent_path, {"hours": {}}).get("hours", {}) if absent_path else {}
    queue = hours_to_fetch(now_utc, existing, max_hours, absent)
    print(f"Heures Dukascopy à télécharger : {len(queue)} (plafond {max_hours})")
    failures = 0
    streak = 0
    fetched = 0

    def persist():
        if not candles_path:
            return
        merged_now = [item for item in existing.values() if item["t"] >= now_utc - timedelta(days=40)]
        save_candles(candles_path, merged_now)
        if absent_path:
            _write_json(absent_path, {"hours": absent})

    for moment in queue:
        print(f"  {hour_key(moment)} …", flush=True)
        bar, error = fetch_hour(moment)
        if error in ("absent", "vide"):
            streak = 0
            if now_utc >= moment + timedelta(hours=1):
                absent[hour_key(moment)] = iso_z(now_utc)
            print("    absent")
            continue
        if error:
            failures += 1
            streak += 1
            print(f"    échec ({error})")
            if streak >= 6:
                print("Six échecs de suite : arrêt de la file, reprise des dernières heures.")
                break
            continue
        streak = 0
        fetched += 1
        closed = now_utc >= moment + timedelta(hours=1)
        existing[hour_key(moment)] = {"t": moment, **bar, "closed": closed}
        absent.pop(hour_key(moment), None)
        print(f"    ok c={bar['c']} v={bar['v']}")
        if fetched % 8 == 0:
            persist()
            if on_progress:
                snapshot = [item for item in existing.values() if item["t"] >= now_utc - timedelta(days=40)]
                on_progress(sorted(snapshot, key=lambda item: item["t"]))
    # Le fichier de l'heure en cours arrive parfois après le début du run.
    wall = datetime.now(UTC)
    retry_from = wall.replace(minute=0, second=0, microsecond=0)
    retried = 0
    moment = retry_from
    while retried < 3 and retry_from - moment <= timedelta(hours=10):
        if market_hour_open(moment):
            retried += 1
            record = existing.get(hour_key(moment))
            hour_closed = wall >= moment + timedelta(hours=1)
            if not (record and record.get("closed") and hour_closed):
                print(f"  reprise {hour_key(moment)} …", flush=True)
                bar, error = fetch_hour(moment)
                if bar:
                    existing[hour_key(moment)] = {
                        "t": moment, **bar, "closed": hour_closed,
                    }
                    absent.pop(hour_key(moment), None)
                    fetched += 1
                    print(f"    ok c={bar['c']} v={bar['v']}")
                elif error in ("absent", "vide") and hour_closed:
                    absent[hour_key(moment)] = iso_z(wall)
        moment -= timedelta(hours=1)
    merged = [item for item in existing.values() if item["t"] >= now_utc - timedelta(days=40)]
    merged.sort(key=lambda item: item["t"])
    persist()
    return merged, {"telechargees": fetched, "echecs": failures, "demandees": len(queue)}


def _write_json(path, payload):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


def _read_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


# ---------------------------------------------------------------------------
# Programme
# ---------------------------------------------------------------------------

def run(data_dir, mode, max_hours, force_summary, pages_url, topic, dry_run=False, now=None):
    now = (now or datetime.now(PARIS)).astimezone(PARIS)
    now_utc = now.astimezone(UTC)
    os.makedirs(data_dir, exist_ok=True)
    candles_path = os.path.join(data_dir, "candles.json")
    latest_path = os.path.join(data_dir, "latest.json")
    state_path = os.path.join(data_dir, "alert_state.json")
    cache_path = os.path.join(data_dir, "macro_cache.json")
    manual_path = os.path.join(data_dir, "macro.json")

    print(f"Mode {mode} — {human_paris(now)}")
    manual = _read_json(manual_path, {})
    cache = _read_json(cache_path, {})
    if mode == "full" or not cache:
        print("Macro…")
        cache = refresh_macro_cache(cache)
        _write_json(cache_path, cache)
    else:
        print("Macro : cache existant (mode orderflow).")

    candles = load_candles(candles_path)

    def on_progress(snapshot):
        provisional, _sessions = analyze(snapshot, manual, cache, now)
        provisional["notifications"] = {
            "topic_configure": topic_is_valid(topic),
            "envoyees": [],
            "prevues": [],
            "note": "Fichier provisoire pendant le téléchargement Dukascopy.",
        }
        _write_json(latest_path, provisional)
        print(f"  aperçu écrit ({len(snapshot)} bougies, prix {provisional['price'].get('last')})")

    candles, fetch_info = update_candles(
        candles,
        now_utc,
        max_hours,
        candles_path=candles_path,
        absent_path=os.path.join(data_dir, "absent_hours.json"),
        on_progress=on_progress,
    )
    save_candles(candles_path, candles)
    print(f"Bougies en mémoire : {len(candles)}")

    print("Minutes Dukascopy…")
    minute_bars, minute_info = update_minutes(os.path.join(data_dir, "minutes.json"), trading_day_for(now), now_utc)
    print(f"Minutes en mémoire : {len(minute_bars)} (fichiers relus {minute_info['fichiers']})")
    print("Calendrier Forex Factory (un seul appel)…")
    ff_raw, calendar_ok = load_ff_week(os.path.join(data_dir, "ff_calendar.json"))
    events = upcoming_from_calendar(ff_raw, now) if calendar_ok else []
    print("News Investing…" if mode == "full" else "News Investing : cache")
    headlines = load_news(os.path.join(data_dir, "news_cache.json"), now, fetch=(mode == "full"))
    print(f"  {len(headlines)} dépêche(s) sur 12 h")
    payload, sessions = analyze(
        candles, manual, cache, now, minute_bars, events, ff_raw, calendar_ok, headlines,
    )
    if payload.get("risque") is not None:
        payload["risque"]["calendrier_ok"] = calendar_ok
    payload["fetch"] = dict(fetch_info)
    payload["fetch"]["minutes"] = minute_info

    state = _read_json(state_path, {})
    topic_set = topic_is_valid(topic)
    notes, proposed = plan_notifications(payload, sessions, state, now, mode, topic_set, force_summary)
    sent = []
    results = []
    if not topic:
        note = "NTFY_TOPIC n'est pas défini : aucune notification envoyée."
    elif not topic_set:
        note = "NTFY_TOPIC invalide (3 à 64 lettres, chiffres, - ou _). Aucune notification."
    elif dry_run:
        note = "Essai à blanc : notifications non envoyées."
    else:
        note = "Notifications traitées."
    for item in notes:
        if dry_run or not topic_set:
            results.append((item, False))
            continue
        ok, status = send_ntfy(topic, item["title"], item["message"], pages_url, item["priority"], item["tags"])
        results.append((item, ok))
        sent.append({"type": item["id"], "titre": item["title"], "ok": ok, "statut": status if ok else "echec"})
        print(("  ntfy ok — " if ok else "  ntfy échec — ") + item["title"])
    new_state = apply_notification_state(state, proposed, notes, results if topic_set and not dry_run else [])
    if not topic_set or dry_run:
        # Mémoriser le signal pour ne pas alerter « changement » le jour où le sujet est ajouté
        # sur tout l'historique, tout en n'avançant pas le créneau de résumé non envoyé.
        if proposed.get("signal"):
            new_state["signal"] = proposed["signal"]
        new_state["last_summary_slot"] = state.get("last_summary_slot")
        new_state["updated"] = now.isoformat(timespec="seconds")
    _write_json(state_path, new_state)
    payload["notifications"] = {
        "topic_configure": topic_set,
        "envoyees": sent,
        "prevues": [item["title"] for item in notes],
        "note": note,
    }
    if payload["price"]["last"] is None and not candles:
        print("Aucune donnée de prix. latest.json n'est pas écrasé.")
        return 1
    _write_json(latest_path, payload)
    signal = payload["orderflow"].get("signal")
    price = payload["price"].get("last")
    print(f"Prix {price} — signal {signal} — {payload['prefere']['phrase']}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Met à jour le desk XAU/USD")
    parser.add_argument("--mode", choices=("full", "orderflow"), default="full")
    parser.add_argument("--max-hours", type=int, default=200)
    parser.add_argument("--force-summary", action="store_true")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    pages_url = os.environ.get("PAGES_URL", DEFAULT_PAGES_URL).strip() or DEFAULT_PAGES_URL
    code = run(
        data_dir=args.data_dir,
        mode=args.mode,
        max_hours=args.max_hours,
        force_summary=args.force_summary,
        pages_url=pages_url,
        topic=topic,
        dry_run=args.dry_run,
    )
    raise SystemExit(code)


if __name__ == "__main__":
    main()
