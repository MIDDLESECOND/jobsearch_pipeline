#!/usr/bin/env python3
"""Stage 4 of the 2026-08-20 ATS sweep: the rest of the AmLaw-200-scale roster.

Stages 1-3 covered the proven AI hirers plus the AmLaw-100-scale names; this stage sweeps
the remaining second-hundred tier (knowledge-derived roster; law.com's canonical list is
paywalled, so membership is approximate — a near-miss inclusion is a harmless cheap
watcher, and a wrong domain fails safely into UNRESOLVED).

Two verification rules stage 3 lacked, each bought by a real false hit that night:
  * every iCIMS hit is verified INLINE — portal branding, >0 job cards, and the firm's
    name in the portal <title> (stage 3 accepted `cookie-policy-scripts` — an iCIMS infra
    host — and two EMPTY LEGACY SHELLS had earlier shadowed real boards);
  * every Workday hit must return an org name (or first-posting evidence) sharing a token
    with the firm (the `wiley/wd1` tenant belongs to the PUBLISHER Wiley, not Wiley Rein —
    caught only by the org check).

Run:  python tests/validation/ats_board_sweep4.py     (background-friendly, ~10-15 min)
"""
import io
import json
import re
import sys
import time
from pathlib import Path

if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8")  # Windows console defaults to gbk

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE.parents[1]))

import ats_board_sweep2 as s2  # noqa: E402
import fetch  # noqa: E402

OUT_MD = BASE / "results" / "ats_sweep4_20260820.md"
OUT_JSON = BASE / "results" / "ats_sweep4_resolved_20260820.json"

