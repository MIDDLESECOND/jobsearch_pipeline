# Open-source inspiration and provenance

This project studies other open-source job-search tools for product ideas. The projects below
informed feature selection and interaction boundaries; source code, assets, schemas, migrations,
and documentation text from the projects listed below were **not** copied or adapted into this
repository. The implementation here was written independently for this project's local SQLite
data model and chain-scoped workflow.

The license column records the license status observed at the exact revision reviewed on
2026-08-06. It is provenance for the design research, not a claim that upstream code is bundled,
and it is not a dependency-license inventory. “No license detected” means no license file or SPDX
identifier was present at that revision; normal copyright restrictions therefore remain relevant.

## Sources and adopted boundaries

| Project | Official source and reviewed revision | License | What informed this project | Deliberately not adopted |
|---|---|---|---|---|
| Job Trail | [`aplaza1/job-trail`](https://github.com/aplaza1/job-trail) at [`e04c1816ffd25c6d29f114a1b1733e9cc2ab1ec0`](https://github.com/aplaza1/job-trail/commit/e04c1816ffd25c6d29f114a1b1733e9cc2ab1ec0) | No license file or SPDX identifier detected | Making upcoming interview dates visible and associating interview plans with an application reinforced the local, chain-scoped interview schedule and Upcoming interviews queue. | AWS/Cognito deployment, hosted accounts, public sharing, and external calendar writes. |
| Applic | [`rpunia29/applic`](https://github.com/rpunia29/applic) at [`5c35ce8a7303773a30bf40430267a7500a22d99a`](https://github.com/rpunia29/applic/commit/5c35ce8a7303773a30bf40430267a7500a22d99a) | [MIT](https://github.com/rpunia29/applic/blob/5c35ce8a7303773a30bf40430267a7500a22d99a/LICENSE) | Bookmarked jobs informed the explicit **Star role** shortlist. Its interview scheduling and document-management adjacency also reinforced keeping plans beside the existing local application evidence. | Authentication, hosted PostgreSQL, notifications, and its external upload/deployment stack. |
| Candidex | [`sebai-dhia/candidex`](https://github.com/sebai-dhia/candidex) at [`dd180461f7125d38eb2ba3ab94d5c76c8179f3b2`](https://github.com/sebai-dhia/candidex/commit/dd180461f7125d38eb2ba3ab94d5c76c8179f3b2) | [MIT](https://github.com/sebai-dhia/candidex/blob/dd180461f7125d38eb2ba3ab94d5c76c8179f3b2/LICENSE) | In-context job tracking, response/interview-date visibility, and user-controlled storage reinforced the local review UI, explicit manual intake for externally found roles, and a user-controlled portable-data boundary. | Browser-extension injection, Google OAuth, Google Sheets as the authoritative store, automatic capture from visited pages, and scraping a manually supplied URL. |
| JobSync | [`Gsync/jobsync`](https://github.com/Gsync/jobsync) at [`06d994b248b73ea57395254f5e3ccce582388ec5`](https://github.com/Gsync/jobsync/commit/06d994b248b73ea57395254f5e3ccce582388ec5) | [MIT](https://github.com/Gsync/jobsync/blob/06d994b248b73ea57395254f5e3ccce582388ec5/LICENSE) | First-class tasks, activity management, upcoming work, and explicit review of discovered jobs informed local role next actions, the bounded unified Activity view, and the human-review boundary around suggestions. | Accounts, MCP/AI automation, general project management, time tracking, notifications, a separate inferred activity store, and a hosted service stack. |
| Jobtra | [`CU1KNIGHT/Jobtra`](https://github.com/CU1KNIGHT/Jobtra) at [`9671e163e8977e2f980287cc85414ff36c98675b`](https://github.com/CU1KNIGHT/Jobtra/commit/9671e163e8977e2f980287cc85414ff36c98675b) | [MIT](https://github.com/CU1KNIGHT/Jobtra/blob/9671e163e8977e2f980287cc85414ff36c98675b/LICENSE) | Treating SQLite and uploaded documents as one backup unit reinforced the verified evidence-bundle command. | Docker-volume assumptions, automatic restore, accounts, email credentials/sync, and LLM-driven status changes. |

These mappings describe influence at the product-pattern level. They do not imply that every
detail of a resulting feature came from the named project.

The source-influenced feature bundle audited for this record is commit `6714f91`. A separate
security change, commit `94e2317` on the local security-review branch, is a project-specific
defensive fix that redacts Adzuna query-string credentials from logged request failures. No
open-source feature implementation was identified as its inspiration. Its commit metadata
separately records the AI-assisted authorship used for that change.

## Independently designed project-specific behavior

The following parts were developed from this repository's requirements and review findings, not
from an identified upstream implementation:

- cross-source duplicate candidate matching, versioned dismiss/restore tombstones, exact
  company/title constraints, and root/version concurrency guards;
- canonical-at-write/current-chain ownership across duplicate merge and unlink operations;
- role notes implemented by exposing the existing append-only `app_events` history;
- the CSV export schema, chain-deduped summary rules, and spreadsheet-formula neutralization;
- star tombstones, monotonic versions, and stale-tab/ABA protection;
- the separation between scheduled interview plans and completed interview outcome events;
- per-target pipeline run evidence, privacy-minimized failure categories, and the descriptive
  first-storage/current-chain search-yield model;
- the draft/confirmed lifecycle, reusable prep-entry bank, versioned role-relevance links, and
  least-disclosure rule that keeps private interview stories out of outreach briefs; and
- opaque, bounded, checksum-verified JD evidence comparison and its strict separation from live
  page checks or semantic interpretations of employer intent.

## Maintenance rule

When a future feature is materially informed by an external project, update this file before the
feature is pushed. Record the official repository, the exact reviewed revision, the observed
license, the adopted product idea, the rejected boundary, and whether any code or assets were
copied or adapted. If code or assets ever are reused, replace the general no-copy statement above
with file-level attribution and preserve every applicable license notice.
