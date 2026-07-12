"""Send-only Telegram client + structured message formatting. No inbound commands in v1.

Two message shapes:
  * format_active        -- full plan with AI narrative, reasoning, management, conviction.
  * format_deterministic -- engine-only fallback when the Gemini call was skipped/failed.
Both use HTML parse_mode; all free-text (AI + sources) is HTML-escaped.
"""

import html
import time
from datetime import datetime, timezone

import requests

from config import ACTIVE_TRADE_THRESHOLD

TG_LIMIT = 4096


def send_message(cfg, text):
    if not cfg.bot_token or not cfg.chat_id:
        print("[Telegram] token/chat_id unset — skipping send")
        return False
    url = f"https://api.telegram.org/bot{cfg.bot_token}/sendMessage"
    payload = {"chat_id": cfg.chat_id, "text": text[:TG_LIMIT], "parse_mode": "HTML",
               "disable_web_page_preview": True}
    for attempt in range(3):
        try:
            r = requests.post(url, json=payload, timeout=8)
            if r.ok:
                return True
            print(f"[Telegram] HTTP {r.status_code}: {r.text[:160]}")
        except Exception as e:
            print(f"[Telegram] send failed ({attempt + 1}/3): {e}")
        time.sleep(2 ** attempt)
    return False


# ---------------- formatting helpers ---------------- #

def _e(v):
    return html.escape(str(v)) if v is not None and v != "" else ""


def _clip(v, n):
    s = _e(v)
    return (s[: n - 1] + "…") if len(s) > n else s


def _p(v):
    """Compact price: thousands-grouped >=1000, else up to 4 dp trailing-zero-trimmed."""
    if not isinstance(v, (int, float)):
        return "—"
    if abs(v) >= 1000:
        return f"{v:,.2f}"
    return (f"{v:.4f}".rstrip("0").rstrip(".")) or "0"


def _ts(snap):
    g = snap.get("generated_at") or ""
    if "T" in g and len(g) >= 16:
        return g[11:16] + " UTC"
    return datetime.now(timezone.utc).strftime("%H:%M") + " UTC"


def _dir_emoji(direction):
    d = (direction or "").upper()
    if d in ("BUY", "BULLISH", "LONG"):
        return "📈"
    if d in ("SELL", "BEARISH", "SHORT"):
        return "📉"
    return "➖"


def _top_confluence(strategies, n=3):
    bd = strategies.get("confluence_breakdown", {}) or {}
    top = sorted(bd.items(), key=lambda kv: kv[1], reverse=True)[:n]
    return " · ".join(f"{k.split('_')[0].upper()} {v}" for k, v in top if v)


def _winrate(strategies):
    wr = strategies.get("empirical_win_rate")
    if wr is None:
        return ""
    src = strategies.get("win_rate_source", "")
    return f"win-rate {round(wr * 100)}%" + (f" ({src})" if src and src != "none" else "")


def _engine_block(et):
    lines = ["📐 <b>Engine plan</b> (deterministic)"]
    lines.append(f"<code>Entry  {_p(et.get('entry'))}</code> {_e(et.get('entry_source'))}")
    lines.append(f"<code>Stop   {_p(et.get('stop_loss'))}</code> {_e(et.get('stop_loss_source'))}")
    lines.append(f"<code>Target {_p(et.get('take_profit'))}</code> {_e(et.get('take_profit_source'))}")
    rr = et.get("rr")
    if rr is not None:
        lines.append(f"R:R <b>{rr}</b> {'✅' if et.get('rr_passed') else '⚠️ &lt;2R'}")
    return "\n".join(lines)


def _module_label(s):
    """This version's regime-conditional mode label: 'trend' or 'mean_revert'
    (no v4-style module/submode split -- see strategies.evaluate_strategies)."""
    return s.get("score_mode") or "?"


def _header_lines(symbol, s, et, action, emoji):
    direction = et.get("direction") or (s.get("setup_direction") or "").upper()
    return [
        f"{emoji} <b>{_e(action)}</b> — {_e(symbol)}",
        f"{_dir_emoji(direction)} <b>{_e(direction)}</b> · {_e(_module_label(s))} · "
        f"score <b>{s.get('confluence_score')}</b>/{ACTIVE_TRADE_THRESHOLD}",
    ]


def _footer_lines(snap, s):
    foot = _top_confluence(s)
    wr = _winrate(s)
    if wr:
        foot = f"{foot} | {wr}" if foot else wr
    out = []
    if foot:
        out.append(f"\n📊 {foot}")
    out.append(f"⏱ {_ts(snap)}")
    return out


# ---------------- full (AI) message ---------------- #

