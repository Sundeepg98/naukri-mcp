# AmbitionBox endpoint census and build verdict

Date: 2026-08-24
Repo: `D:\workspace\projects\job-hunting\mcp-servers\naukri` at HEAD `c3550d3`
Scope: value judgment only. No server code written, nothing committed, live server (PID 43228) untouched.

---

## 0. The count -- the brief's premise does not match disk

The task named "36 AmbitionBox community endpoints". **No probing report on disk names 36 of
anything.** The measured numbers are:

| Reading of "community endpoints" | Measured count | Source |
|---|---|---|
| The `community-services` gateway, literally | **4** | `probing/ambitionbox-browser-probe-report.md` rows 29-32; `probing/ambitionbox-rest-deep-report.md` sections 12a-12d |
| The whole AmbitionBox catalogue (AB being the employee-community site) | **42** in the canonical dedup table | `probing/ambitionbox-browser-probe-report.md` section 3, rows 1-42 |
| Same, plus endpoints documented outside that table | **44** | the 42 above, plus the 2 named below |

The three reports also print three different self-reported totals for the same catalogue: "35+"
(`ambitionbox-rest-deep-report.md`), "40+" (`company-ecosystem-report.md`), 42
(`ambitionbox-browser-probe-report.md`). None is 36.

**The number used in this document is 44** -- the deduplicated union of distinct AmbitionBox
endpoint paths across all three reports. It is the superset, so it answers the question under
either reading. The 2 endpoints beyond the canonical 42-row table are:

- `SG/inventory-management-services/v2/page/pagename/{name}` (POST) -- described in
  `ambitionbox-browser-probe-report.md` section 4 and `company-ecosystem-report.md` 5N, but never
  given a row in the numbered dedup table.
- `SG/user-services/v0/user/profile/basic-details` (14-field variant) --
  `company-ecosystem-report.md` 5H lists this as a distinct path from the 24-field
  `SG/user-services/v0/api/v2/user/profile/basic-details`; the browser report collapses the two.

Path key used in the table below:
`SG/` = `https://www.ambitionbox.com/servicegateway-ambitionbox`
`L/` = `https://www.ambitionbox.com/api` (legacy REST)

---

## 1. FLAGGED FINDING -- the shipping AmbitionBox REST bridge is probably dead right now

This was not part of the brief. It surfaced while confirming liveness and it changes how the
"already covered" column should be read, so it is reported rather than acted on.

**Measured today, 2026-08-24, from a cookie-less client:**

| Request | Status | Body |
|---|---|---|
| `GET SG/review-services/v0/review/filters/41`, no `appid`/`systemid` headers | **400** | `AppId or SystemId not present` |
| same URL, plus `appid: 109` + `systemid: AmbitionBox` | **200** | 402,765 bytes of real data |
| same URL, plus `appid: 1001` + `systemid: ambitionbox` | **200** | 402,765 bytes, byte-identical |

So the AB gateway checks only that the two headers are **present**, not what they contain, and it
needs **no cookies at all** for company-scoped endpoints.

**The shipped bridge sends neither header.** `_ab_rest_get` at
`naukri_server/tools/ambitionbox_rest.py:82-83` opens the session with cookies only:

```python
async with aiohttp.ClientSession(cookies=cookies) as session:
    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
```

There is no `headers=` argument anywhere in the file, and a repo-wide grep finds no AmbitionBox
`appid`/`systemid` constant -- `naukri_server/config.py:81-84, 419-428` defines them for Naukri
(`121`/`Naukri`), the widget API (`109`/`109`) and the company API (`103`/`Naukri`) only.

Consequences, if this holds with a real session cookie attached:

- All seven helpers in `ambitionbox_rest.py` hit the same 400: `ab_get_benefits` (:107),
  `ab_get_work_culture` (:120), `ab_get_interview_questions` (:149), `ab_get_competitors` (:165),
  `ab_get_locations` (:181), `ab_get_applied_jobs_insights` (:194), `ab_get_salary_rest` (:213).
- The failure is **silent**. `_ab_rest_get` retries only on 401/403 (`ambitionbox_rest.py:84`); a
  400 raises through `raise_for_status()`, and `_enrich_with_rest`
  (`naukri_server/tools/ambitionbox.py:740-779`) catches it into a debug log and an
  `_enrichment_errors` key. `naukri_company_intel` still returns `status: "success"` with the
  `ab_rest` block simply absent.
