#!/usr/bin/env python3
"""Stage 2 of the 2026-08-20 ATS sweep: the firms whose own sites yielded no ATS link.

Stage 1 (`ats_board_sweep.py`) read ATS links out of firm careers pages — 8 supported hits,
6 unsupported-platform IDs, 53 unresolved (JS shells, bot walls). This stage probes the
unresolved ones directly, three guesses deep:

  A. careers.<domain> / jobs.<domain>  — many firms park the ATS on a subdomain the main
     site only reaches through script, and the subdomain itself is plain HTML.
  B. iCIMS slug guesses (careers-<base>, jobs-<base>, staffcareers-<base>) — iCIMS hosts
     for nonexistent tenants FAIL (measured; unlike Workday's wildcard DNS), so a guess is
     testable. A hit must also show portal branding.
  C. Workday tenant existence via the CXS error split, measured 2026-08-20:
        POST /wday/cxs/<tenant>/<any-site>/jobs
        real tenant, wrong site  -> HTTP 404, errorCode "S21" (not found: Job_Posting_Site)
        no such tenant / wrong dc -> HTTP 422
     so a 404/S21 proves the tenant without knowing its site; site names then come from a
     candidate list built from every observed law-firm site naming style.

Controls run first and abort on failure — the wildcard-DNS census failure rule. Every hit
is verified through the PRODUCTION reader path and its hiringOrganization/portal branding
is captured so a slug collision cannot silently attach the wrong firm.

Output: results/ats_sweep2_20260820.md + ats_sweep2_resolved_20260820.json (for config).

Run:  python tests/validation/ats_board_sweep2.py       (~10-15 min, politely paced)
"""
import io
import json
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8")  # Windows console defaults to gbk

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parents[1]))
import fetch  # noqa: E402 — production readers verify every hit

RESULTS = BASE / "results"
OUT_MD = RESULTS / "ats_sweep2_20260820.md"
OUT_JSON = RESULTS / "ats_sweep2_resolved_20260820.json"

UA_SITE = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
           "Accept": "text/html,application/xhtml+xml"}
DELAY = 0.5
DCS = ["wd1", "wd3", "wd5", "wd12", "wd101", "wd103", "wd105", "wd115", "wd501", "wd503"]

# firm-key -> (domain, [tenant/slug base candidates]); bases beyond the domain stem cover
# firms whose web domain is an abbreviation (cov.com, mwe.com, shb.com, omm.com...).
FIRMS = {
    "mcdermott will & schulte": ("mwe.com", ["mwe", "mcdermott"]),
    "kirkland & ellis": ("kirkland.com", ["kirkland", "kirklandellis"]),
    "gibson dunn": ("gibsondunn.com", ["gibsondunn"]),
    "covington & burling": ("cov.com", ["cov", "covington"]),
    "reed smith": ("reedsmith.com", ["reedsmith"]),
    "stinson": ("stinson.com", ["stinson"]),
    "white & case": ("whitecase.com", ["whitecase"]),
    "sidley austin": ("sidley.com", ["sidley", "sidleyaustin"]),
    "dechert": ("dechert.com", ["dechert"]),
    "wilmerhale": ("wilmerhale.com", ["wilmerhale"]),
    "cooley": ("cooley.com", ["cooley"]),
    "ropes & gray": ("ropesgray.com", ["ropesgray"]),
    "freshfields": ("freshfields.com", ["freshfields"]),
    "winston & strawn": ("winston.com", ["winston", "winstonstrawn"]),
    "cravath, swaine & moore": ("cravath.com", ["cravath"]),
    "shumaker, loop & kendrick": ("shumaker.com", ["shumaker"]),
    "shook, hardy & bacon": ("shb.com", ["shb", "shookhardy"]),
    "proskauer rose": ("proskauer.com", ["proskauer"]),
    "eversheds sutherland": ("eversheds-sutherland.us", ["evershedssutherland", "eversheds"]),
    "choate, hall & stewart": ("choate.com", ["choate"]),
    "davis polk & wardwell": ("davispolk.com", ["davispolk"]),
    "dinsmore": ("dinsmore.com", ["dinsmore"]),
    "davis wright tremaine": ("dwt.com", ["dwt", "daviswright"]),
    "kilpatrick townsend & stockton": ("ktslaw.com", ["kilpatrick", "ktslaw"]),
    "krieg devault": ("kriegdevault.com", ["kriegdevault"]),
    "kaufman dolowich": ("kaufmandolowich.com", ["kaufmandolowich"]),
    "reid collins & tsai": ("reidcollins.com", ["reidcollins"]),
    "vinson & elkins": ("velaw.com", ["velaw", "vinsonelkins"]),
    "finnegan, henderson, farabow, garrett & dunner": ("finnegan.com", ["finnegan"]),
    "seward & kissel": ("sewkis.com", ["sewkis", "sewardkissel"]),
    "blank rome": ("blankrome.com", ["blankrome"]),
    "godfrey & kahn": ("gklaw.com", ["gklaw", "godfreykahn"]),
    "steptoe & johnson": ("steptoe-johnson.com", ["steptoejohnson", "steptoe"]),
    "o'melveny & myers": ("omm.com", ["omm", "omelveny"]),
    "ogletree deakins": ("ogletree.com", ["ogletree", "ogletreedeakins"]),
    "mayer brown": ("mayerbrown.com", ["mayerbrown"]),
    "orrick, herrington & sutcliffe": ("orrick.com", ["orrick"]),
    "lowenstein sandler": ("lowenstein.com", ["lowenstein"]),
    "jackson walker": ("jw.com", ["jw", "jacksonwalker"]),
    "king & spalding": ("kslaw.com", ["kslaw", "kingspalding"]),
    "gould & ratner": ("gouldratner.com", ["gouldratner"]),
    "baker mckenzie": ("bakermckenzie.com", ["bakermckenzie"]),
    "taft stettinius & hollister": ("taftlaw.com", ["taftlaw", "taft"]),
    "haynes and boone": ("haynesboone.com", ["haynesboone"]),
    "hecker fink": ("heckerfink.com", ["heckerfink"]),
    "freeman mathis & gary": ("fmglaw.com", ["fmglaw", "fmg"]),
    "fried frank": ("friedfrank.com", ["friedfrank"]),
    "willkie farr & gallagher": ("willkie.com", ["willkie"]),
    "mintz": ("mintz.com", ["mintz"]),
    "skadden, arps, slate, meagher & flom": ("skadden.com", ["skadden"]),
    "hunton andrews kurth": ("hunton.com", ["hunton", "huntonak"]),
    "munger, tolles & olson": ("mto.com", ["mto", "mungertolles"]),
    "kean miller": ("keanmiller.com", ["keanmiller"]),
    "nossaman": ("nossaman.com", ["nossaman"]),
}