def format_active(symbol, snap, analysis):
    s = snap["strategies"]
    et = s.get("engine_trade", {}) or {}
    a = analysis or {}
    header = a.get("header", {}) or {}
    narr = a.get("narrative", {}) or {}
    td = a.get("trade_decision", {}) or {}
    tm = td.get("trade_management", {}) or {}
    rsn = td.get("reasoning", {}) or {}
    action = (header.get("action") or "ACTIVE_TRADE").replace("_", " ")

    L = _header_lines(symbol, s, et, action, "🟢")

    # AI narrative
    if narr.get("summary"):
        L.append(f"\n💬 <i>{_clip(narr['summary'], 320)}</i>")
    if narr.get("primary_draw"):
        L.append(f"🎯 Draw: {_clip(narr['primary_draw'], 160)}")

    # engine geometry
    L.append("\n" + _engine_block(et))

    # AI trade management (selected from real candidates)
    mgmt = []
    if tm.get("tp1") is not None:
        mgmt.append(f"TP1 {_p(tm['tp1'])}"
                    + (f" ({tm['tp1_rr']}R)" if tm.get("tp1_rr") is not None else "")
                    + (f" · {_e(tm['tp1_source'])}" if tm.get("tp1_source") else ""))
    if tm.get("tp2") is not None:
        mgmt.append(f"TP2 {_p(tm['tp2'])}"
                    + (f" ({tm['tp2_rr']}R)" if tm.get("tp2_rr") is not None else "")
                    + (f" · {_e(tm['tp2_source'])}" if tm.get("tp2_source") else ""))
    if tm.get("move_to_breakeven_at") is not None:
        mgmt.append(f"BE → {_p(tm['move_to_breakeven_at'])}")
    if tm.get("scale_out"):
        mgmt.append(f"Scale: {_clip(tm['scale_out'], 140)}")
    if mgmt:
        L.append("\n🛠 <b>Management</b> (AI)\n" + "\n".join(mgmt))

    # conviction line
    conv = []
    if td.get("confidence_pct") is not None:
        conv.append(f"Conf {td['confidence_pct']}%")
    if td.get("position_size"):
        conv.append(f"Size {_e(td['position_size'])}")
    prim = td.get("primary", {}) or {}
    if prim.get("probability") is not None:
        conv.append(f"P(win) {prim['probability']}%")
    conv_line = " · ".join(conv)
    if conv_line:
        L.append("\n⚖️ " + conv_line)

    # AI reasoning
    why = []
    for label, key in (("Structure", "structure"), ("Liquidity", "liquidity"),
                       ("Momentum", "momentum"), ("Sentiment", "sentiment")):
        if rsn.get(key):
            why.append(f"• <b>{label}:</b> {_clip(rsn[key], 200)}")
    if why:
        L.append("\n🧠 <b>Why</b>\n" + "\n".join(why))

    # triggers / risk
    risk = []
    if td.get("entry_trigger"):
        risk.append(f"🎯 Trigger: {_clip(td['entry_trigger'], 160)}")
    if td.get("invalidation"):
        risk.append(f"⚠️ Invalidation: {_clip(td['invalidation'], 160)}")
    alt = td.get("alternative", {}) or {}
    if alt.get("direction") and alt.get("direction") != "HOLD" and alt.get("trigger"):
        risk.append(f"🔀 Alt {_e(alt['direction'])} "
                    + (f"{alt['probability']}% " if alt.get("probability") is not None else "")
                    + f"if {_clip(alt['trigger'], 120)}")
    if risk:
        L.append("\n" + "\n".join(risk))

    L += _footer_lines(snap, s)
    return "\n".join(L)


# ---------------- engine-only fallback ---------------- #

def format_deterministic(symbol, snap, banner):
    s = snap["strategies"]
    et = s.get("engine_trade", {}) or {}

    L = _header_lines(symbol, s, et, "ACTIVE — engine only", "🟡")
    L.append(f"⚠️ <i>{_e(banner)}</i>")
    L.append("\n" + _engine_block(et))

    # real target candidates (the menu the LLM would have picked from)
    targets = (et.get("candidates") or {}).get("targets") or []
    if targets:
        rows = []
        for t in targets[:3]:
            rr = t.get("rr")
            src = " + ".join(t.get("confluence") or []) or t.get("source") or ""
            hvn = " HVN" if t.get("hvn") else ""
            rows.append(f"• {_p(t.get('price'))}"
                        + (f" ({rr}R)" if rr is not None else "")
                        + hvn + (f" · {_e(src)}" if src else ""))
        L.append("\n🎯 <b>Target candidates</b>\n" + "\n".join(rows))

    # entry/stop options
    entries = (et.get("candidates") or {}).get("entries") or []
    if entries:
        rows = [f"• {_p(e.get('price'))}"
                + (f" {_e(e.get('zone'))}" if e.get("zone") and e.get("zone") != "neutral" else "")
                + (" HVN" if e.get("hvn") else "")
                + (f" · {_e(e.get('source'))}" if e.get("source") else "")
                for e in entries[:2]]
        L.append("\n📍 <b>Entry options</b>\n" + "\n".join(rows))

    # order-flow read (deterministic insight in lieu of AI narrative)
    notes = s.get("order_flow_notes") or []
    if notes:
        L.append("\n🌊 " + _clip("; ".join(notes), 300))

    L += _footer_lines(snap, s)
    return "\n".join(L)
