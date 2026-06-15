#!/usr/bin/env python3
"""Render the Monday Pipeline Brief HTML.

Refactored from build_brief_v2.py. The CSS, masthead, KPI strip, per-rep
sections, tag logic and footer structure are preserved. The difference is
that `deals` arrives as a live list of dicts (not hardcoded
constants), staleness is clocked off `last_real_touch` (the engagement pull),
and enrichment fields (PMS, specialty, locations, contact name) are added to
each deal card's meta line.

Public API:
    render(deals, run_date) -> html_str           # desktop, <style> block
    render_email(deals, run_date) -> html_str      # inlined, email-safe
"""
from datetime import date

PORTAL = "47651487"
CAP = 12  # max deals shown per rep

REPS = {
    "403415850": "Kim Cannon",
    "12742684": "Tami Ferro",
    "530210063": "Eric Wong",
    "79812036": "Jonny Harris",
    "1494239239": "Michelle Simpson",
}
REP_ORDER = ["12742684", "530210063", "403415850", "79812036", "1494239239"]

STAGE = {
    "appointmentscheduled": "First call complete", "qualifiedtobuy": "Demo scheduled",
    "presentationscheduled": "First Demo complete", "977785007": "Pricing/Proposal Scheduled",
    "977785006": "Pricing/Proposal Completed", "952722997": "Proposal sent",
    "952722999": "Nurture", "1035454057": "Verbal agreement", "1338074758": "Not Applicable",
    "992346187": "Cloud conversation started", "1113824689": "Support request made",
    "952805184": "First call complete", "952805185": "Demo scheduled", "952805186": "Demo complete",
    "952805187": "Proposal sent", "952805188": "Nurture", "1035269911": "Verbal agreement",
    "1119649101": "Not Applicable",
}
PARKED = {"952805188", "952722999"}


# ----------------------------------------------------------------------
# small formatting helpers
# ----------------------------------------------------------------------
def fmt_date(d):
    if d is None:
        return None
    if isinstance(d, str):
        y, m, dd = map(int, d.split("-"))
        d = date(y, m, dd)
    # %-d is not portable to Windows; format day without leading zero manually
    return d.strftime("%b ") + str(d.day) + d.strftime(", %Y")


def arr_str(arr):
    if arr in ("", None):
        return None
    try:
        v = int(round(float(arr)))
    except (TypeError, ValueError):
        return None
    return None if v == 0 else "${:,}".format(v)


def badge_class(days):
    if days >= 90:
        return "red"
    if days >= 45:
        return "orange"
    return "amber"


def pipe_label(pipeline, dtype):
    if pipeline == "647046539":
        return "Existing · Atlas" if dtype == "Atlas Conversion" else "Existing · Cloud"
    return "New Customer"


def _arr_int(d):
    s = d.get("arr", "")
    if s in ("", None, "0"):
        return 0
    try:
        return int(round(float(s)))
    except (TypeError, ValueError):
        return 0


# ----------------------------------------------------------------------
# enrichment meta (PMS, specialty, locations, contact name)
# ----------------------------------------------------------------------
def enrichment_pills(d):
    pills = []
    company = d.get("company") or {}
    pms = company.get("current_pms")
    spec = company.get("practice_type")
    locs = company.get("number_of_locations")
    if pms:
        pills.append(f'<span class="pill">PMS: {pms}</span>')
    if spec:
        pills.append(f'<span class="pill">{spec}</span>')
    if locs:
        pills.append(f'<span class="pill">{locs} location{"s" if str(locs) != "1" else ""}</span>')
    contact = d.get("contact") or {}
    name = " ".join(p for p in [contact.get("firstname"), contact.get("lastname")] if p).strip()
    if name:
        title = contact.get("jobtitle")
        label = f"{name}, {title}" if title else name
        pills.append(f'<span class="pill">Contact: {label}</span>')
    return pills


