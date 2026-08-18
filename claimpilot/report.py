"""Renderers: case_file.json (the audit artifact), brief.md (terminal/PR
mirror), brief.html (the deliverable - single self-contained file, light/dark
aware, printable), and draft_reply.txt.

Rendering is presentation only: every figure shown here already exists in the
case dictionary; nothing is computed in this module beyond percentages for
bar widths.
"""

from __future__ import annotations

import html
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List

from .util import D, money, pretty_json

_REF = re.compile(r"\[((?:F|D|G|E|CT)-\d+)\]")


def _m(value: Any) -> str:
    try:
        return money(value)
    except (InvalidOperation, ValueError, TypeError):
        return str(value)


def _pct_of(part: Any, whole: Any) -> int:
    try:
        p, w = D(str(part)), D(str(whole))
        if w == 0:
            return 0
        return max(0, min(100, int(p / w * 100)))
    except (InvalidOperation, ValueError, TypeError):
        return 0


# ------------------------------------------------------------------ markdown

def render_markdown(case: Dict[str, Any]) -> str:
    c, run, pos = case["claim"], case["run"], case["position"]
    pn = case["position_numbers"]
    lines: List[str] = []
    add = lines.append
    add("# Negotiation Position Brief - Claim {}".format(c.get("claim_id")))
    add("")
    add("**{}** vs **{}** | status {} | owner {} | generated {} | provider {} ({}) | "
        "retrieval {}".format(c.get("claimant"), c.get("carrier"), c.get("status"),
                              c.get("owner"), run["generated_at"], run["provider"],
                              run["model"], run["retrieval_backend"]))
    add("")
    add("| Demand | Carrier offer | Recommended counter | Expected band |")
    add("|---|---|---|---|")
    band = "{} - {}".format(
        _m(pn.get("position.expected_band_low", {}).get("value", "?")),
        _m(pn.get("position.expected_band_high", {}).get("value", "?")))
    add("| {} | {} | {} | {} |".format(
        _m(c.get("demand_usd")), _m(c.get("carrier_offer_usd")),
        _m(pn.get("position.recommended_counter", {}).get("value", "?")), band))
    add("")
    if pos.get("ok"):
        add("## Executive summary")
        add("")
        add(pos["data"].get("executive_summary", ""))
        add("")
    else:
        add("> **Composition failed closed.** The AI-written sections were withheld "
            "because they did not pass the grounding guard ({} violations). The "
            "deterministic analysis below is unaffected.".format(len(pos["violations"])))
        add("")
    add("## Entitlement by demand line (deterministic)")
    add("")
    add("| # | Line | Claimed | Entitled (low-high) | Class | Rationale |")
    add("|---|---|---|---|---|---|")
    for e in case["entitlements"]:
        add("| {} | {} | {} | {} - {} | {} | {} |".format(
            e["ent_id"], e["label"], _m(e["claimed"]), _m(e["entitled_low"]),
            _m(e["entitled_high"]), e["classification"],
            e["rationale"].replace("|", "/")))
    add("")
    add("## The negotiation numbers")
    add("")
    for key, entry in pn.items():
        add("- **{}** = {}  ({})".format(key.split(".")[-1], _m(entry["value"]),
                                         entry.get("formula", "")))
    add("")
    add("## Discrepancies & consistency findings")
    add("")
    for d in case["discrepancies"]:
        add("- **[{}] {} - {}**: {}".format(d["disc_id"], d["severity"], d["title"],
                                            d["description"]))
        if d.get("authority_note"):
            add("  - *Authority*: {}".format(d["authority_note"]))
    add("")
    add("## Evidence gaps")
    add("")
    for g in case["gaps"]:
        add("- **[{}] {}** - {} (requested by: {}) Impact: {}".format(
            g["gap_id"], g["item"], g["why_needed"], g.get("requested_by") or "n/a",
            g.get("impact", "")))
    add("")
    add("## Contract terms applied")
    add("")
    for t in case["contract_terms"]:
        add("- **[{}] {}** ({}): \"{}\"".format(t["term_id"], t["topic"], t["section"],
                                                t["quote"]))
    add("")
    add("## Historical comparables")
    add("")
    add("| Claim | Match | Type | Claimed | Settled | Pct | Summary |")
    add("|---|---|---|---|---|---|---|")
    for cp in case["comparables"]:
        add("| {} | {} ({:.2f}) | {} | {} | {} | {}% | {} |".format(
            cp["claim_id"], cp.get("match_basis", "structural"), cp["score"],
            cp["issue_type"], _m(cp["claimed"]), _m(cp["settled"]),
            cp["settlement_pct"], cp["negotiation_summary"]))
    for co in case["cohorts"]:
        add("- Cohort **{}** (n={}): median {}%, range {}%-{}%".format(
            co["description"], co["n"], co["median_pct"], co["min_pct"], co["max_pct"]))
    add("")
    if pos.get("ok"):
        add("## Negotiation analysis (AI-composed, guard-validated)")
        add("")
        for p in pos["data"].get("negotiation_analysis", []):
            add(p)
            add("")
        add("## Recommended next steps")
        add("")
        for i, s in enumerate(pos["data"].get("recommended_next_steps", []), 1):
            add("{}. **{}** - {}".format(i, s.get("action", ""), s.get("rationale", "")))
        add("")
        add("## Risks & watchouts")
        add("")
        for r in pos["data"].get("risks_and_watchouts", []):
            add("- {}".format(r))
        add("")
        draft = pos["data"].get("draft_reply", {})
        add("## Draft reply (for review - not sent)")
        add("")
        add("**Subject:** {}".format(draft.get("subject", "")))
        add("")
        add("```")
        add(draft.get("body", ""))
        add("```")
        add("")
    sec = case.get("security", {})
    add("## Security & tamper checks")
    add("")
    if sec.get("findings"):
        for f in sec["findings"]:
            add("- **{} / {}** in `{}` ({}): `{}`".format(
                f["severity"], f["kind"], f["source_id"], f.get("location", ""),
                f["evidence"]))
        add("")
        add("The recommended position is computed deterministically; injected document "
            "content cannot alter the math, only the prose, which NumberGuard, the "
            "reference check and the eval judge police.")
    else:
        add("- No injection or tamper indicators across {} scanned sources "
            "(classes: {}).".format(sec.get("sources_scanned", "?"),
                                    ", ".join(sec.get("classes", []))))
    add("")
    qa = case["qa"]
    add("## Quality & audit")
    add("")
    add("- Citation validity: {}/{} quotes verified ({}%)".format(
        qa["quotes_exact"] + qa["quotes_fuzzy"], qa["quotes_total"],
        round(qa["citation_validity_rate"] * 100, 1)))
    add("- Quarantined facts: {}".format(len(qa["quarantined_facts"]) or "none"))
    add("- NumberGuard: {} (position attempts: {})".format(
        "clean" if pos.get("ok") else "FAILED", pos.get("attempts")))
    add("- LLM cost this run: ${:.2f} | elapsed {}s | ablated: {}".format(
        run["llm_cost_usd"], run["elapsed_s"], run["ablated_sources"] or "none"))
    return "\n".join(lines)