- `naukri_research_company` degrades the same way -- `company_service.py:500-508` gathers
  `ab_get_work_culture` and `ab_get_benefits` and, at :530-535, "silently skips failed AB REST
  calls (not critical)".
- The cookie machinery (`_get_ab_cookies`, `ambitionbox_rest.py:49-69`) drives a full browser
  navigation to `/list-of-companies` for cookies that these endpoints never required.

**Evidence class: DERIVED, not verified.** I proved the header gate from a cookie-less client and
I proved the bridge sends no headers. I did not run the live bridge with a real session, so I
cannot exclude that a session cookie satisfies the gateway check. Confirming it is one call to
`naukri_company_intel` and a look for the `ab_rest` key -- deliberately not run here, since it is
outside this slice and would put more browser traffic on an account with bot-block history.

This is a two-line fix if confirmed, and it is worth more than any endpoint in the table below:
restoring three tools that already exist beats adding a fourth.

---

## 2. Also on disk but unreachable: three helpers wired to nothing

`ab_get_interview_questions` (`ambitionbox_rest.py:149`), `ab_get_locations` (:181) and
`ab_get_salary_rest` (:213) are implemented, exported (`ambitionbox.py:26-28`) and tested
(`tests/test_ambitionbox_deep.py:884, 911, 941`) -- and referenced by **no MCP tool**. A grep over
`naukri_server/` finds only the import line and the definition. Their data never reaches the user.

`ab_get_salary_rest` additionally cannot be called even in principle: it needs a `job_profile_id`,
and no tool in the server can produce one. That is the gap endpoint #8 below closes.

Related: `AB_REVIEW_FILTERS_API` is defined at `config.py:211` and registered in the healing tier
registry (`naukri_server/healing/tier_registry.py:102`) as belonging to
`naukri_server.tools.ambitionbox_rest` -- but `ambitionbox_rest.py:30-35` does not import it and
no code uses it. The registry watches a constant nothing calls.

---

## 3. Classification -- all 44 endpoints

Classes: **DUPLICATE** (already covered), **DEAD** (does not answer today), **LOW-VALUE** (works,
novel, but nothing a job seeker acts on), **WORTH BUILDING** (novel and decision-relevant).