def deal_situation(d):
    days = d["stale_days"]
    touch = d.get("last_real_touch")
    if touch is not None:
        s = (f"Last genuine human touch was {fmt_date(touch)}, {days} days ago "
             f"(notes, calls, emails, meetings, completed tasks or a manual stage "
             f"move — automation bumps excluded). No next step is booked.")
    else:
        s = ("No genuine human touch found across notes, calls, emails, meetings, "
             "tasks or manual stage moves. No next step is booked.")
    if d.get("stage") in PARKED:
        s += " Parked in a nurture / not-applicable stage, so it is not in any active sequence."
    contact = d.get("contact") or {}
    name = " ".join(p for p in [contact.get("firstname"), contact.get("lastname")] if p).strip()
    if name:
        s += f" Primary contact on file: {name}."
    if arr_str(d.get("arr")) is None:
        s += " No ARR value is set on the record."
    return f'<div class="sit">{s}</div>'


def deal_tags(d):
    days = d["stale_days"]
    t = ['<span class="tag bad">No next step</span>']
    if arr_str(d.get("arr")) is None:
        t.append('<span class="tag warn">No ARR</span>')
    nm = (d.get("name") or "").strip()
    if nm.startswith("(") or len(nm) < 8:
        t.append('<span class="tag warn">Incomplete record</span>')
    if days >= 120:
        t.append('<span class="tag bad">120+ days cold</span>')
    return '<div class="tags">' + "".join(t) + '</div>'


def deal_row(d):
    days = d["stale_days"]
    did = d["id"]
    url = f"https://app.hubspot.com/contacts/{PORTAL}/record/0-3/{did}?utm_source=monday_brief"
    arrtxt = arr_str(d.get("arr"))
    arr_html = (f'<span class="arr">{arrtxt} ARR</span><span class="sep">·</span>'
                if arrtxt else '<span class="pill">no ARR set</span><span class="sep">·</span>')
    touch = d.get("last_real_touch")
    touch_txt = f"last touch {fmt_date(touch)}" if touch else "no logged touch"
    extra = "".join(enrichment_pills(d))
    return f"""<div class="row"><div class="row-top">
<a class="nm" href="{url}">{d.get('name','')}<span class="ext">↗ HubSpot</span></a>
<div class="badge {badge_class(days)}">{days}d silent</div></div>
<div class="meta"><span class="pill">{pipe_label(d.get('pipeline'),d.get('dtype'))}</span><span class="pill">{STAGE.get(d.get('stage'),'Open stage')}</span>{extra}<span class="sep">·</span>{arr_html}<span>{touch_txt}</span></div>{deal_situation(d)}{deal_tags(d)}</div>"""


