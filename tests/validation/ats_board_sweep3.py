#!/usr/bin/env python3
"""Stage 3 of the 2026-08-20 ATS sweep: the rest of the AmLaw-100-scale roster.

Stages 1-2 covered every firm that had already posted an AI-titled role into jobs.db (the
proven hirers). This stage sweeps the large firms the DB has NOT yet caught hiring AI —
they enter as cheap watchers: a board with no matching titles costs one list call per run
and inserts nothing, and the 7-week measurement window is far shorter than law-firm hiring
cycles, so "no AI titles observed yet" is weak evidence of "not hiring".

Roster是 knowledge-derived (firm -> domain); a wrong domain fails safely into UNRESOLVED
(never a false hit — every hit is org-verified before configuring). Reuses stage 2's
probing machinery verbatim, including its positive/negative controls.

Run:  python tests/validation/ats_board_sweep3.py     (background-friendly, ~10 min)
"""
import io
import json
import sys
import time
from pathlib import Path

if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8")  # Windows console defaults to gbk

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE.parents[1]))

import ats_board_sweep2 as s2  # noqa: E402 — controls + probes + the S21/422 discriminator

OUT_MD = BASE / "results" / "ats_sweep3_20260820.md"
OUT_JSON = BASE / "results" / "ats_sweep3_resolved_20260820.json"

# firm -> (domain, [tenant/slug bases]). Large-firm roster not yet probed by stages 1-2.
ROSTER = {
    "jones day": ("jonesday.com", ["jonesday"]),
    "morgan lewis": ("morganlewis.com", ["morganlewis"]),
    "hogan lovells": ("hoganlovells.com", ["hoganlovells"]),
    "paul hastings": ("paulhastings.com", ["paulhastings"]),
    "akin gump": ("akingump.com", ["akingump", "akin"]),
    "debevoise & plimpton": ("debevoise.com", ["debevoise"]),
    "sullivan & cromwell": ("sullcrom.com", ["sullcrom"]),
    "weil gotshal": ("weil.com", ["weil"]),
    "milbank": ("milbank.com", ["milbank"]),
    "cleary gottlieb": ("clearygottlieb.com", ["cleary", "clearygottlieb"]),
    "paul weiss": ("paulweiss.com", ["paulweiss"]),
    "wachtell lipton": ("wlrk.com", ["wlrk", "wachtell"]),
    "quinn emanuel": ("quinnemanuel.com", ["quinnemanuel"]),
    "fenwick & west": ("fenwick.com", ["fenwick"]),
    "katten muchin": ("katten.com", ["katten"]),
    "sheppard mullin": ("sheppardmullin.com", ["sheppardmullin"]),
    "venable": ("venable.com", ["venable"]),
    "crowell & moring": ("crowell.com", ["crowell"]),
    "ballard spahr": ("ballardspahr.com", ["ballardspahr"]),
    "troutman pepper locke": ("troutman.com", ["troutman"]),
    "bryan cave leighton paisner": ("bclplaw.com", ["bclp", "bclplaw"]),
    "squire patton boggs": ("squirepattonboggs.com", ["squirepb", "squirepattonboggs"]),
    "dentons": ("dentons.com", ["dentons"]),
    "seyfarth shaw": ("seyfarth.com", ["seyfarth"]),
    "fox rothschild": ("foxrothschild.com", ["foxrothschild"]),
    "duane morris": ("duanemorris.com", ["duanemorris"]),
    "polsinelli": ("polsinelli.com", ["polsinelli"]),
    "nelson mullins": ("nelsonmullins.com", ["nelsonmullins"]),
    "k&l gates": ("klgates.com", ["klgates"]),
    "morrison foerster": ("mofo.com", ["mofo", "morrisonfoerster"]),
    "steptoe (llp)": ("steptoe.com", ["steptoe"]),
    "arentfox schiff": ("afslaw.com", ["afslaw", "arentfox"]),
    "wiley rein": ("wiley.law", ["wiley", "wileyrein"]),
    "cozen o'connor": ("cozen.com", ["cozen"]),
    "saul ewing": ("saul.com", ["saulewing", "saul"]),
    "buchanan ingersoll": ("bipc.com", ["bipc", "buchanan"]),
    "jackson lewis": ("jacksonlewis.com", ["jacksonlewis"]),
    "fisher phillips": ("fisherphillips.com", ["fisherphillips"]),
    "lewis brisbois": ("lewisbrisbois.com", ["lewisbrisbois"]),
    "holland & hart": ("hollandhart.com", ["hollandhart"]),
    "snell & wilmer": ("swlaw.com", ["swlaw", "snellwilmer"]),
    "vedder price": ("vedderprice.com", ["vedderprice"]),
    "barnes & thornburg": ("btlaw.com", ["btlaw", "barnesthornburg"]),
    "baker botts": ("bakerbotts.com", ["bakerbotts"]),
    "bracewell": ("bracewell.com", ["bracewell"]),
    "kramer levin": ("kramerlevin.com", ["kramerlevin"]),
    "cadwalader": ("cwt.com", ["cwt", "cadwalader"]),
    "cahill gordon": ("cahill.com", ["cahill"]),
    "kelley drye": ("kelleydrye.com", ["kelleydrye"]),
}