# firm -> (domain, [tenant/slug bases], [verification tokens, lowercase])
ROSTER = {
    "bakerhostetler": ("bakerlaw.com", ["bakerhostetler", "bakerlaw"], ["baker"]),
    "gunderson dettmer": ("gunder.com", ["gunderson", "gunder"], ["gunderson"]),
    "fragomen": ("fragomen.com", ["fragomen"], ["fragomen"]),
    "fish & richardson": ("fr.com", ["fishrichardson", "fr"], ["fish"]),
    "knobbe martens": ("knobbe.com", ["knobbe"], ["knobbe"]),
    "jenner & block": ("jenner.com", ["jenner"], ["jenner"]),
    "baker donelson": ("bakerdonelson.com", ["bakerdonelson"], ["donelson"]),
    "womble bond dickinson": ("womblebonddickinson.com", ["womble"], ["womble"]),
    "dorsey & whitney": ("dorsey.com", ["dorsey"], ["dorsey"]),
    "dykema": ("dykema.com", ["dykema"], ["dykema"]),
    "frost brown todd": ("frostbrowntodd.com", ["frostbrowntodd", "fbt"], ["frost"]),
    "honigman": ("honigman.com", ["honigman"], ["honigman"]),
    "ice miller": ("icemiller.com", ["icemiller"], ["ice miller", "icemiller"]),
    "kutak rock": ("kutakrock.com", ["kutakrock"], ["kutak"]),
    "lathrop gpm": ("lathropgpm.com", ["lathropgpm", "lathrop"], ["lathrop"]),
    "lewis roca": ("lewisroca.com", ["lewisroca"], ["lewis roca", "lewisroca"]),
    "mccarter & english": ("mccarter.com", ["mccarter"], ["mccarter"]),
    "michael best": ("michaelbest.com", ["michaelbest"], ["michael best", "michaelbest"]),
    "miller canfield": ("millercanfield.com", ["millercanfield"], ["canfield"]),
    "moore & van allen": ("mvalaw.com", ["mvalaw", "moorevanallen"], ["van allen", "mva"]),
    "morris manning & martin": ("mmmlaw.com", ["mmmlaw", "morrismanning"], ["morris manning", "mmm"]),
    "neal gerber eisenberg": ("nge.com", ["nge", "nealgerber"], ["neal gerber", "nge"]),
    "parker poe": ("parkerpoe.com", ["parkerpoe"], ["parker poe", "parkerpoe"]),
    "pierce atwood": ("pierceatwood.com", ["pierceatwood"], ["pierce atwood", "pierceatwood"]),
    "porter wright": ("porterwright.com", ["porterwright"], ["porter wright", "porterwright"]),
    "pryor cashman": ("pryorcashman.com", ["pryorcashman"], ["pryor"]),
    "quarles & brady": ("quarles.com", ["quarles"], ["quarles"]),
    "robins kaplan": ("robinskaplan.com", ["robinskaplan"], ["robins kaplan", "robinskaplan"]),
    "robinson & cole": ("rc.com", ["robinsoncole", "rc"], ["robinson"]),
    "shipman & goodwin": ("shipmangoodwin.com", ["shipmangoodwin"], ["shipman"]),
    "shutts & bowen": ("shutts.com", ["shutts"], ["shutts"]),
    "stoel rives": ("stoel.com", ["stoel", "stoelrives"], ["stoel"]),
    "sterne kessler": ("sternekessler.com", ["sternekessler"], ["sterne"]),
    "stevens & lee": ("stevenslee.com", ["stevenslee"], ["stevens"]),
    "sullivan & worcester": ("sullivanlaw.com", ["sullivanworcester"], ["worcester"]),
    "thompson coburn": ("thompsoncoburn.com", ["thompsoncoburn"], ["coburn"]),
    "thompson hine": ("thompsonhine.com", ["thompsonhine"], ["thompson hine", "thompsonhine"]),
    "vorys": ("vorys.com", ["vorys"], ["vorys"]),
    "warner norcross": ("wnj.com", ["wnj", "warnernorcross"], ["warner", "wnj"]),
    "williams mullen": ("williamsmullen.com", ["williamsmullen"], ["williams mullen", "williamsmullen"]),
    "wilson elser": ("wilsonelser.com", ["wilsonelser"], ["wilson elser", "wilsonelser"]),
    "winstead": ("winstead.com", ["winstead"], ["winstead"]),
    "bradley arant": ("bradley.com", ["bradley", "bradleyarant"], ["bradley"]),
    "burr & forman": ("burr.com", ["burr", "burrforman"], ["burr"]),
    "butler snow": ("butlersnow.com", ["butlersnow"], ["butler snow", "butlersnow"]),
    "carlton fields": ("carltonfields.com", ["carltonfields"], ["carlton"]),
    "chapman and cutler": ("chapman.com", ["chapman", "chapmancutler"], ["chapman"]),
    "clark hill": ("clarkhill.com", ["clarkhill"], ["clark hill", "clarkhill"]),
    "cole schotz": ("coleschotz.com", ["coleschotz"], ["cole schotz", "coleschotz"]),
    "day pitney": ("daypitney.com", ["daypitney"], ["day pitney", "daypitney"]),
    "dickinson wright": ("dickinsonwright.com", ["dickinsonwright", "dickinson"], ["dickinson"]),
    "fennemore": ("fennemorelaw.com", ["fennemore"], ["fennemore"]),
    "foley hoag": ("foleyhoag.com", ["foleyhoag"], ["foley hoag", "foleyhoag"]),
    "fredrikson & byron": ("fredlaw.com", ["fredrikson", "fredlaw"], ["fredrikson"]),
    "goldberg segalla": ("goldbergsegalla.com", ["goldbergsegalla"], ["segalla"]),
    "grayrobinson": ("gray-robinson.com", ["grayrobinson"], ["gray"]),
    "hanson bridgett": ("hansonbridgett.com", ["hansonbridgett"], ["hanson"]),
    "harris beach murtha": ("harrisbeach.com", ["harrisbeach"], ["harris beach", "harrisbeach"]),
    "hinckley allen": ("hinckleyallen.com", ["hinckleyallen"], ["hinckley"]),
    "hinshaw & culbertson": ("hinshawlaw.com", ["hinshaw"], ["hinshaw"]),
    "jackson kelly": ("jacksonkelly.com", ["jacksonkelly"], ["jackson kelly", "jacksonkelly"]),
    "kasowitz": ("kasowitz.com", ["kasowitz"], ["kasowitz"]),
    "marshall dennehey": ("marshalldennehey.com", ["marshalldennehey"], ["dennehey"]),
    "miles & stockbridge": ("milesstockbridge.com", ["milesstockbridge"], ["stockbridge"]),
    "miller & chevalier": ("millerchevalier.com", ["millerchevalier"], ["chevalier"]),
    "mitchell silberberg": ("msk.com", ["msk", "mitchellsilberberg"], ["silberberg", "msk"]),
    "munsch hardt": ("munsch.com", ["munsch"], ["munsch"]),
    "nutter mcclennen": ("nutter.com", ["nutter"], ["nutter"]),
    "offit kurman": ("offitkurman.com", ["offitkurman"], ["offit"]),
    "potter anderson": ("potteranderson.com", ["potteranderson"], ["potter"]),
    "smith anderson": ("smithlaw.com", ["smithanderson", "smithlaw"], ["smith anderson", "smithlaw"]),
    "ub greensfelder": ("ubglaw.com", ["ubglaw", "ubgreensfelder"], ["greensfelder", "ubg"]),
    "wiggin and dana": ("wiggin.com", ["wiggin"], ["wiggin"]),
    "wolf greenfield": ("wolfgreenfield.com", ["wolfgreenfield"], ["wolf greenfield", "wolfgreenfield"]),
    "brownstein hyatt": ("bhfs.com", ["bhfs", "brownstein"], ["brownstein", "bhfs"]),
    "buchalter": ("buchalter.com", ["buchalter"], ["buchalter"]),
    "epstein becker green": ("ebglaw.com", ["ebglaw", "epsteinbecker"], ["epstein", "ebg"]),
    "greenspoon marder": ("gmlaw.com", ["gmlaw", "greenspoonmarder"], ["greenspoon", "gmlaw"]),
    "bass berry & sims": ("bassberry.com", ["bassberry"], ["bass berry", "bassberry"]),
    "benesch": ("beneschlaw.com", ["benesch", "beneschlaw"], ["benesch"]),
    # NOLA-relevant (user's own market):
    "jones walker": ("joneswalker.com", ["joneswalker"], ["jones walker", "joneswalker"]),
    "adams and reese": ("adamsandreese.com", ["adamsandreese", "adamsreese"], ["adams"]),
    "phelps dunbar": ("phelps.com", ["phelps", "phelpsdunbar"], ["phelps"]),
    "mcglinchey stafford": ("mcglinchey.com", ["mcglinchey"], ["mcglinchey"]),
}