CSS = """
:root{--ink:#0A1B2E;--ink-2:#102844;--paper:#F4F2EB;--card:#FFFFFF;--text:#13212F;--muted:#5E6E7C;--faint:#8C99A4;--line:#E5E1D6;--line-2:#EDEAE1;--accent:#1E63FF;--glow:#1FC4DD;--amber:#B7791F;--amber-bg:#FBF1DC;--orange:#C24A16;--orange-bg:#FBE6D8;--red:#C42727;--red-bg:#FADEDE;--ok:#2C7A57;}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--paper);color:var(--text);font-family:'Hanken Grotesk',sans-serif;-webkit-font-smoothing:antialiased;line-height:1.5;padding:32px 16px 64px;background-image:radial-gradient(circle at 1px 1px,rgba(10,27,46,0.04) 1px,transparent 0);background-size:22px 22px;}
.wrap{max-width:720px;margin:0 auto;}
.masthead{background:var(--ink);background-image:radial-gradient(120% 140% at 88% -20%,rgba(31,196,221,0.30) 0%,rgba(31,196,221,0) 55%),radial-gradient(120% 160% at 8% 120%,rgba(30,99,255,0.34) 0%,rgba(30,99,255,0) 50%);border-radius:14px 14px 0 0;padding:34px 38px 30px;color:#fff;position:relative;overflow:hidden;}
.kicker{font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:0.26em;text-transform:uppercase;color:var(--glow);font-weight:600;display:flex;gap:10px;align-items:center;}
.kicker .dot{width:5px;height:5px;border-radius:50%;background:var(--glow);box-shadow:0 0 10px var(--glow);}
.title{font-family:'Fraunces',serif;font-size:42px;line-height:1.02;font-weight:600;margin:14px 0 6px;letter-spacing:-0.01em;}
.title em{font-style:italic;font-weight:500;color:#cfe6ff;}
.dateline{font-family:'JetBrains Mono',monospace;font-size:12.5px;color:#A9BBCB;display:flex;gap:16px;flex-wrap:wrap;margin-top:4px;}
.dateline b{color:#fff;font-weight:600;}
.kpis{background:var(--ink-2);display:flex;flex-wrap:wrap;padding:4px 38px 20px;}
.kpi{flex:1 1 0;min-width:130px;padding:18px 0 4px;border-top:1px solid rgba(255,255,255,0.08);}
.kpi:not(:last-child){border-right:1px solid rgba(255,255,255,0.07);}
.kpi .lab{font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:0.16em;text-transform:uppercase;color:#8FA3B6;padding-left:18px;}
.kpi:first-child .lab{padding-left:0;}
.kpi .num{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:30px;color:#fff;line-height:1.1;margin-top:6px;padding-left:18px;letter-spacing:-0.02em;}
.kpi:first-child .num{padding-left:0;}
.kpi .num .unit{font-size:15px;color:var(--glow);font-weight:500;}
.kpi.risk .num{color:#FFD9A8;}
.note{background:var(--card);padding:22px 38px;border-bottom:1px solid var(--line);font-size:15px;}
.note b{font-weight:700;}
.note .tag-l{font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:0.16em;text-transform:uppercase;color:var(--accent);font-weight:600;display:block;margin-bottom:7px;}
.rep{background:var(--card);border-bottom:1px solid var(--line);}
.rep-head{display:flex;align-items:baseline;justify-content:space-between;padding:24px 38px 16px;border-left:3px solid var(--accent);gap:16px;flex-wrap:wrap;}
.rep-name{font-family:'Fraunces',serif;font-size:25px;font-weight:600;}
.rep-stats{font-family:'JetBrains Mono',monospace;font-size:12px;color:var(--muted);display:flex;gap:14px;flex-wrap:wrap;align-items:center;}
.rep-stats b{color:var(--text);font-weight:700;}.rep-stats .sep{color:var(--line);}.rep-stats .risk{color:var(--orange);}
.subhead{font-family:'JetBrains Mono',monospace;font-size:10.5px;letter-spacing:0.18em;text-transform:uppercase;color:var(--faint);font-weight:600;padding:8px 38px 0;display:flex;align-items:center;gap:10px;}
.subhead::after{content:"";flex:1;height:1px;background:var(--line-2);}
.deals{padding:10px 38px 18px;}
.row{padding:13px 0;border-bottom:1px dashed var(--line);}
.row:last-child{border-bottom:none;}
.row-top{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;}
a.nm{font-size:15.5px;font-weight:700;color:var(--text);line-height:1.25;text-decoration:none;}
a.nm:hover{color:var(--accent);text-decoration:underline;}
.ext{font-family:'JetBrains Mono',monospace;font-size:0.76em;color:var(--accent);margin-left:6px;font-weight:600;}
.badge{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:11.5px;padding:5px 9px;border-radius:6px;white-space:nowrap;flex-shrink:0;}
.badge.amber{background:var(--amber-bg);color:var(--amber);}.badge.orange{background:var(--orange-bg);color:var(--orange);}.badge.red{background:var(--red-bg);color:var(--red);}
.meta{font-family:'JetBrains Mono',monospace;font-size:11.5px;color:var(--muted);margin-top:7px;display:flex;gap:9px;flex-wrap:wrap;align-items:center;}
.meta .pill{background:var(--paper);padding:2px 8px;border-radius:5px;border:1px solid var(--line);}
.meta .arr{color:var(--ok);font-weight:700;}.meta .sep{color:var(--line);}
.sit{font-size:13.5px;color:var(--muted);margin-top:8px;line-height:1.5;}
.tags{margin-top:9px;display:flex;gap:6px;flex-wrap:wrap;}
.tag{font-family:'JetBrains Mono',monospace;font-size:9.5px;font-weight:700;letter-spacing:0.06em;padding:3px 8px;border-radius:5px;text-transform:uppercase;}
.tag.warn{background:var(--amber-bg);color:var(--amber);}
.tag.bad{background:var(--red-bg);color:var(--red);}
.tag.info{background:#E5EEFF;color:#1E50C8;}
.tag.exist{background:#EAE6F7;color:#5B43B0;}
.tag.ok{background:#E4F2EA;color:var(--ok);}
.more{font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--faint);padding:6px 0 2px;font-style:italic;}
.empty{padding:14px 38px 20px;font-size:13.5px;color:var(--faint);font-style:italic;}
.section-banner{background:var(--ink-2);color:#fff;padding:22px 38px;border-top:1px solid rgba(255,255,255,0.08);}
.section-banner .h{font-family:'Fraunces',serif;font-size:23px;font-weight:600;letter-spacing:-0.01em;}
.section-banner .s{font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:0.14em;text-transform:uppercase;color:#8FA3B6;font-weight:600;margin-top:5px;}
.foot{background:var(--ink);color:#A9BBCB;border-radius:0 0 14px 14px;padding:26px 38px 30px;font-size:12.5px;line-height:1.65;}
.foot h4{font-family:'JetBrains Mono',monospace;font-size:10.5px;letter-spacing:0.18em;text-transform:uppercase;color:var(--glow);font-weight:600;margin-bottom:12px;}
.foot ul{list-style:none;}.foot li{padding:3px 0 3px 18px;position:relative;}.foot li::before{content:"›";position:absolute;left:0;color:var(--glow);}
.foot li b{color:#fff;font-weight:600;}
.foot .stamp{margin-top:18px;padding-top:14px;border-top:1px solid rgba(255,255,255,0.1);font-family:'JetBrains Mono',monospace;font-size:11px;color:#7C8E9E;}
.flag{text-align:center;font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:0.10em;text-transform:uppercase;color:var(--ok);margin:0 auto 14px;max-width:720px;}
"""

