"""Probe every configured iCIMS tenant for the inert cookie-message template.

The evidence instrument behind `fetch._icims_interstitial`. On 2026-08-24 a bare
`"Please Enable Cookies" in body` test took all 12 configured iCIMS boards dark at once,
because the portal ships that message on every HEALTHY page inside a display:none template
div. The detector that replaced it counts message copies against what the page's inert
templates actually contain, and its correctness rests on a claim about live markup: that
the template is byte-identical across tenants and across platform builds.

That claim is what this script re-measures. Run it after any iCIMS breakage, before
touching the detector, and when a board goes quiet:

    python tests/validation/icims_template_probe.py

It is READ-ONLY in every direction — one logged-out GET per tenant (the same request
`_icims_get` makes), no database, no writes outside tests/validation/results/. It reports
per tenant: the platform build serving it, whether the template is present and byte-equal
to the reference capture, how many message copies the page carries, how many the templates
explain, the detector's verdict, and the listed-card count. Any tenant whose verdict is
True is a board that is about to go dark; any tenant whose template differs byte-wise is
the early warning that the detector's tolerances need re-reading.

First recorded run, 2026-08-24: 12/12 tenants healthy, template byte-identical on all of
them, nine serving platform_183.4.0.260723 and careers-ropesgray/careers-willkie/
careers-ebglaw serving 186.3.1 — two builds, one markup.
"""
import datetime as dt
import io
import os
import sys
import time
import urllib.request
from http.cookiejar import CookieJar

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import yaml  # noqa: E402

import fetch  # noqa: E402

# Verbatim from the 2026-08-24 careers-cravath capture (cookieless 200, one occurrence).
REFERENCE_TEMPLATE = (
    '<div id="iCIMS_NoCookiesMessage" class="iCIMS_ErrorMsg iCIMS_ErrorMessage '
    'iCIMS_NoCookies" style="display: none">\n'
    '<div class="iCIMS_ErrorMsgTitle">Please Enable Cookies to Continue</div>\n'
    'Please enable cookies in your browser to experience all the personalized features '
    'of this site, including the ability to apply for a job.</div>')

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RESULTS = os.path.join(ROOT, "tests", "validation", "results")


def main():
    cfg = yaml.safe_load(io.open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
    tenants = [c["slug"] for c in cfg["settings"]["ats"]["companies"]
               if c.get("board") == "icims"]
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(CookieJar()))

    lines, dark, drifted = [], [], []
    for slug in tenants:
        url = f"https://{slug}.icims.com/jobs/search?ss=1&in_iframe=1&pr=0"
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (jobsearch-pipeline)", "Accept": "text/html"})
            with opener.open(req, timeout=30) as resp:
                body = resp.read().decode("utf-8", "replace")
                build = resp.headers.get("x-icims-build", "?")
        except Exception as exc:                      # noqa: BLE001 — one tenant, not the run
            lines.append(f"{slug:28} ERROR {type(exc).__name__}: {exc}")
            continue
        verdict = fetch._icims_interstitial(body)
        # Newlines are the one capture artifact that differs harmlessly between transports.
        exact = REFERENCE_TEMPLATE.replace("\r\n", "\n") in body.replace("\r\n", "\n")
        if verdict:
            dark.append(slug)
        if not exact:
            drifted.append(slug)
        lines.append(
            f"{slug:28} build={build:9} template={'exact' if exact else 'DIFFERS':7} "
            f"msgs={body.count(fetch._ICIMS_COOKIE_MSG)} "
            f"explained={fetch._icims_explained_copies(body)} "
            f"cards={body.count('iCIMS_JobCardItem'):>3} interstitial={verdict}")
        time.sleep(1.5)

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    report = [f"iCIMS cookie-template probe — {stamp}", ""] + lines + [
        "",
        f"tenants probed: {len(tenants)}",
        f"would go dark (detector says wall): {len(dark)} {dark or ''}",
        f"template drifted from the reference capture: {len(drifted)} {drifted or ''}",
    ]
    os.makedirs(RESULTS, exist_ok=True)
    out = os.path.join(RESULTS, f"icims_template_probe_{stamp}.txt")
    with io.open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(report) + "\n")
    print("\n".join(report))
    print(f"\nwritten to {out}")
    return 1 if (dark or drifted) else 0


if __name__ == "__main__":
    sys.exit(main())