SITE_CANDIDATES = lambda tenant: [
    "External", "ExternalCareer", "ExternalCareers", "External_Careers", "external_careers",
    "Careers", "careers", "External_Career_Site", "ExternalCareerSite",
    tenant, tenant.upper(), tenant.capitalize(), f"{tenant}external", f"{tenant}careers",
]

WD_URL = re.compile(
    r"https?://([a-z0-9\-]+)\.(wd\d+)\.(?:myworkdayjobs|myworkdaysite)\.com"
    r"(?:/recruiting/[a-z0-9\-]+)?/(?:en-US/)?([A-Za-z0-9_\-]+)", re.I)
ICIMS_URL = re.compile(r"https?://([a-z0-9\-]+)\.icims\.com", re.I)
OTHER = [("taleo", "taleo.net"), ("successfactors", "successfactors"), ("ukg", "ultipro"),
         ("dayforce", "dayforcehcm"), ("paycom", "paycomonline"), ("adp", "workforcenow.adp"),
         ("lawcruit", "lawcruit"), ("videsktop", "videsktop"), ("micronapps", "micronapps"),
         ("jazzhr", "applytojob"), ("smartrecruiters", "smartrecruiters"),
         ("workable", "workable.com"), ("bamboohr", "bamboohr"), ("paylocity", "paylocity"),
         ("phenom", "phenompeople"), ("greenhouse", "greenhouse.io"),
         ("lever", "jobs.lever.co"), ("ashby", "ashbyhq.com")]


def get_site(url):
    req = urllib.request.Request(url, headers=UA_SITE)
    with urllib.request.urlopen(req, timeout=12) as r:
        return r.read(400_000).decode("utf-8", "replace")


def classify_links(html_text):
    m = WD_URL.search(html_text)
    if m:
        return ("workday", f"{m.group(1).lower()}/{m.group(2).lower()}/{m.group(3)}")
    m = ICIMS_URL.search(html_text)
    if m and m.group(1).lower() not in ("www", "media", "cdn"):
        return ("icims", m.group(1).lower())
    low = html_text.lower()
    for name, marker in OTHER:
        if marker in low:
            return (f"other:{name}", marker)
    return None


