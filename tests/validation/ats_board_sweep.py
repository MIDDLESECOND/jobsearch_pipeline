#!/usr/bin/env python3
"""Which ATS does each proven-AI-hiring law firm use — swept from their OWN sites.

Scope: the firms `ats_sweep_targets_20260820.json` lists (every firm that posted an
AI/innovation-titled role into jobs.db, measured 2026-08-20). The census (results/
ats_census_20260820.md) resolved 8 by hand in a browser; this scripts the same method —
read the ATS domain out of the firm's own careers page links — for the full set.

Two scripted shortcuts already failed and are NOT used (see the census): guessing Workday
tenants behind wildcard DNS, and Adzuna redirect_urls. This script instead fetches the
firm's own site (a page the firm publishes for exactly this audience) and parses hrefs.

**Controls run first and abort the sweep on failure** — the census's DNS probe returned
"host exists" for 22/22 fake tenants and nothing in its output could show the break:
  positive: wsgr's Workday root must classify as workday and yield its site name;
            careers-lw.icims.com must classify as an iCIMS portal.
  negative: a nonsense icims host must NOT classify; a nonsense Workday tenant's root
            must NOT yield a site.

Workday/iCIMS hits get one verification call against the actual list endpoint (role count
in the output). Findings land in results/ats_sweep_20260820.md. Read-only everywhere; the
only writes are the results file. Politely paced (~0.6s between requests).

Run:  python tests/validation/ats_board_sweep.py
"""
import json
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parents[1]))
import fetch  # noqa: E402 — the PRODUCTION readers verify hits (their honest UA is also
#               what iCIMS's WAF accepts; the browser-ish UA below is for firm sites only,
#               and iCIMS 405s it — measured 2026-08-20)
RESULTS = BASE / "results"
TARGETS = RESULTS / "ats_sweep_targets_20260820.json"
OUT = RESULTS / "ats_sweep_20260820.md"

UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
      "Accept": "text/html,application/xhtml+xml"}
DELAY = 0.6

# Firm-key -> primary web domain. Knowledge-derived (stable, verified by the fetch itself:
# a wrong domain simply yields no ATS link and lands in UNRESOLVED, never a false hit).
DOMAINS = {
    "mcdermott will & schulte": "mwe.com", "mcdermott will and emery": "mwe.com",
    "mcdermott will & emery": "mwe.com",
    "nixon peabody": "nixonpeabody.com",
    "kirkland & ellis": "kirkland.com",
    "gibson dunn": "gibsondunn.com",
    "covington & burling": "cov.com",
    "reed smith": "reedsmith.com",
    "stinson": "stinson.com", "stinson co": "stinson.com",
    "foley & lardner": "foley.com",
    "simpson thacher & bartlett": "stblaw.com", "simpson thacher": "stblaw.com",
    "white & case": "whitecase.com",
    "sidley austin": "sidley.com",
    "dechert": "dechert.com",
    "pillsbury winthrop shaw pittman": "pillsburylaw.com",
    "dla piper": "dlapiper.com",
    "norton rose fulbright": "nortonrosefulbright.com",
    "wilmerhale": "wilmerhale.com",
    "akerman": "akerman.com",
    "cooley": "cooley.com", "cooley corp.": "cooley.com",
    "ropes & gray": "ropesgray.com",
    "freshfields": "freshfields.com",
    "winston & strawn": "winston.com",
    "cravath, swaine & moore": "cravath.com",
    "shumaker, loop & kendrick": "shumaker.com",
    "littler": "littler.com",
    "shook, hardy & bacon": "shb.com",
    "alston & bird": "alston.com",
    "proskauer rose": "proskauer.com", "proskauer": "proskauer.com",
    "eversheds sutherland": "eversheds-sutherland.com",
    "choate, hall & stewart": "choate.com",
    "davis polk & wardwell": "davispolk.com",
    "dinsmore": "dinsmore.com", "dinsmore & shohl": "dinsmore.com",
    "faegre drinker": "faegredrinker.com",
    "davis wright tremaine": "dwt.com",
    "kilpatrick townsend & stockton": "ktslaw.com",
    "krieg devault": "kriegdevault.com",
    "kaufman dolowich": "kaufmandolowich.com",
    "reid collins & tsai": "reidcollins.com",
    "vinson & elkins": "velaw.com",
    "finnegan, henderson, farabow, garrett & dunner": "finnegan.com",
    "seward & kissel": "sewkis.com",
    "blank rome": "blankrome.com",
    "godfrey & kahn": "gklaw.com",
    "steptoe & johnson": "steptoe-johnson.com",   # the PLLC (the AI Solutions Analyst)
    "husch blackwell": "huschblackwell.com",
    "o'melveny & myers": "omm.com",
    "ogletree deakins": "ogletree.com",
    "mayer brown": "mayerbrown.com",
    "ashurst perkins coie": "perkinscoie.com", "perkins coie": "perkinscoie.com",
    "orrick, herrington & sutcliffe": "orrick.com",
    "lowenstein sandler": "lowenstein.com",
    "jackson walker": "jw.com",
    "king & spalding": "kslaw.com",
    "gould & ratner": "gouldratner.com",
    "baker mckenzie": "bakermckenzie.com",
    "taft stettinius & hollister": "taftlaw.com",
    "haynes and boone": "haynesboone.com",
    "hecker fink": "heckerfink.com",
    "freeman mathis & gary": "fmglaw.com",
    "fried frank": "friedfrank.com", "fried frank business services": "friedfrank.com",
    "willkie farr & gallagher": "willkie.com",
    "mintz": "mintz.com",
    "skadden, arps, slate, meagher & flom llp": "skadden.com",
    "hunton andrews kurth": "hunton.com",
    "munger, tolles & olson": "mto.com",
    "kean miller": "keanmiller.com",
    "nossaman": "nossaman.com",
    "mandarich law group": "mandarichlaw.com",
    "latham & watkins": "lw.com",                       # configured (positive-control tier)
    "mcguirewoods": "mcguirewoods.com",                  # configured
    "greenberg traurig": "gtlaw.com",                    # configured
    "holland & knight": "hklaw.com",                     # configured
    "wilson sonsini goodrich & rosati": "wsgr.com",      # configured
    "goodwin procter": "goodwinlaw.com",                 # configured
}
ALREADY = {"latham & watkins", "mcguirewoods", "greenberg traurig", "holland & knight",
           "wilson sonsini goodrich & rosati", "goodwin procter"}