def main():
    s2.DELAY = 0.4
    s2.controls()
    resolved, other_ats, still = [], [], []
    for key, (domain, bases) in ROSTER.items():
        found = None
        try:
            hit = s2.classify_links(s2.get_site(f"https://www.{domain}/careers"))
            if not hit:
                hit = s2.classify_links(s2.get_site(f"https://careers.{domain}/"))
            if hit:
                found = hit
        except Exception:
            pass
        if not found:
            for base in bases:
                for pat in (f"careers-{base}", f"jobs-{base}"):
                    try:
                        n, title = s2.verify_icims(pat)
                        found = ("icims", pat)
                        break
                    except Exception:
                        time.sleep(s2.DELAY)
                if found:
                    break
        if not found:
            base = bases[0]
            hit_dc = next((dc for dc in s2.DCS if s2.wd_tenant_exists(base, dc)), None)
            if hit_dc:
                site, total = s2.wd_find_site(base, hit_dc)
                found = (("workday", f"{base}/{hit_dc}/{site}") if site else
                         ("workday-tenant-only", f"{base}/{hit_dc}/???"))
        if not found:
            still.append(key)
            print("  %-32s STILL UNRESOLVED" % key[:31])
            continue
        platform, slug = found[0], found[1]
        org = ""
        if platform == "workday":
            try:
                org = s2.wd_org_name(slug)
            except Exception:
                platform = "workday-tenant-only"
        row = {"firm": key, "platform": platform, "slug": slug, "org": org}
        (other_ats if platform.startswith("other:") else resolved).append(row)
        print("  %-32s %-20s %-42s %s" % (key[:31], platform, slug[:41], org[:28]))
        time.sleep(s2.DELAY)

    OUT_JSON.write_text(json.dumps(resolved, indent=1, ensure_ascii=False), encoding="utf-8")
    lines = ["# ATS sweep stage 3 — AmLaw-scale roster beyond the proven AI hirers (2026-08-20)",
             "", "Watcher rationale + method: module docstring; probes from ats_board_sweep2.",
             "", f"resolved: {len(resolved)} · other ATS: {len(other_ats)} · unresolved: {len(still)}",
             "", "| firm | platform | slug | org |", "|---|---|---|---|"]
    for r in resolved + other_ats:
        lines.append(f"| {r['firm']} | {r['platform']} | `{r['slug']}` | {r['org']} |")
    lines += ["", "## Unresolved", ""] + [f"- {k}" for k in still]
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwritten: {OUT_MD}")


if __name__ == "__main__":
    main()