def _resolve_vars(css):
    """Inline CSS custom properties to literal values.

    Email clients (Gmail, Outlook) do not support `var()` / `:root` custom
    properties, so the email variant must resolve them to hex/literals.
    """
    import re
    m = re.search(r":root\s*\{([^}]*)\}", css)
    vars_map = {}
    if m:
        for decl in m.group(1).split(";"):
            decl = decl.strip()
            if decl.startswith("--") and ":" in decl:
                name, val = decl.split(":", 1)
                vars_map[name.strip()] = val.strip()
    css = re.sub(r"var\(\s*(--[\w-]+)\s*\)",
                 lambda mm: vars_map.get(mm.group(1).strip(), "inherit"), css)
    css = re.sub(r":root\s*\{[^}]*\}", "", css)
    return css


# Email-safe stylesheet: web-safe font stacks, no animation/glow effects that
# Gmail strips, and CSS variables resolved to literals. premailer then inlines
# these onto the elements before sending.
CSS_EMAIL = _resolve_vars(
    CSS
    .replace("'Hanken Grotesk',sans-serif",
             "Arial,Helvetica,sans-serif")
    .replace("'Fraunces',serif", "Georgia,'Times New Roman',serif")
    .replace("'JetBrains Mono',monospace",
             "'Courier New',Courier,monospace")
    .replace("box-shadow:0 0 10px var(--glow);", "")
)