| # | Path | Returns | Class | Evidence |
|---|---|---|---|---|
| 1 | `SG/salaries-services/v0/company/{id}/jobProfile/{pid}/salaryData` | 17-field salary summary, experience bands, take-home, confidence | DUPLICATE | `ab_get_salary_rest` `ambitionbox_rest.py:213`; salary tables already scraped by `_fetch_salary` `ambitionbox.py:249` + `_scrape_salary_table` :91, surfaced via `naukri_company_intel(intel_type="salary")` :788 |
| 2 | `SG/review-services/v0/review/distribution/{id}` | Work culture: timing, travel, work days, shifts | DUPLICATE | `ab_get_work_culture` `ambitionbox_rest.py:120`, wired at `ambitionbox.py:762` and `company_service.py:506` |
| 3 | `SG/interview-services/v0/company/top-questions` | Interview questions by company + role | DUPLICATE | `ab_get_interview_questions` `ambitionbox_rest.py:149`; questions also scraped by `_scrape_interview_data` `ambitionbox.py:132` (question list built at :207-220) and returned by `naukri_company_intel(intel_type="interviews")` |
| 4 | `SG/benefits-services/v0/company/{id}/benefits-stats` | 30 employee-reported benefit categories | DUPLICATE | `ab_get_benefits` `ambitionbox_rest.py:107`, wired at `ambitionbox.py:763` and `company_service.py:508` |
| 5 | `SG/benefits-services/v0/company/{id}/employer-benefits` | 4 employer-claimed perks | LOW-VALUE | Novel, but it is the company's own marketing copy; #4 already ships the 30-category employee-reported version, which is the trustworthy one |
| 6 | `SG/company-services/v0/company/{id}/sectionalDetails?sections=companyLocations` | All office addresses, 8 fields each | DUPLICATE | `ab_get_locations` `ambitionbox_rest.py:181` (see section 2 -- written but unwired) |
| 7 | `SG/review-services/v0/review/popular/{id}?limit=N&jobProfile={slug}` | Reviews scoped to ONE job profile: `count`, `avgOverallRating`, 19-field review rows | **WORTH BUILDING** | No code reads it (grep: zero hits). Shipping reviews are company-wide only -- `_fetch_reviews` `ambitionbox.py:464`. Verified live today; see recommendation R1 |
| 8 | `SG/review-services/v0/review/filters/{id}` | 2907 job profiles with review counts and ids, 122 divisions, 245 locations, departments | **WORTH BUILDING** | No code reads it; `AB_REVIEW_FILTERS_API` `config.py:211` is defined and unused. Verified live today, 402,765 bytes; see recommendation R2 |
| 9 | `SG/insights-services/v0/insights/niAppliedJobs` | Salary ranges for jobs already applied to on Naukri | DUPLICATE | `ab_get_applied_jobs_insights` `ambitionbox_rest.py:194`, wired into `daily_brief.py:25, 198, 331` as `applied_salary_insights` |
| 10 | `SG/insights-services/v0/company/{id}/similar-companies-v3?type=overview` | 3 widgets, 13-field peer comparison | LOW-VALUE | Overlaps #11, which already ships 41 peers with rating and review count. Extra columns on a comparison he does not make -- he applies to a job, not to a company ranked against peers |
| 11 | `SG/company-services/v0/compare/company/top-comparsions/{id}` | 41 competitors with rating, review count | DUPLICATE | `ab_get_competitors` `ambitionbox_rest.py:165`, wired at `ambitionbox.py:764` |
| 12 | `SG/jobs-services/v0/jobs/info/naukri_{jobId}` | AB's mirror of a Naukri JD | DUPLICATE | `naukri_get_job` `jobs.py:308` returns the Naukri JD directly; AB is a strictly lossier copy of the same record |
| 13 | `SG/jobs-services/v0/jobs/similar/naukri_{jobId}` | 10 similar jobs, 18 fields | DUPLICATE | `naukri_similar_jobs` `jobs.py:323` |
| 14 | `SG/jobs-services/v0/jobs/filters/company/{id}` | Filter catalogue: 999 skills, 490 profiles, 23 locations | LOW-VALUE | Search-construction metadata; `naukri_taxonomy` `insights.py:382` already ships 37 departments / 167 role categories / 1461 roles for exactly this |
| 15 | `SG/jobs-services/v0/jobs/company/{id}` | Paginated company jobs, 30 fields | DUPLICATE | `naukri_company_jobs` `companies.py:73`, reached via `naukri_research_company` `company_service.py:466` |
| 16 | `SG/user-services/v0/user/get-preferences` | His own preferred locations, departments, CTC, experience | DUPLICATE | These are his Naukri values synced INTO AB. `naukri_get_profile` `profile.py:66` is the source of record; reading them back adds nothing |
| 17 | `SG/user-services/v0/user/naukri-account/consent` | SSO linkage flags | LOW-VALUE | Plumbing status. Changes no application decision |
| 18 | `SG/user-services/v0/api/v2/user/profile/basic-details` | 24 profile fields | DUPLICATE | `naukri_get_profile` `profile.py:66` |
| 19 | `L/v2/overview/interview-insights/{id}?limit=0` | Interview counts + source split (JobPortal / Referral / Campus / WalkIn ...) | LOW-VALUE | Verified live today: JobPortal 31, CompanyWebsite 22, CampusPlacement 20, Referral 11, Others 9, WalkIn 5, RecruitmentConsultant 2. The nearest miss in the whole table -- see "the one I argued about" below |
| 20 | `SG/review-services/v0/review/comments/bulk?rIds=` | Comment counts per review id | LOW-VALUE | Comment-thread metadata on reviews |
| 21 | `SG/review-services/v0/review/marked-helpful-reviews?reviewIds=` | Which reviews HE marked helpful | LOW-VALUE | His own votes; he has none |
| 22 | `SG/review-services/v0/review/comments/owner/{id}?type=review` | Employer replies to reviews | LOW-VALUE | Corporate PR responses |
| 23 | `L/v2/user/membership/status` (POST) | AB premium flag | LOW-VALUE | POST, so out of the read-only scope anyway. `naukri_subscription_status` `settings.py:495` covers the subscription question that matters |
| 24 | `L/v4/helpful/{id}?resourceType=CompanyInterview` | His helpful votes on interviews | LOW-VALUE | Empty in the 2026-03-02 capture |
| 25 | `SG/jobs-services/v0/job-alert/get-all` | His AB job alerts | DUPLICATE | `naukri_list_alerts` `alerts.py:368` owns alerts; a second alert store on AB is a liability, not a feature |
| 26 | `SG/chatbot-services/v0/chat/passthrough/recommended_content` (POST) | AI content recommendations per page | LOW-VALUE | POST; generated marketing content |
| 27 | `SG/chatbot-services/v0/chat/chat-history` | Chatbot transcript | LOW-VALUE | Empty |
| 28 | `SG/chatbot-services/v0/chat/chatbot-experiment` | A/B cookie assignment | LOW-VALUE | Returns "Cookie already set" |
| 29 | `SG/community-services/v0/api/v1/community/followed` | Communities he follows | LOW-VALUE | He follows none; and following a topic is not a hiring signal |
| 30 | `SG/community-services/v0/api/v2/community/aggregate?type=FOLLOWED` | Aggregate over followed communities | LOW-VALUE | Returned an empty array in the 2026-03-02 capture, and is empty by construction while #29 is empty |
| 31 | `SG/community-services/v0/api/v2/topics?limit=10` | Topic directory with post/follower/poll counts | LOW-VALUE | Verified live today (200, 5,466 bytes): "Salary Discussions", city topics, etc. A forum index. No company, role, or job scoping |
| 32 | `SG/community-services/v0/api/v3/textual-feed` | 25 social posts, 25 fields each | LOW-VALUE | Unranked, unverified, anonymous forum posts with no company or role scoping. This is the single largest volume item in the community set and the clearest case of volume without value |
| 33 | `SG/aggregation-services/v0/api/user/browsing-history` | Last 10 companies he browsed | LOW-VALUE | His own recent activity, which he already knows |
| 34 | `SG/utility-services/v0/notification/user/self?device=computer` | AB notifications | LOW-VALUE | Empty; `naukri_list_notifications` `notifications.py:58` is the channel that carries recruiter signal |
| 35 | `SG/utility-services/v0/app-info` | AB's own app-store rating | LOW-VALUE | About AmbitionBox, not about any employer |
| 36 | `SG/utility-services/v0/api/v3/abusiveList` | 1717-word profanity list | LOW-VALUE | The source report itself grades this "NONE for our purposes" |
| 37 | `SG/user-services/v0/user/pseudo-profile/available` | Anonymous review-posting identities | LOW-VALUE | For writing reviews, not reading them |
| 38 | `SG/user-services/v0/user/profile/suggestion` | His current employer and role | LOW-VALUE | `naukri_get_profile` `profile.py:66` already has it |
| 39 | `L/v2/user/isSubscribed?Entity=company&Id={id}` | Whether he follows a company | DUPLICATE | `naukri_follow_status` `companies.py:191` |
| 40 | `L/v4/user/consolidated-contribution-pitch` | Prompt asking him to write a review | LOW-VALUE | An upsell aimed at him |
| 41 | `L/v2/comment/helpful/{id}?type=interview_story` | Helpful votes on interview comments | LOW-VALUE | Vote metadata |
| 42 | `L/v2/comment/owner/{id}?type=interview_story` | Employer comments on interview stories | LOW-VALUE | Corporate PR responses |
| 43 | `SG/inventory-management-services/v2/page/pagename/{name}` (POST) | Ad and widget slot layout | LOW-VALUE | POST; AB's ad CMS |
| 44 | `SG/user-services/v0/user/profile/basic-details` (14-field variant) | Profile subset incl. `followedTopicsCount` | DUPLICATE | Same record as #18; `naukri_get_profile` `profile.py:66` |