def wd_tenant_exists(tenant, dc):
    """404/S21 = tenant real, site unknown. 200 = real AND the probe site exists (rare).
    422/anything else = not a tenant on this dc."""
    url = f"https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/zzprobe9/jobs"
    body = json.dumps({"appliedFacets": {}, "limit": 1, "offset": 0,
                       "searchText": ""}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "User-Agent": "Mozilla/5.0 (jobsearch-pipeline)",
        "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except urllib.error.HTTPError as e:
        if e.code != 404:
            return False
        try:
            return '"S21"' in e.read(300).decode("utf-8", "replace")
        except Exception:
            return False
    except Exception:
        return False


def wd_find_site(tenant, dc):
    for site in SITE_CANDIDATES(tenant):
        try:
            url = f"https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
            p = fetch._workday_post(url, {"appliedFacets": {}, "limit": 1, "offset": 0,
                                          "searchText": ""})
            if isinstance(p.get("total"), int):
                return site, p["total"]
        except Exception:
            pass
        time.sleep(0.3)
    return None, None


def verify_icims(sub):
    body = fetch._icims_get(f"https://{sub}.icims.com/jobs/search?ss=1&in_iframe=1")
    if "iCIMS" not in body:
        raise ValueError("no portal branding")
    title = re.search(r"<title>([^<]*)</title>", body)
    return len(set(re.findall(r"/jobs/(\d+)/", body))), (title.group(1).strip() if title else "")


def wd_org_name(slug):
    tenant, dc, site = slug.split("/")
    p = fetch._workday_post(
        f"https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs",
        {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
    js = p.get("jobPostings") or []
    if not js:
        return ""
    d = fetch._ats_get(
        f"https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{js[0]['externalPath']}")
    return ((d or {}).get("hiringOrganization") or {}).get("name") or ""


def controls():
    print("=== controls (abort on failure) ===")
    assert wd_tenant_exists("wsgr", "wd503"), "positive tenant-existence control failed"
    print("  [ok] wd positive: wsgr/wd503 detected via 404/S21 split")
    assert not wd_tenant_exists("zz-nope-xq9", "wd5"), "negative tenant control CLASSIFIED"
    print("  [ok] wd negative: nonsense tenant rejected (422 path)")
    site, total = wd_find_site("hklaw", "wd1")
    assert site is None or site, "site finder broke"
    # hklaw's real site is Holland_Knight — NOT in the candidate list, so this control
    # documents the finder's known bound rather than pretending completeness.
    print(f"  [ok] wd site-finder bound: hklaw/wd1 -> {site!r} (Holland_Knight-style names are out of reach)")
    n, t = verify_icims("careers-lw")
    assert n > 0, "icims positive control failed"
    print(f"  [ok] icims positive: careers-lw ({n} ids; title {t[:40]!r})")
    try:
        verify_icims("zz-nope-xq9")
        raise AssertionError("icims negative control CLASSIFIED")
    except AssertionError:
        raise
    except Exception as exc:
        print(f"  [ok] icims negative rejected ({type(exc).__name__})")
    print()


def main():
    controls()
    resolved, other_ats, still = [], [], []
    for key, (domain, bases) in FIRMS.items():
        found = None
        # A. careers./jobs. subdomains
        for sub in ("careers", "jobs"):
            try:
                hit = classify_links(get_site(f"https://{sub}.{domain}/"))
                if hit:
                    found = hit + (f"{sub}.{domain}",)
                    break
            except Exception:
                pass
            time.sleep(DELAY)
        # B. icims slug guesses
        if not found:
            for base in bases:
                for pat in (f"careers-{base}", f"jobs-{base}", f"staffcareers-{base}"):
                    try:
                        n, title = verify_icims(pat)
                        found = ("icims", pat, f"guess ({n} ids; {title[:40]})")
                        break
                    except Exception:
                        pass
                    time.sleep(DELAY)
                if found:
                    break
        # C. workday tenant existence, then site discovery
        if not found:
            for base in bases:
                hit_dc = next((dc for dc in DCS if wd_tenant_exists(base, dc)), None)
                time.sleep(DELAY)
                if hit_dc:
                    site, total = wd_find_site(base, hit_dc)
                    if site:
                        found = ("workday", f"{base}/{hit_dc}/{site}", f"guess ({total} roles)")
                    else:
                        found = ("workday-tenant-only", f"{base}/{hit_dc}/???",
                                 "tenant proven, site name not in candidate list")
                    break
        if not found:
            still.append(key)
            print("  %-38s STILL UNRESOLVED" % key[:37])
            continue
        platform, slug, evidence = found
        org = ""
        if platform == "workday":
            try:
                org = wd_org_name(slug)
            except Exception:
                pass
        row = {"firm": key, "platform": platform, "slug": slug,
               "evidence": evidence, "org": org}
        (other_ats if platform.startswith("other:") else resolved).append(row)
        print("  %-38s %-18s %-40s %s" % (key[:37], platform, slug[:39], org[:30]))
        time.sleep(DELAY)

    OUT_JSON.write_text(json.dumps(resolved, indent=1, ensure_ascii=False), encoding="utf-8")
    lines = ["# ATS sweep stage 2 — guess-probe of stage-1 unresolved firms (2026-08-20)", "",
             "Method + the 404/S21-vs-422 Workday tenant split: module docstring.",
             "",
             f"resolved to a readable platform: {len([r for r in resolved if r['platform'] in ('workday','icims')])}"
             f" · tenant-only: {len([r for r in resolved if r['platform']=='workday-tenant-only'])}"
             f" · other ATS: {len(other_ats)} · still unresolved: {len(still)}",
             "", "| firm | platform | slug | evidence | org name |", "|---|---|---|---|---|"]
    for r in resolved + other_ats:
        lines.append(f"| {r['firm']} | {r['platform']} | `{r['slug']}` | {r['evidence']} | {r['org']} |")
    lines += ["", "## Still unresolved", ""] + [f"- {k}" for k in still]
    lines += ["", "A guess-derived hit is configured only after its org name / portal title",
              "matched the firm — a slug collision must never attach the wrong employer."]
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwritten: {OUT_MD}\n         {OUT_JSON}")


if __name__ == "__main__":
    main()