SKIP = {"block"}   # normalization noise, not a law firm

WD_URL = re.compile(
    r"https?://([a-z0-9\-]+)\.(wd\d+)\.(?:myworkdayjobs|myworkdaysite)\.com"
    r"(?:/recruiting/[a-z0-9\-]+)?/(?:en-US/)?([A-Za-z0-9_\-]+)", re.I)
ICIMS_URL = re.compile(r"https?://([a-z0-9\-]+)\.icims\.com", re.I)
SIMPLE = [
    ("greenhouse", re.compile(r"(?:boards|job-boards)\.greenhouse\.io/([a-z0-9\-]+)|greenhouse\.io/embed/job_board\?for=([a-z0-9\-]+)", re.I)),
    ("lever", re.compile(r"jobs\.lever\.co/([a-zA-Z0-9\-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([a-zA-Z0-9\-%.]+)", re.I)),
]
OTHER = [("taleo", "taleo.net"), ("successfactors", "successfactors"), ("ukg", "ultipro"),
         ("dayforce", "dayforcehcm"), ("paycom", "paycomonline"), ("adp", "workforcenow.adp"),
         ("lawcruit", "lawcruit"), ("videsktop", "videsktop"), ("micronapps", "micronapps"),
         ("jazzhr", "applytojob"), ("smartrecruiters", "smartrecruiters"),
         ("workable", "workable.com"), ("bamboohr", "bamboohr"), ("paylocity", "paylocity"),
         ("clearcompany", "clearcompany"), ("phenom", "phenompeople")]


def get(url, timeout=15):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.geturl(), r.read(400_000).decode("utf-8", "replace")


def classify(html_text):
    """First supported-platform hit in the page's links, else first other-ATS name."""
    m = WD_URL.search(html_text)
    if m:
        return ("workday", f"{m.group(1).lower()}/{m.group(2).lower()}/{m.group(3)}")
    m = ICIMS_URL.search(html_text)
    if m and m.group(1).lower() not in ("www", "media", "cdn"):
        return ("icims", m.group(1).lower())
    for name, rx in SIMPLE:
        m = rx.search(html_text)
        if m:
            return (name, next(g for g in m.groups() if g))
    low = html_text.lower()
    for name, marker in OTHER:
        if marker in low:
            return (f"other:{name}", marker)
    return None


def verify_workday(slug):
    """One PRODUCTION-reader list call: does the composite slug actually serve postings?"""
    tenant, dc, site = slug.split("/")
    url = f"https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    p = fetch._workday_post(url, {"appliedFacets": {}, "limit": 20, "offset": 0,
                                  "searchText": ""})
    total = p.get("total")
    return total if isinstance(total, int) else len(p.get("jobPostings") or [])


def verify_icims(sub):
    """One PRODUCTION-reader listing call: does the portal serve job cards logged-out?"""
    body = fetch._icims_get(f"https://{sub}.icims.com/jobs/search?ss=1&in_iframe=1")
    if "iCIMS" not in body:
        raise ValueError("no portal branding")
    return len(set(re.findall(r"/jobs/(\d+)/", body)))


def controls():
    print("=== controls (abort on failure) ===")
    n = verify_workday("wsgr/wd503/WSGR")
    assert n > 0, "positive workday control returned no roles"
    print(f"  [ok] workday positive: wsgr serves {n} postings")
    n = verify_icims("careers-lw")
    assert n > 0, "positive icims control returned no cards"
    print(f"  [ok] icims positive: careers-lw serves {n} cards on page 1")
    try:
        verify_icims("zz-not-a-tenant-xq9")
        raise AssertionError("negative icims control CLASSIFIED — the check is broken")
    except AssertionError:
        raise
    except Exception as exc:
        print(f"  [ok] icims negative: nonsense host rejected ({type(exc).__name__})")
    try:
        n = verify_workday("zz-not-a-tenant-xq9/wd5/External")
        raise AssertionError(f"negative workday control served {n} — the check is broken")
    except AssertionError:
        raise
    except Exception as exc:
        print(f"  [ok] workday negative: nonsense tenant rejected ({type(exc).__name__})")
    print()