# ---------------------------------------------------------------------- html

_CSS = """
:root{
  --bg:#f6f7f9; --panel:#ffffff; --ink:#1a2130; --muted:#5b6474; --line:#e3e6ec;
  --accent:#1f6feb; --accent-ink:#ffffff; --good:#1a7f37; --good-bg:#e6f4ea;
  --warn:#9a6700; --warn-bg:#fff3d6; --bad:#c93c37; --bad-bg:#fdebea;
  --info:#57606a; --info-bg:#eef1f4; --lever:#6f42c1; --lever-bg:#f1ebfa;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}
@media (prefers-color-scheme: dark){:root{
  --bg:#0e1116; --panel:#161b23; --ink:#e6e9ef; --muted:#9aa4b2; --line:#2a3140;
  --accent:#539bf5; --good:#57ab5a; --good-bg:#1b2a1f; --warn:#c69026;
  --warn-bg:#2b2412; --bad:#e5534b; --bad-bg:#2d1a19; --info:#909dab;
  --info-bg:#1d232c; --lever:#b083f0; --lever-bg:#241a33;}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:28px 20px 80px}
header.hero{background:var(--panel);border:1px solid var(--line);border-radius:14px;
  padding:22px 26px;margin-bottom:18px}
h1{font-size:22px;margin:0 0 6px} h2{font-size:17px;margin:34px 0 10px}
h1 .cid{font-family:var(--mono);color:var(--accent)}
.meta{color:var(--muted);font-size:13px}
.chip{display:inline-block;padding:2px 10px;border-radius:999px;font-size:12px;
  font-weight:600;letter-spacing:.2px;border:1px solid transparent}
.chip.STRONG{background:var(--good-bg);color:var(--good)}
.chip.MODERATE{background:var(--warn-bg);color:var(--warn)}
.chip.NEEDS_INFO{background:var(--info-bg);color:var(--info)}
.chip.EXCLUDED_CONTRACTUAL{background:var(--bad-bg);color:var(--bad)}
.chip.GOODWILL_LEVER{background:var(--lever-bg);color:var(--lever)}
.chip.HIGH{background:var(--bad-bg);color:var(--bad)}
.chip.MEDIUM{background:var(--warn-bg);color:var(--warn)}
.chip.LOW,.chip.INFO{background:var(--info-bg);color:var(--info)}
.chip.OKAY{background:var(--good-bg);color:var(--good)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));
  gap:12px;margin:16px 0}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:12px;
  padding:14px 16px}
.stat .k{color:var(--muted);font-size:12px;text-transform:uppercase;
  letter-spacing:.6px}
.stat .v{font-size:22px;font-weight:700;font-family:var(--mono);margin-top:4px}
.stat .s{color:var(--muted);font-size:12px;margin-top:2px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;
  padding:16px 18px;margin:10px 0}
table{width:100%;border-collapse:collapse;font-size:14px}
th{color:var(--muted);text-align:left;font-size:12px;text-transform:uppercase;
  letter-spacing:.5px;padding:8px 10px;border-bottom:1px solid var(--line)}
td{padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}
td.num,th.num{text-align:right;font-family:var(--mono);white-space:nowrap}
tr:last-child td{border-bottom:none}
.scroll{overflow-x:auto}
.bar{height:6px;border-radius:4px;background:var(--info-bg);margin-top:6px;
  min-width:120px}
.bar>span{display:block;height:100%;border-radius:4px;background:var(--accent)}
.ref{font-family:var(--mono);font-size:11px;background:var(--info-bg);
  color:var(--accent);border-radius:5px;padding:1px 5px;text-decoration:none}
.quote{font-family:var(--mono);font-size:12.5px;background:var(--info-bg);
  border-left:3px solid var(--accent);border-radius:6px;padding:8px 10px;
  margin:6px 0;white-space:pre-wrap}
.card{border:1px solid var(--line);border-left-width:4px;border-radius:10px;
  padding:12px 14px;margin:10px 0;background:var(--panel)}
.card.HIGH{border-left-color:var(--bad)} .card.MEDIUM{border-left-color:var(--warn)}
.card.LOW{border-left-color:var(--info)}
.card.INFO{border-left-color:var(--good)}
.card h3{margin:0 0 6px;font-size:14.5px}
.card .auth{color:var(--muted);font-size:13px;margin-top:6px;font-style:italic}
.email{border:1px solid var(--line);border-radius:12px;background:var(--panel);
  overflow:hidden;margin-top:8px}
.email .subj{padding:12px 16px;border-bottom:1px solid var(--line);font-weight:600}
.email .body{padding:16px;white-space:pre-wrap;font-size:14px}
.small{font-size:12.5px;color:var(--muted)}
details{margin:8px 0}
details>summary{cursor:pointer;font-weight:600;padding:8px 0;color:var(--accent)}
.badge{display:inline-block;font-family:var(--mono);font-size:11px;
  color:var(--muted);border:1px solid var(--line);border-radius:6px;
  padding:1px 6px;margin-left:6px}
.tag-ai{background:var(--lever-bg);color:var(--lever);border:none;font-weight:600}
.tag-det{background:var(--good-bg);color:var(--good);border:none;font-weight:600}
.note{background:var(--warn-bg);color:var(--warn);border-radius:10px;
  padding:12px 14px;font-size:14px}
footer{margin-top:40px;color:var(--muted);font-size:12.5px;text-align:center}
@media print{body{background:#fff}.panel,.card,.stat,header.hero{border-color:#ccc}
  details{open:true} .wrap{max-width:none}}
"""