---

## 4. Per-class tally

| Class | Count | Share of 44 |
|---|---|---|
| DUPLICATE | 15 | 34.1% |
| LOW-VALUE | 27 | 61.4% |
| WORTH BUILDING | 2 | 4.5% |
| DEAD | 0 | 0.0% |
| **Total** | **44** | **100%** |

DUPLICATE rows: 1, 2, 3, 4, 6, 9, 11, 12, 13, 15, 16, 18, 25, 39, 44.
WORTH BUILDING rows: 7, 8.
DEAD is 0 **as measured**: 6 distinct endpoints were probed (across 9 requests) and all 6 answer
today. The other 38 were not probed -- see section 6. Liveness is load-bearing only for the WORTH
BUILDING class, and both members of it were probed and are live.

Restricting to the literal `community-services` gateway -- rows 29, 30, 31, 32 -- the tally is
**4 endpoints, 4 LOW-VALUE, 0 worth building.**

---

## 5. RECOMMENDATION

**Build 2 of 44, and neither of them is a community endpoint. If the question was strictly about
the `community-services` gateway, the answer is build none -- all 4 are LOW-VALUE.**

The two worth building are both in `review-services`, and they are one feature, not two: together
they answer "what is it like to do THIS job at THIS company", which the server currently cannot
answer at any granularity finer than the whole company.