def probe_firm(key, domain):
    """Fetch up to 4 firm pages; return (platform, slug, evidence_url) or (None, reason, '')."""
    pages = [f"https://www.{domain}/careers", f"https://{domain}/careers",
             f"https://www.{domain}/"]
    last_err = "no ATS link found"
    followed = 0
    for url in pages:
        try:
            status, final, body = get(url)
        except Exception as exc:
            last_err = f"{type(exc).__name__}"
            continue
        hit = classify(body)
        if hit:
            return hit[0], hit[1], final
        # one level deeper: the most careers-ish link on the page
        if followed == 0:
            links = re.findall(r'href="(https?://[^"]+|/[^"]+)"', body)
            cand = [l for l in links if re.search(
                r"career|join[-_ ]?us|work[-_ ]?with|professional[-_ ]?staff|opportunit",
                l, re.I)]
            if cand:
                nxt = cand[0]
                if nxt.startswith("/"):
                    nxt = f"https://{final.split('/')[2]}{nxt}"
                followed = 1
                try:
                    time.sleep(DELAY)
                    _, final2, body2 = get(nxt)
                    hit = classify(body2)
                    if hit:
                        return hit[0], hit[1], final2
                except Exception as exc:
                    last_err = f"deep: {type(exc).__name__}"
        time.sleep(DELAY)
    return None, last_err, ""


def main():
    controls()
    targets = json.loads(TARGETS.read_text(encoding="utf-8"))
    seen_domains, rows = set(), []
    for t in targets:
        key = t["key"]
        if key in SKIP or key in ALREADY:
            continue
        domain = DOMAINS.get(key)
        if not domain:
            rows.append((key, t, None, "no domain mapped", ""))
            continue
        if domain in seen_domains:
            continue                       # name variant of a firm already probed
        seen_domains.add(domain)
        platform, slug, evidence = probe_firm(key, domain)
        count = ""
        if platform == "workday":
            try:
                count = verify_workday(slug)
            except Exception as exc:
                platform, slug = None, f"workday link found but CXS failed ({type(exc).__name__}: {slug})"
        elif platform == "icims":
            try:
                count = verify_icims(slug)
            except Exception as exc:
                platform, slug = None, f"icims link found but portal failed ({type(exc).__name__}: {slug})"
        rows.append((key, t, platform, slug, count))
        tag = platform or "UNRESOLVED"
        print("  %-38s %-14s %-42s %s" % (key[:37], tag, str(slug)[:41], count))
        time.sleep(DELAY)

    supported = [r for r in rows if r[2] in ("workday", "icims", "greenhouse", "lever", "ashby")]
    other = [r for r in rows if r[2] and r[2].startswith("other:")]
    unresolved = [r for r in rows if not r[2]]
    lines = ["# ATS sweep over proven-AI-hiring law firms — 2026-08-20",
             "",
             "Method + controls: see the module docstring of `ats_board_sweep.py`. Scope is",
             "every firm that posted an AI/innovation-titled role into jobs.db (the",
             "`ats_sweep_targets_20260820.json` measurement), minus the 6 already configured.",
             "",
             f"**Supported platform (ready to configure): {len(supported)}** · "
             f"other ATS (no reader): {len(other)} · unresolved: {len(unresolved)}",
             "", "## Supported", "",
             "| firm | platform | slug | roles served | strict-AI titles in DB |",
             "|---|---|---|---|---|"]
    for key, t, p, slug, count in sorted(supported, key=lambda r: -r[1]["strict"]):
        lines.append(f"| {key} | {p} | `{slug}` | {count} | {t['strict']} |")
    lines += ["", "## Other ATS (recorded, no reader built)", "",
              "| firm | platform marker | strict-AI titles |", "|---|---|---|"]
    for key, t, p, slug, _ in sorted(other, key=lambda r: -r[1]["strict"]):
        lines.append(f"| {key} | {p} | {t['strict']} |")
    lines += ["", "## Unresolved", "", "| firm | reason | strict-AI titles |", "|---|---|---|"]
    for key, t, p, slug, _ in sorted(unresolved, key=lambda r: -r[1]["strict"]):
        lines.append(f"| {key} | {slug} | {t['strict']} |")
    lines += ["", "Unresolved is **not** \"not on a supported platform\" — it is \"their site",
              "didn't yield a readable ATS link to this script\" (JS shells, bot walls, or a",
              "wrong domain guess). The census browser method still applies to them."]
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwritten: {OUT}")
    print(f"supported {len(supported)} / other {len(other)} / unresolved {len(unresolved)}")


if __name__ == "__main__":
    main()