def _esc(text: Any) -> str:
    return html.escape(str(text if text is not None else ""))


def _refs(text: str) -> str:
    """Escape, then linkify [F-012]-style references."""
    return _REF.sub(r'<a class="ref" href="#\1">\1</a>', _esc(text))


def render_html(case: Dict[str, Any]) -> str:
    c, run, pos, pn, qa = (case["claim"], case["run"], case["position"],
                           case["position_numbers"], case["qa"])
    out: List[str] = []
    add = out.append
    counter = pn.get("position.recommended_counter", {}).get("value")
    band_lo = pn.get("position.expected_band_low", {}).get("value")
    band_hi = pn.get("position.expected_band_high", {}).get("value")

    add("<title>Claim {} Position Brief</title>".format(_esc(c.get("claim_id"))))
    add('<style>{}</style>'.format(_CSS))
    add('<div class="wrap">')
    add('<header class="hero"><h1>Negotiation Position Brief '
        '<span class="cid">{}</span></h1>'.format(_esc(c.get("claim_id"))))
    add('<div class="meta">{} vs {} &nbsp;•&nbsp; status <b>{}</b> &nbsp;•&nbsp; '
        'owner {} &nbsp;•&nbsp; generated {}<br>provider {} ({}) &nbsp;•&nbsp; '
        'retrieval <b>{}</b>{} &nbsp;•&nbsp; prompts {} &nbsp;•&nbsp; '
        'LLM cost ${:.2f}</div>'.format(
            _esc(c.get("claimant")), _esc(c.get("carrier")), _esc(c.get("status")),
            _esc(c.get("owner")), _esc(run["generated_at"]), _esc(run["provider"]),
            _esc(run["model"]), _esc(run["retrieval_backend"]),
            " <span class='badge'>fallback: {}</span>".format(
                _esc(run["retrieval_fallback_note"][:80]))
            if run.get("retrieval_fallback_note") else "",
            _esc(run["prompts_version"]), run["llm_cost_usd"]))
    if run.get("ablated_sources"):
        add('<p class="note">Degraded run: source(s) intentionally removed for this run: '
            '<b>{}</b>. Findings and entitlements reflect the reduced evidence.</p>'
            .format(_esc(", ".join(run["ablated_sources"]))))
    add('</header>')

    add('<div class="stats">')
    for key, value, sub in [
            ("Shipper demand", _m(c.get("demand_usd")), "6 components"),
            ("Carrier offer", _m(c.get("carrier_offer_usd")),
             "equals the evidence floor" if pn.get("position.offer_equals_floor",
                                                   {}).get("value") else "on the table"),
            ("Recommended counter", _m(counter) if counter else "n/a",
             "every dollar documented"),
            ("Expected settlement band",
             "{} – {}".format(_m(band_lo), _m(band_hi)) if band_lo else "n/a",
             "conservative – full success")]:
        add('<div class="stat"><div class="k">{}</div><div class="v">{}</div>'
            '<div class="s">{}</div></div>'.format(_esc(key), _esc(value), _esc(sub)))
    add('</div>')

    if pos.get("ok"):
        add('<div class="panel"><h2 style="margin-top:0">Executive summary '
            '<span class="badge tag-ai">AI-composed · guard-validated</span></h2>'
            '<p>{}</p></div>'.format(_refs(pos["data"].get("executive_summary", ""))))
    else:
        add('<p class="note"><b>Composition failed closed.</b> The AI-written sections '
            'were withheld: {} grounding violations after {} attempt(s). The '
            'deterministic sections below are unaffected. Details in the QA panel.</p>'
            .format(len(pos["violations"]), pos.get("attempts")))

    # ---- entitlement table
    add('<h2>Entitlement by demand line '
        '<span class="badge tag-det">deterministic</span></h2>')
    add('<div class="panel scroll"><table><tr><th></th><th>Demand line</th>'
        '<th class="num">Claimed</th><th class="num">Entitled low–high</th>'
        '<th>Classification</th><th>Rationale</th></tr>')
    for e in case["entitlements"]:
        bar = _pct_of(e["entitled_high"], e["claimed"])
        add('<tr id="{id}"><td><a class="ref" href="#{id}">{id}</a></td>'
            '<td><b>{label}</b><div class="bar" title="entitled high as share of '
            'claimed"><span style="width:{bar}%"></span></div></td>'
            '<td class="num">{claimed}</td><td class="num">{lo} – {hi}</td>'
            '<td><span class="chip {cls}">{cls}</span>{flags}</td><td>{rat}</td></tr>'
            .format(id=_esc(e["ent_id"]), label=_esc(e["label"]), bar=bar,
                    claimed=_m(e["claimed"]), lo=_m(e["entitled_low"]),
                    hi=_m(e["entitled_high"]), cls=_esc(e["classification"]),
                    flags="".join('<div class="small">• {}</div>'.format(_esc(f))
                                  for f in e.get("flags", [])),
                    rat=_esc(e["rationale"])))
    add('</table></div>')

    # ---- negotiation numbers
    add('<h2>The negotiation numbers <span class="badge tag-det">deterministic</span></h2>')
    add('<div class="panel scroll"><table><tr><th>Quantity</th><th class="num">Value</th>'
        '<th>How it was computed</th></tr>')
    for key, entry in pn.items():
        add('<tr id="{fid}"><td>{k} <a class="ref" href="#{fid}">{fid}</a></td>'
            '<td class="num">{v}</td><td class="small">{f}{n}</td></tr>'.format(
                fid=_esc(entry["id"]), k=_esc(key.split(".")[-1].replace("_", " ")),
                v=_esc(_m(entry["value"])), f=_esc(entry.get("formula", "")),
                n=" — <i>{}</i>".format(_esc(entry["note"])) if entry.get("note") else ""))
    add('</table></div>')

    # ---- discrepancies
    add('<h2>Discrepancies &amp; consistency findings '
        '<span class="badge tag-det">deterministic</span></h2>')
    for d in case["discrepancies"]:
        sev = d["severity"]
        add('<div class="card {sev}" id="{id}"><h3><span class="chip {sev}">{sev}</span> '
            '&nbsp;{title} <a class="ref" href="#{id}">{id}</a></h3><div>{desc}</div>{auth}'
            '</div>'.format(
                sev=_esc(sev), id=_esc(d["disc_id"]), title=_esc(d["title"]),
                desc=_refs(d["description"]),
                auth='<div class="auth">Authority: {}</div>'.format(
                    _esc(d["authority_note"])) if d.get("authority_note") else ""))

    # ---- gaps
    add('<h2>Evidence gaps &amp; open questions</h2><div class="panel"><table>'
        '<tr><th></th><th>Missing item</th><th>Why it matters</th><th>Impact</th></tr>')
    for g in case["gaps"]:
        add('<tr id="{id}"><td><a class="ref" href="#{id}">{id}</a></td><td><b>{item}</b>'
            '<div class="small">requested by: {req}</div></td><td>{why}</td><td>{imp}</td>'
            '</tr>'.format(id=_esc(g["gap_id"]), item=_esc(g["item"]),
                           req=_esc(g.get("requested_by") or "—"),
                           why=_esc(g["why_needed"]), imp=_esc(g.get("impact", ""))))
    add('</table></div>')

    # ---- contract terms
    add('<h2>Contract terms applied <span class="badge tag-det">retrieved + quoted '
        'verbatim</span></h2><div class="panel">')
    for t in case["contract_terms"]:
        quoted = [ct for ct in t.get("citations", []) if ct.get("quote")]
        if not t.get("quote"):
            chip = ' <span class="chip MEDIUM">no clause quoted</span>'
        elif quoted and all(ct.get("verified") for ct in quoted):
            chip = ' <span class="chip OKAY">quote verified</span>'
        else:
            chip = ' <span class="chip MEDIUM">quote unverified</span>'
        add('<div id="{id}" style="margin-bottom:12px"><b>{topic}</b> '
            '<span class="badge">{sec}</span> <a class="ref" href="#{id}">{id}</a>'
            '{v}<div class="quote">{q}</div></div>'.format(
                id=_esc(t["term_id"]), topic=_esc(t["topic"].replace("_", " ")),
                sec=_esc(t["section"]),
                v=chip, q=_esc(t["quote"] or "(no supporting clause quoted)")))
    add('</div>')

    # ---- comparables
    add('<h2>Historical comparables <span class="badge tag-det">deterministic '
        'similarity</span></h2>')
    add('<div class="panel scroll"><table><tr><th>Claim</th><th>Match</th>'
        '<th>Type</th><th>Service</th><th class="num">Claimed</th>'
        '<th class="num">Settled</th><th class="num">Pct</th><th>Outcome</th></tr>')
    for cp in case["comparables"]:
        add('<tr><td class="num">{}</td><td class="small">{} <span class="badge">'
            '{:.2f}</span></td><td>{}</td><td>{}</td>'
            '<td class="num">{}</td><td class="num">{}</td><td class="num">{}%</td>'
            '<td class="small">{}{}</td></tr>'.format(
                _esc(cp["claim_id"]), _esc(cp.get("match_basis", "structural")),
                cp["score"], _esc(cp["issue_type"]),
                _esc(cp["service_level"]), _m(cp["claimed"]), _m(cp["settled"]),
                _esc(cp["settlement_pct"]), _esc(cp["negotiation_summary"]),
                " <i>({})</i>".format(_esc(cp["notes"])) if cp.get("notes") else ""))
    add('</table>')
    for co in case["cohorts"]:
        add('<div class="small" style="margin-top:6px">Cohort <b>{}</b> (n={}): median '
            '<b>{}%</b>, range {}%–{}%</div>'.format(
                _esc(co["description"]), co["n"], _esc(co["median_pct"]),
                _esc(co["min_pct"]), _esc(co["max_pct"])))
    add('<div class="small" style="margin-top:8px"><i>Caveat: historical settlement '
        'percentages are outcomes, not contractual entitlements.</i></div></div>')

    # ---- AI sections
    if pos.get("ok"):
        data = pos["data"]
        add('<h2>Negotiation analysis <span class="badge tag-ai">AI-composed · '
            'guard-validated · {} refs</span></h2><div class="panel">'.format(
                len(pos.get("refs_used", []))))
        for p in data.get("negotiation_analysis", []):
            add('<p>{}</p>'.format(_refs(p)))
        add('</div>')
        add('<h2>Recommended next steps <span class="badge tag-ai">AI-composed · '
            'recommendations</span></h2><div class="panel"><ol>')
        for s in data.get("recommended_next_steps", []):
            add('<li style="margin-bottom:8px"><b>{}</b><br><span class="small">{}'
                '</span></li>'.format(_refs(s.get("action", "")),
                                      _refs(s.get("rationale", ""))))
        add('</ol></div>')
        add('<h2>Risks &amp; watchouts <span class="badge tag-ai">AI-composed</span>'
            '</h2><div class="panel"><ul>')
        for r in data.get("risks_and_watchouts", []):
            add('<li>{}</li>'.format(_refs(r)))
        add('</ul></div>')
        draft = data.get("draft_reply", {})
        add('<h2>Draft reply <span class="badge tag-ai">AI-composed · for review, '
            'not sent</span></h2><div class="email"><div class="subj">Subject: {}</div>'
            '<div class="body">{}</div></div>'.format(
                _esc(draft.get("subject", "")), _esc(draft.get("body", ""))))

    # ---- retrieval audit
    retr = case.get("retrieval", {})
    add('<h2>Retrieval audit</h2><div class="panel scroll"><table><tr><th>Topic</th>'
        '<th>Query</th><th>Backend</th><th>Status</th><th class="num">Sufficiency</th>'
        '<th>Plan id</th><th>Top hit</th><th class="num">ms</th></tr>')
    for r in retr.get("log", []):
        add('<tr><td>{}</td><td class="small">{}</td><td>{}</td>'
            '<td><span class="chip {}">{}</span></td><td class="num">{}</td>'
            '<td class="small" style="font-family:var(--mono)">{}</td><td class="small">'
            '{}</td><td class="num">{}</td></tr>'.format(
                _esc(r["topic"]), _esc(r["query"]), _esc(r["backend"]),
                "OKAY" if r["status"] == "ok" else "MEDIUM", _esc(r["status"]),
                _esc(r.get("sufficiency", "")), _esc(r.get("plan_id", ""))[:22],
                _esc(r.get("top") or "—"), _esc(r.get("elapsed_ms", ""))))
    add('</table>')
    if retr.get("no_clause_topics"):
        add('<div class="small" style="margin-top:6px">Topics with <b>no supporting '
            'clause</b> (honest abstention, not a guess): {}</div>'.format(
                _esc(", ".join(retr["no_clause_topics"]))))
    if retr.get("explain_sample"):
        add('<details><summary>Retrieval plan (datum explain)</summary>'
            '<div class="quote">{}</div></details>'.format(_esc(retr["explain_sample"])))
    add('</div>')

    # ---- security panel
    sec = case.get("security", {})
    add('<h2>Security &amp; tamper checks '
        '<span class="badge tag-det">deterministic scan, every run</span></h2>')
    if sec.get("findings"):
        for f in sec["findings"]:
            add('<div class="card HIGH"><h3><span class="chip {sev}">{sev}</span> '
                '&nbsp;{kind} in {src}</h3><div class="small">{loc}</div>'
                '<div class="quote">{ev}</div></div>'.format(
                    sev=_esc(f["severity"]), kind=_esc(f["kind"]),
                    src=_esc(f["source_id"]), loc=_esc(f.get("location", "")),
                    ev=_esc(f["evidence"])))
        add('<p class="note">Indicators of adversarial content were found. The '
            'recommended position is computed deterministically, so injected text '
            'cannot alter the math; the prose is policed by NumberGuard, the reference '
            'check and the eval judge. Review the flagged sources before relying on '
            'their statements.</p>')
    else:
        add('<div class="panel"><span class="chip OKAY">clean</span> '
            '<span class="small">No injection or tamper indicators across {} scanned '
            'sources. Classes checked: {}.</span></div>'.format(
                _esc(sec.get("sources_scanned", "?")),
                _esc(", ".join(sec.get("classes", [])))))

    # ---- QA panel
    add('<h2>Quality &amp; guardrails</h2><div class="stats">')
    for key, value, sub in [
            ("Citation validity", "{}%".format(round(qa["citation_validity_rate"] * 100, 1)),
             "{} exact + {} fuzzy of {} quotes".format(qa["quotes_exact"],
                                                       qa["quotes_fuzzy"],
                                                       qa["quotes_total"])),
            ("Quarantined facts", str(len(qa["quarantined_facts"])),
             "excluded from all reasoning"),
            ("NumberGuard", "clean" if pos.get("ok") else "FAILED CLOSED",
             "attempt(s): {}".format(pos.get("attempts"))),
            ("Elapsed", "{}s".format(run["elapsed_s"]),
             "{} LLM calls, {} cached".format(
                 len(run["llm_calls"]),
                 sum(1 for x in run["llm_calls"] if x["cached"])))]:
        add('<div class="stat"><div class="k">{}</div><div class="v" '
            'style="font-size:18px">{}</div><div class="s">{}</div></div>'.format(
                _esc(key), _esc(value), _esc(sub)))
    add('</div>')
    if pos.get("violations"):
        add('<div class="panel"><b>Guard violations (withheld output):</b><ul>')
        for v in pos["violations"][:20]:
            add('<li class="small">{}</li>'.format(_esc(v)))
        add('</ul></div>')
    if qa.get("quote_failures"):
        add('<div class="panel"><b>Quote verification failures:</b><ul>')
        for f in qa["quote_failures"][:20]:
            add('<li class="small">{} ({}): "{}"</li>'.format(
                _esc(f["fact_id"]), _esc(f["key"]), _esc(f["quote"])))
        add('</ul></div>')

    # ---- appendices
    add('<h2>Appendix</h2>')
    add('<details><summary>Fact ledger ({} facts - every value, its provenance, and '
        'its formula)</summary><div class="panel scroll"><table><tr><th>Id</th>'
        '<th>Key</th><th>Value</th><th>Kind</th><th>Method</th><th class="num">Conf</th>'
        '<th>Source&nbsp;/ formula</th></tr>'.format(len(case["facts"])))
    for f in case["facts"]:
        cites = f.get("citations", [])
        src = ""
        if cites:
            c0 = cites[0]
            mark = ("✓" if c0.get("verified") else
                    "✗" if c0.get("verified") is False else "")
            src = "{} {} <span class='small'>{}</span>".format(
                _esc(c0.get("source_id", "")), mark,
                _esc((c0.get("quote") or "")[:90]))
        if f.get("formula"):
            src += "<div class='small'>= {}</div>".format(_esc(f["formula"]))
        add('<tr id="{id}"><td class="num"><a class="ref" href="#{id}">{id}</a></td>'
            '<td class="small" style="font-family:var(--mono)">{key}</td>'
            '<td class="small">{val}</td><td class="small">{kind}</td>'
            '<td class="small">{meth}</td><td class="num">{conf}</td><td>{src}</td></tr>'
            .format(id=_esc(f["fact_id"]), key=_esc(f["key"]),
                    val=_esc(json.dumps(f["value"], ensure_ascii=False)[:160]),
                    kind=_esc(f["kind"]), meth=_esc(f["method"]),
                    conf=f.get("confidence", 1.0), src=src))
    add('</table></div></details>')
    add('<details><summary>Sources ({} files, checksummed)</summary>'
        '<div class="panel scroll"><table><tr><th>Source</th><th>File</th><th>Kind</th>'
        '<th>Trust tier</th><th>Status</th><th>sha256</th></tr>'.format(
            len(case["sources"])))
    for s in case["sources"]:
        add('<tr><td>{}</td><td class="small">{}</td><td>{}</td><td class="small">{}</td>'
            '<td><span class="chip {}">{}</span></td>'
            '<td class="small" style="font-family:var(--mono)">{}</td></tr>'.format(
                _esc(s["source_id"]), _esc(s["filename"]), _esc(s["kind"]),
                _esc(s["trust_tier"]),
                "OKAY" if s["status"] in ("OK", "OCR_DERIVED") else "HIGH",
                _esc(s["status"]), _esc(s["sha256"][:16])))
    add('</table></div></details>')

    add('<footer>Freight Claim Copilot — facts are cited and mechanically verified; '
        'computed figures carry their formulas; AI-composed sections are labeled and '
        'guard-validated. A specialist decision remains required before anything is '
        'sent.</footer>')
    add('</div>')
    return "\n".join(out)


# ------------------------------------------------------------------- writing

def write_outputs(case: Dict[str, Any], out_dir: str) -> Dict[str, str]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = {
        "case_file": str(out / "case_file.json"),
        "brief_md": str(out / "position_brief.md"),
        "brief_html": str(out / "position_brief.html"),
    }
    (out / "case_file.json").write_text(pretty_json(case), encoding="utf-8")
    (out / "position_brief.md").write_text(render_markdown(case), encoding="utf-8")
    (out / "position_brief.html").write_text(render_html(case), encoding="utf-8")
    draft = (case.get("position", {}).get("data", {}) or {}).get("draft_reply")
    if draft:
        paths["draft_reply"] = str(out / "draft_reply.txt")
        (out / "draft_reply.txt").write_text(
            "Subject: {}\n\n{}".format(draft.get("subject", ""), draft.get("body", "")),
            encoding="utf-8")
    return paths