### R1 -- `SG/review-services/v0/review/popular/{companyId}?jobProfile={slug}`

**The decision it changes: whether to apply to a specific role at a company whose overall rating
looks fine.**

Measured today against company 41 (Infosys), two roles:

| `jobProfile` | `count` | `avgOverallRating` |
|---|---|---|
| `senior-java-developer` | 13 | **2.4** |
| `senior-systems-engineer` | 5,999 | **3.5** |

The company-wide figure for the same employer is **3.5** across 47,802 reviews
(`ambitionbox-browser-probe-report.md` section 2f, schema.org `EmployerAggregateRating`).

**Read these two honestly, they cut both ways.** The high-volume role returns exactly the
company-wide number -- which is what you would expect when one role supplies 5,999 of 47,802
reviews, and it means role-level data is often *not* new information. The divergent role is 1.1
stars below the company, but on n=13, which is small enough that the gap could be noise.

So the defensible claim is narrower than "the role rating differs from the company rating": it is
that **role-level rating exists, sometimes diverges by more than a star, and the shipping tool
cannot show it at all** -- `_fetch_reviews` (`ambitionbox.py:464`) is company-wide only. The
endpoint returns `count` next to `avgOverallRating`, so a consumer can weight by sample size and
suppress thin roles rather than being misled by them; that is a design requirement for whoever
builds this, not an afterthought. The response also carries per-review `likesText`/`dislikesText`
scoped to the role, which is better interview and negotiation input than a company-wide dump
regardless of how the average lands.

This is the weakest point in the recommendation and I would not object if the lead downgrades R1
to LOW-VALUE on the strength of the 3.5/5,999 row.

### R2 -- `SG/review-services/v0/review/filters/{companyId}`

**The decision it changes: none directly -- it is the key-resolver without which R1 cannot be
called, and it unblocks a helper that already exists.**

Verified live today: 402,765 bytes, `jobProfiles` = **2907** entries (up from the 2586 recorded on
2026-03-02), each carrying `id`, `urlName` and a review `count`; plus 122 `divisions` and 245
`locations` with counts. Two concrete uses:

1. It supplies the `jobProfile` slug R1 needs, and the numeric `id` that
   `ab_get_salary_rest(company_id, job_profile_id)` (`ambitionbox_rest.py:213`) needs. That helper
   is currently uncallable because **nothing in the server can produce a `job_profile_id`.**
   Building R2 turns dead code into a working salary-by-role capability at no extra cost.
2. The `count` per profile and per division is itself signal: it says how many people at that
   company hold the role he is applying for. A role with 5,999 reviewers is a factory floor; one
   with 13 is a bespoke position. That is a real difference in what the job will be.

I am recommending R2 as an enabler, not on its own merits. If R1 is rejected, R2 should be
rejected with it -- unless the `ab_get_salary_rest` unblock is judged to stand alone, which is a
reasonable call to make the other way.

### The one I argued about and rejected: #19, interview source split

`L/v2/overview/interview-insights/{id}` is live, needs no auth, and returns a genuinely novel
number: what fraction of people interviewed there came in via job portal vs referral vs campus.
The case for it is that a company at 3% JobPortal / 70% Referral would tell him cold-applying is
near-futile. The case against, which I think wins: the figure is aggregated across the entire
company, all roles and all years; the marginal cost of one more Naukri application is close to
zero, so information that only changes "should I bother applying" buys very little; and the
referral pathing he would act on is already served by the `linkedin-jobs` skill. It is a nice
number that would not change what he does. LOW-VALUE.

### What NOT to build

The remaining 42. In particular the 4 `community-services` endpoints (rows 29-32): the feed is 25
anonymous, unranked, unscoped forum posts, the topic list is a forum index, and the two
"followed" endpoints are empty because he follows nothing. There is no path from any of them to a
decision about where to apply. Adding them would grow the tool surface and the AmbitionBox request
budget on an account that already carries bot-block history, in exchange for nothing.

### Sequencing note