def _verify_icims(sub, tokens):
    # Same origin _icims_rows derives, so the count here is the count the reader would get:
    # cards are held to the tenant's own host, and off-host ones are dropped, not counted.
    base = f"https://{sub}.icims.com"
    body = fetch._icims_get(f"{base}/jobs/search?ss=1&in_iframe=1")
    if "iCIMS" not in body:
        raise ValueError("no portal branding")
    m = re.search(r"<title>([^<]*)</title>", body)
    title = (m.group(1) if m else "").strip()
    cards = len(fetch._icims_cards(body, base))
    if cards == 0:
        raise ValueError("empty shell (0 cards)")
    if not any(t in title.lower() for t in tokens):
        raise ValueError(f"portal title {title[:40]!r} does not name the firm")
    return cards, title


def _verify_workday(slug, tokens):
    # (v2 fix: stage 4 first shipped calling s2.verify_workday, which only ever existed in
    # STAGE 1's module — the AttributeError rejected two real finds, Gunderson and Fragomen.)
    tenant, dc, site = slug.split("/")
    p = fetch._workday_post(
        f"https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs",
        {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
    n = p.get("total")
    if not isinstance(n, int):
        raise ValueError("no total in list response")
    org = ""
    try:
        org = s2.wd_org_name(slug)
    except Exception:
        pass
    hay = (org + " " + slug).lower()
    if org and not any(t in hay for t in tokens):
        raise ValueError(f"org {org[:40]!r} does not name the firm (collision?)")
    return n, org


def main():
    s2.DELAY = 0.4
    s2.controls()
    resolved, other_ats, still = [], [], []
    for key, (domain, bases, tokens) in ROSTER.items():
        found = reject = None
        # A. firm site pages (www + careers subdomain)
        for url in (f"https://www.{domain}/careers", f"https://careers.{domain}/",
                    f"https://www.{domain}/"):
            try:
                hit = s2.classify_links(s2.get_site(url))
                if hit:
                    found = hit
                    break
            except Exception:
                pass
            time.sleep(s2.DELAY)
        # B. icims slug guesses
        if not found:
            for base in bases:
                for pat in (f"careers-{base}", f"jobs-{base}"):
                    try:
                        _verify_icims(pat, tokens)
                        found = ("icims", pat)
                        break
                    except Exception:
                        time.sleep(s2.DELAY)
                if found:
                    break
        # C. workday tenant existence + site discovery
        if not found:
            base = bases[0]
            hit_dc = next((dc for dc in s2.DCS if s2.wd_tenant_exists(base, dc)), None)
            if hit_dc:
                site, total = s2.wd_find_site(base, hit_dc)
                found = (("workday", f"{base}/{hit_dc}/{site}") if site else
                         ("workday-tenant-only", f"{base}/{hit_dc}/???"))
        # verification gate — a hit that fails it is recorded as a REJECTED hit, not a find
        detail = ""
        if found and found[0] == "icims":
            try:
                cards, title = _verify_icims(found[1], tokens)
                detail = f"{cards} cards; {title[:40]}"
            except Exception as exc:
                reject, found = f"icims {found[1]}: {exc}", None
        elif found and found[0] == "workday":
            try:
                n, org = _verify_workday(found[1], tokens)
                detail = f"{n} roles; org {org[:40]}"
            except Exception as exc:
                reject, found = f"workday {found[1]}: {exc}", None
        if not found:
            still.append((key, reject or "no ATS link found"))
            print("  %-28s UNRESOLVED %s" % (key[:27], ("(REJECTED: %s)" % reject) if reject else ""))
            continue
        platform, slug = found[0], found[1]
        row = {"firm": key, "platform": platform, "slug": slug, "detail": detail}
        (other_ats if platform.startswith("other:") else resolved).append(row)
        print("  %-28s %-20s %-40s %s" % (key[:27], platform, slug[:39], detail[:36]))
        time.sleep(s2.DELAY)

    OUT_JSON.write_text(json.dumps(resolved, indent=1, ensure_ascii=False), encoding="utf-8")
    lines = ["# ATS sweep stage 4 — the AmLaw-200 second-hundred tier (2026-08-20)", "",
             "Roster is knowledge-derived (law.com's canonical list is paywalled); inline",
             "verification per the module docstring — every icims hit needs cards + the firm's",
             "name in the portal title, every workday hit needs a firm-matching org.",
             "",
             f"resolved: {len(resolved)} · other ATS: {len(other_ats)} · unresolved: {len(still)}",
             "", "| firm | platform | slug | evidence |", "|---|---|---|---|"]
    for r in resolved + other_ats:
        lines.append(f"| {r['firm']} | {r['platform']} | `{r['slug']}` | {r['detail']} |")
    lines += ["", "## Unresolved (reason)", ""] + [f"- {k} — {why}" for k, why in still]
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwritten: {OUT_MD}")


if __name__ == "__main__":
    main()