def _build_parts(deals, run_date, css, web_fonts, flag_text, email_mode=False):
    """Shared HTML assembly for both the desktop and email variants.

    When email_mode is set, the markup is wrapped in a centered 600px table
    (with an MSO conditional table fallback) so Outlook-on-Windows renders the
    layout at native size instead of downscaling it, which is what blurs it.
    """
    # split active vs parked-nurture, then group + sort each by rep
    active = [d for d in deals if d.get("stage") not in PARKED]
    nurture = [d for d in deals if d.get("stage") in PARKED]

    def _group(items):
        by_rep = {o: [] for o in REPS}
        for d in items:
            by_rep.setdefault(d["owner"], [])
            by_rep[d["owner"]].append(d)
        for o in by_rep:
            by_rep[o].sort(key=lambda x: x["stale_days"], reverse=True)
        return by_rep

    deals_by_rep = _group(active)
    nurture_by_rep = _group(nurture)

    total_deals = len(active)
    total_nurture = len(nurture)
    total_arr = sum(_arr_int(d) for d in active)
    oldest = max((d["stale_days"] for d in active), default=0)
    run_str = fmt_date(run_date)

    head_fonts = (
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        '<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600;9..144,700&family=Hanken+Grotesk:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">'
        if web_fonts else ""
    )

    if email_mode:
        html_open = '<html lang="en" xmlns:o="urn:schemas-microsoft-com:office:office">'
        container_open = (
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#F4F2EB;width:100%;"><tr><td align="center">'
            '<!--[if mso]><table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0"><tr><td><![endif]-->'
            '<div class="email-container" style="max-width:600px;margin:0 auto;text-align:left;">'
        )
        container_close = '</div><!--[if mso]></td></tr></table><![endif]--></td></tr></table>'
    else:
        html_open = '<html lang="en">'
        container_open = '<div class="wrap">'
        container_close = '</div>'

    parts = []
    parts.append(f"""<!DOCTYPE html>{html_open}<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DSN Monday Pipeline Brief</title>
{head_fonts}
<style>{css}</style></head><body>
<div class="flag">● {flag_text} ●</div>
{container_open}
<div class="masthead"><div class="kicker"><span class="dot"></span>DSN Growth · Weekly Sales Action Brief</div>
<div class="title">The Monday <em>Pipeline</em> Brief</div>
<div class="dateline"><span>Week of <b>{run_str}</b></span><span>Source <b>live HubSpot</b></span><span>Scope <b>New + Existing (Cloud/Atlas)</b></span></div></div>
<div class="kpis">
<div class="kpi"><div class="lab">Stale deals</div><div class="num">{total_deals}</div></div>
<div class="kpi risk"><div class="lab">ARR at risk</div><div class="num">${total_arr/1000:.0f}<span class="unit">K</span></div></div>
<div class="kpi"><div class="lab">Longest silent</div><div class="num">{oldest}<span class="unit">d</span></div></div></div>
<div class="note"><span class="tag-l">How staleness is measured</span>
A deal is stale only when it has <b>no future next step booked</b> and <b>no genuine human touch in {DEFAULT_STALE_DAYS}+ days</b> — where a touch means a real note, call, email, meeting, completed task or manual stage move, not a workflow/automation bump. Deals worked recently through any of those drop off, even if a summary field reads cold; deals coasting on automation alone stay. Company context (PMS, specialty, locations) and the named contact are attached live from associated records.</div>
""")

    for o in REP_ORDER:
        ds = deals_by_rep.get(o, [])
        rep_arr = sum(_arr_int(d) for d in ds)
        shown = ds[:CAP]
        parts.append(f"""<div class="rep"><div class="rep-head"><div class="rep-name">{REPS[o]}</div>
<div class="rep-stats"><span><b>{len(ds)}</b> stale</span><span class="sep">·</span><span class="risk"><b>${rep_arr/1000:.0f}K</b> at risk</span></div></div>""")
        parts.append('<div class="subhead">Stale Deals · longest silent first</div>')
        if shown:
            body = "".join(deal_row(d) for d in shown)
            if len(ds) > CAP:
                body += f'<div class="more">+ {len(ds)-CAP} more stale deals not shown.</div>'
            parts.append('<div class="deals">' + body + '</div>')
        else:
            parts.append('<div class="empty">No stale deals this week.</div>')
        parts.append('</div>')

    # nurture section: parked deals, same staleness rule, displayed separately
    if nurture:
        parts.append(f"""<div class="section-banner"><div class="h">Nurture</div>
<div class="s">Parked deals · cold {DEFAULT_STALE_DAYS}+ days · {total_nurture} total</div></div>""")
        for o in REP_ORDER:
            ns = nurture_by_rep.get(o, [])
            if not ns:
                continue
            n_arr = sum(_arr_int(d) for d in ns)
            shown = ns[:CAP]
            parts.append(f"""<div class="rep"><div class="rep-head"><div class="rep-name">{REPS[o]}</div>
<div class="rep-stats"><span><b>{len(ns)}</b> nurture</span><span class="sep">·</span><span class="risk"><b>${n_arr/1000:.0f}K</b> at risk</span></div></div>""")
            parts.append('<div class="subhead">Nurture · longest silent first</div>')
            body = "".join(deal_row(d) for d in shown)
            if len(ns) > CAP:
                body += f'<div class="more">+ {len(ns)-CAP} more nurture deals not shown.</div>'
            parts.append('<div class="deals">' + body + '</div>')
            parts.append('</div>')

    parts.append(f"""<div class="foot"><h4>How this brief is built</h4><ul>
<li><b>Live data</b> from HubSpot portal {PORTAL}. Every title links to its real record. Nothing is fabricated.</li>
<li><b>Stale deal:</b> open deal in New Customer + Existing (Cloud/Atlas), no future next step, and no genuine human touch in {DEFAULT_STALE_DAYS}+ days.</li>
<li><b>Staleness clock:</b> the runner pulls each deal's real engagement records (notes, calls, emails, meetings, completed tasks) plus manual stage changes and clocks off the most recent. Workflow/automation events are ignored, so a deal worked through notes or stage moves no longer reads as cold, and one coasting on automation no longer reads as fresh.</li>
<li><b>Next-step gate:</b> any open task, scheduled meeting or call with a future date keeps a deal out of the brief — it is being worked.</li>
<li><b>Enrichment:</b> company PMS, specialty and location count, plus the primary contact, are attached from associated records. Only real, id-linked values are printed.</li>
<li><b>Nurture split:</b> deals parked in a nurture stage are moved out of the main stale list into their own section below it, scored on the same staleness rule.</li>
<li><b>Perio excluded:</b> deals whose associated company specialty is Periodontist are dropped from the brief entirely.</li>
<li><b>Tags:</b> No next step, No ARR, Incomplete record, 120+ days cold flag where the deal or its data needs work before outreach.</li>
<li><b>Display cap:</b> top {CAP} per rep by silence. Per-rep counts and ARR above reflect the full set.</li>
</ul><div class="stamp">Generated {run_date.strftime('%Y-%m-%d')} from live HubSpot · {total_deals} active stale · {total_nurture} nurture</div></div>
{container_close}</body></html>""")

    return "".join(parts)