Both recommendations are downstream of section 1. If the header finding is confirmed, fixing
`_ab_rest_get` restores seven helpers and three tools' worth of enrichment for a two-line change,
and it is a strictly better use of the next hour than either R1 or R2. Fix the bridge, wire the
three orphaned helpers, and only then build R1+R2 -- which will need the same header fix to work
at all.

---

## 6. What I could not determine

1. **Where "36" came from.** No report on disk states 36 endpoints of any kind. The four candidate
   numbers are 4 (`community-services` proper), 42 (canonical dedup table), 44 (full union), and
   the reports' own inconsistent self-totals of "35+" and "40+". I classified all 44 rather than
   guess which 36 were meant.
2. **Whether the shipped bridge is actually 400-ing with a real session cookie attached.** I
   proved the gateway rejects a cookie-less request that omits `appid`/`systemid`, and that
   `_ab_rest_get` sends no headers. I did not prove that a valid session cookie fails to satisfy
   the check. One `naukri_company_intel` call plus a look for the `ab_rest` key settles it; not
   run here to keep browser traffic off the account and to stay inside this slice.
3. **Liveness of 38 of the 44 endpoints.** 6 were probed. The user-scoped ones (rows 16-18, 21,
   24, 29, 30, 33, 34, 37, 38, 40, 44) cannot be meaningfully probed without his session, and
   probing them would return his personal data, which this document deliberately does not touch.
   None of the 38 is in the WORTH BUILDING class, so liveness does not affect the verdict.
4. **The real `appid`/`systemid` values AmbitionBox's own front end sends.** Two different
   invented pairs both returned byte-identical 200s, so the gateway appears to check presence
   only. A future AB change could start validating the value; whatever fix lands should note that
   the values used are not the site's own.
5. **Whether role-level review ratings diverge often enough to be worth a tool.** Two roles at one
   large employer is not a sample. One matched the company average exactly (n=5,999), one was 1.1
   stars below it (n=13). I cannot say from two points how often a *mid-volume* role -- the kind
   he would actually target -- diverges from its company, and that frequency is precisely what
   decides whether R1 earns its place. The cheap test before building: pull 10 profiles with
   `count` between 100 and 1,000 from one `review/filters` response and compare their ratings to
   the company aggregate. If the spread is flat, R1 is LOW-VALUE and R2 survives only on the
   `ab_get_salary_rest` unblock.
6. **Whether `review/popular` behaves the same for small employers.** Both samples are from one
   47,802-review employer. At a 200-review company every role may be too thin to read.
7. **The POST endpoints (rows 23, 26, 43).** Classified from the reports only; the read-only
   constraint forbade exercising them, and nothing about them looked worth an exception.

---

## 7. Probe log

9 requests total, all GET, all read-only, minimum 3s apart, no 403 and no 429 at any point.

| # | Endpoint | Headers | Status | Bytes |
|---|---|---|---|---|
| 1 | `SG/review-services/v0/review/filters/41` | none | 400 | 29 |
| 2 | `SG/review-services/v0/review/popular/41?limit=1&jobProfile=senior-java-developer` | none | 400 | 29 |
| 3 | `SG/community-services/v0/api/v2/topics?limit=10` | none | 200 | 5,466 |
| 4 | `SG/insights-services/v0/company/41/similar-companies-v3?type=overview` | none | 400 | 29 |
| 5 | `L/v2/overview/interview-insights/41?limit=0` | none | 200 | 282 |
| 6 | `SG/review-services/v0/review/filters/41` | `appid: 109`, `systemid: AmbitionBox` | 200 | 402,765 |
| 7 | `SG/review-services/v0/review/filters/41` | `appid: 1001`, `systemid: ambitionbox` | 200 | 402,765 |
| 8 | `SG/review-services/v0/review/popular/41?limit=1&jobProfile=senior-java-developer` | `appid: 109`, `systemid: AmbitionBox` | 200 | 1,121 |
| 9 | `SG/review-services/v0/review/popular/41?limit=1&jobProfile=senior-systems-engineer` | `appid: 109`, `systemid: AmbitionBox` | 200 | 838 |

All 400s returned the identical body `AppId or SystemId not present`. Probes 6 and 7 were
byte-identical, which is what establishes that the header values are not validated. Probes 8 and 9
are the two role-level rating samples discussed in R1.

Company 41 = Infosys, the id used throughout the 2026-03-02 probing reports. No personal or
account-scoped endpoint was probed; no request carried a cookie.