DEFAULT_STALE_DAYS = 45


def _inject_email_head(html):
    """Add the MSO OfficeDocumentSettings block and the responsive media query.

    Injected *after* premailer runs so neither is inlined or stripped. The
    OfficeDocumentSettings (PixelsPerInch 96 + AllowPNG) stops Windows Outlook
    from scaling the layout up — the scaling is what makes the email look blurry.
    """
    mso = ('<!--[if gte mso 9]><xml><o:OfficeDocumentSettings>'
           '<o:AllowPNG/><o:PixelsPerInch>96</o:PixelsPerInch>'
           '</o:OfficeDocumentSettings></xml><![endif]-->')
    media = ('<style>@media only screen and (max-width:600px){'
             '.email-container{width:100%!important;max-width:100%!important;}}</style>')
    if "</title>" in html:
        return html.replace("</title>", "</title>" + mso + media, 1)
    if "</head>" in html:
        return html.replace("</head>", mso + media + "</head>", 1)
    return mso + media + html


def render(deals, run_date):
    """Desktop HTML with a <style> block and Google Fonts."""
    flag = (f"LIVE HubSpot data · portal {PORTAL} · {fmt_date(run_date)} · "
            f"staleness measured on real engagement records")
    return _build_parts(deals, run_date, CSS, web_fonts=True, flag_text=flag)


def render_email(deals, run_date):
    """Email-safe HTML: web-safe fonts, no animation, CSS inlined onto elements."""
    flag = (f"LIVE HubSpot data · portal {PORTAL} · {fmt_date(run_date)}")
    html = _build_parts(deals, run_date, CSS_EMAIL, web_fonts=False,
                        flag_text=flag, email_mode=True)
    try:
        import logging
        import cssutils
        cssutils.log.setLevel(logging.CRITICAL)  # silence CSS3-property warnings
        from premailer import transform
        html = transform(html, keep_style_tags=False, strip_important=False)
    except ImportError:
        # premailer not installed — return the email-CSS HTML with its <style>
        # block intact rather than failing the run.
        pass
    return _inject_email_head(html)
