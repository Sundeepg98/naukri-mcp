# Naukri profile WRITE CONTRACT, derived from the shipped front-end bundle

Date derived: 2026-08-24
Method: static read of Naukri's own public JavaScript. **Zero writes were issued.** Every network
request made during this derivation was a GET of a public static asset or a public HTML page.
No POST/PUT/PATCH/DELETE was sent to naukri.com at any point.

---

## 0. Headline

**The route is DELTA (merge) semantics, not FULL-OBJECT.** An omitted key is NOT a deletion.
Deletion is expressed through two dedicated, explicit channels that would be redundant under
FULL-OBJECT semantics: an `isDelete` array inside the section object, and a per-row DELETE route
that is a different URL entirely. See section 5 for the code that settles this.

The semantic verb is **PUT, tunnelled over POST** via `X-HTTP-Method-Override: PUT`. This
reconciles the repo's existing observation (`profile_update.py:624` matches
`response.request.method == "POST"`) with a PUT contract: the wire method really is POST, the
intended method really is PUT.

---

## 1. Where the code lives (provenance)

`https://www.naukri.com/mnjuser/profile` returns HTTP 302 to `/nlogin/login` when unauthenticated,
so the page HTML is not directly harvestable. The bundle was reached as follows, each step a GET:

1. `GET https://www.naukri.com/mnjuser/homepage` -> HTTP 200, a Next.js App Router shell.
   Asset prefix: `https://static.naukimg.com/s/9/105/_next/`.
2. `GET https://static.naukimg.com/s/9/105/_next/app-build-manifest.json` -> HTTP 200, 11090 bytes.
   It enumerates **22 routes**. `/mnjuser/profile` is **not** among them. The profile editor is
   therefore NOT in the Next app. (This is a fact from the manifest, not an inference.)
3. `GET https://www.naukri.com/nlogin/login` -> HTTP 200. Its script tags point at the legacy
   RequireJS family under `https://static.naukimg.com/s/5/105/j/`.
4. `GET https://static.naukimg.com/s/5/105/j/app_v467.min.js` -> HTTP 200, 180348 bytes. It carries
   the RequireJS loader map. At offset 27593: `Y="//static.naukimg.com/s/5/105"`, `baseUrl:Y+"/j"`;
   at offset 23459 the flow-version map
   `$c={app:"_v467",mnj:"_v320",searchFlow:"_v235",savedJobs:"_v200",inbox:"_v232",login:"_v204",setting:"_v240",pendingAction:"_v285",dashboard:"_v295"}`;
   at offset 156807 the flow list `ci=[{flowName:"mnj",jsVersion:$c.mnj,...},...]` and the module
   name rule `flowName + jsVersion + ".min"`. Hence `mnj` resolves to `mnj_v320.min`.
5. `GET https://static.naukimg.com/s/5/105/j/mnj_v320.min.js` -> HTTP 200, **848718 bytes**.
   This is the My-Naukri profile-editor bundle and the source of everything below.

**Primary evidence file: `mnj_v320.min.js`, 848718 bytes, asset prefix `/s/5/105/j/`.**
All byte offsets below are into that exact file as served.

Secondary evidence file: `https://static.naukimg.com/s/9/105/_next/static/chunks/325-eba35e0818b4f361.js`
(15614 bytes) plus its URL-constant module
`.../chunks/5469-4470b56fce17d3fb.js` (14954 bytes) and
`.../chunks/ni-gnb-drawer.a1d45bee088e740c.js` (8491 bytes). These carry the dashboard's own
job-preferences write, which uses a **different route version** (see row 21).

Beautification: the bundle is minified. `jsbeautifier` (Python) was installed and used to produce a
1144788-byte / 34708-line readable form for reading only. All quoted code below is verbatim from
that beautified form; all offsets are into the original minified file.

---

## 2. The two write routes

| id | URL | used by |
|----|-----|---------|
| `_c` | `//www.naukri.com/cloudgateway-mynaukri/resman-aggregator-services/v1/users/self/fullprofiles` | **every** profile-section save in `mnj_v320.min.js` |
| (Next app) | `https://www.naukri.com/cloudgateway-mynaukri/resman-aggregator-services/v0/users/self/fullprofiles` | the dashboard job-preferences drawer only |

Declaration of `_c`, `mnj_v320.min.js` offset **44671**:

```
_c="//www.naukri.com/cloudgateway-mynaukri/resman-aggregator-services/v1/users/self/fullprofiles"
```

Sibling base used for per-row deletes, `Eb`, offset **44830**:

```
Eb="//www.naukri.com/cloudgateway-mynaukri/resman-aggregator-services/v0/users/self"
```

A `/v2/users/self` constant also exists in the bundle (`Fb`) but **no write call site references
it** -- it is not part of the profile write contract. There is no `/v2/.../fullprofiles` anywhere
in either bundle.

### Wire shape (identical at every `_c` call site)

```
method:  "POST"
url:     <_c>
headers:
  accept:                   "application/json"
  "Content-Type":           "application/json"
  "X-HTTP-Method-Override": "PUT"
  clientid:                 "d3skt0p"      (present at most, not all, sites)
  appid:                    "105"
  systemid:                 "Naukri"
  authorization:            "Bearer " + <value of cookie nauk_at>
body:    JSON.stringify(<payload>)
```

`clientid: "d3skt0p"` is absent at exactly 2 of the section sites (`saveRecommendedDetails`,
`submitEmployment`); `appid`/`systemid`/`authorization`/`X-HTTP-Method-Override` are present at all
of them. `refreshApi: !0` is a jQuery-plugin flag (token refresh on 401), not a wire field.

Every payload is assembled as `Ud({}, <sectionBuilder>(...), {profileId: <id>})`, where `Ud` is
plain `Object.assign` -- offset **67191** in the beautified form reads
`Ud = Object.assign || function(a){...}`. So `profileId` sits at the TOP level of the body,
alongside the section key.

---

## 3. Per-section table

Evidence class: `BUNDLE-VERIFIED` = the call site AND the payload builder were both read.
Every row below is BUNDLE-VERIFIED except where stated. Offsets are into `mnj_v320.min.js`
(848718 bytes) unless the file column says otherwise.

| # | Section | Method | URL path | Body envelope (verbatim JS-derived) | DELTA / FULL | Locator (file + offset / token) | Evidence |
|---|---------|--------|----------|--------------------------------------|--------------|--------------------------------|----------|
| 1 | Resume headline | POST + `X-HTTP-Method-Override: PUT` | `/cloudgateway-mynaukri/resman-aggregator-services/v1/users/self/fullprofiles` | `{profileId, profile:{resumeHeadline}}` | DELTA | mnj_v320 @35852 `function da(a){var b={resumeHeadline:` | BUNDLE-VERIFIED |
| 2 | Profile summary | same | same | `{profileId, profile:{summary}}` | DELTA | mnj_v320 @35970 `function ea(a,b){if(a)if("save"==b)var c={summary:` | BUNDLE-VERIFIED |
| 3 | Key skills | same | same | `{profileId, profile:{keySkills}}` (comma-joined string) | DELTA | mnj_v320 @36311 `function ha(a,b){if(a.keySkills=` | BUNDLE-VERIFIED |
| 4 | Career profile / preferences (desired profile) | same | same | `{profileId, profile:{entityIndustryTypeId, entityDepartment, entityRoleCategory, entityRole, desiredJobType, desiredEmploymentType, newLocationPrefId, desiredRoleTypeId, shiftPrefTime, expectedCtcCurrency, absoluteExpectedCtc, joinDate}}` then passed through `Go(...)` | DELTA (+ explicit `isDelete`) | mnj_v320 @36863 `function la(a,b){b.entityDepartment.id?` | BUNDLE-VERIFIED |
| 5 | IT skills | same | same | `{profileId, itskills:[{entitySkill, entitySkillId, version, lastUsed, experienceTime:{year,month}, sid?}]}` | DELTA, single row | mnj_v320 @37812 `function ma(a){return{itskills:[` | BUNDLE-VERIFIED |
| 6 | Education (degree; education id 1/2/3) | same | same | `{profileId, educations:[{course, courseType, educationType, entityInstitute:{id,value}, specialisation, yearOfCompletion, yearOfStart, grade?, marks?, educationId?, isPrimary?}]}` | DELTA, single row | mnj_v320 @38155 `function oa(a,b){var c={};return c=Array.isArray(a.grade)` | BUNDLE-VERIFIED |
| 7 | Education (class 10 / 12; education id 10/12) | same | same | `{profileId, schools:[{schoolBoard, schoolCompletionYear, schoolEnglishMarks, schoolLevel, schoolMathematicsMarks, schoolMedium, absPercentage?, schoolId?}]}` | DELTA, single row | mnj_v320 @38913 `function pa(a){return{schools:[` | BUNDLE-VERIFIED |
| 8 | Education (no formal education; id 99) | same | same | `{profileId, schools:[{schoolLevel:-1, schoolId?}]}` | DELTA, single row | mnj_v320 @39371 `function qa(a){return{schools:[` | BUNDLE-VERIFIED |
| 9 | Employment | same | same | `{profileId, employments:[{employmentType, jobDescription, organization, organizationId, startDate, endDate, experienceType, employmentId?, ...}], profile?, profileAdditional?, noticePeriod?}` | DELTA, single row | mnj_v320 @568934 `rq=function(a,b){var c={employments:[{employmentType:` | BUNDLE-VERIFIED |
| 10 | Projects | same | same | `{profileId, projects:[{clientName, projectClientId, startDate, employmentType, isOnSiteProject, projectDetails, projectLocation, projectTitle, skills, teamSize, designation, role, projectId?, endDate?, eduExpFlag, eduExpId}]}` | DELTA, single row | mnj_v320 @755823 `As=function(a){var b={clientName:` | BUNDLE-VERIFIED |
| 11 | Certifications | same | same | `{profileId, certifications:[<form object, passed through>]}` | DELTA, single row | mnj_v320 @718277 `fs=function(a){return{certifications:[a]}}` | BUNDLE-VERIFIED |
| 12 | Personal details (incl. languages) | same | same | `{profileId, profile:{birthDate?, gender, contactAddress, mailCity, pincode, maritalStatus, category, workStatus, workPermitForCountry, disability?}, userIdentification?:{panNumber?, aadharNumber?}, disability?:{...}, languages?:[ALL], extendedProfile?:{workDiversity?, careerBreak?}}` then `Go(...)` | DELTA at section level; `languages` is a FULL LIST | mnj_v320 @790786 `at=function(a,b){var c={profile:{}};` | BUNDLE-VERIFIED |
| 13 | Online profiles | same | same | `{profileId, onlineProfiles:[ALL rows]}` | DELTA at section level; FULL LIST within section | mnj_v320 @640662 `er=function(a,b){return a.forEach(function(a){a.onlineProfileId=a.id` | BUNDLE-VERIFIED |
| 14 | Work samples | same | same | `{profileId, workSamples:[ALL rows]}` | DELTA at section level; FULL LIST within section | mnj_v320 @656875 `pr=function(a,b){var c=[];...b.workSampleId=` | BUNDLE-VERIFIED |
| 15 | Presentations | same | same | `{profileId, presentations:[ALL rows]}` | DELTA at section level; FULL LIST within section | mnj_v320 @668250 `Ar=function(a,b){...b.presentationId=` | BUNDLE-VERIFIED |
| 16 | Publications | same | same | `{profileId, publications:[ALL rows]}` | DELTA at section level; FULL LIST within section | mnj_v320 @681299 `Lr=function(a,b){...b.publicationId=` | BUNDLE-VERIFIED |
| 17 | Patents | same | same | `{profileId, patents:[ALL rows]}` | DELTA at section level; FULL LIST within section | mnj_v320 @696760 `Wr=function(a,b){...b.patentId=` | BUNDLE-VERIFIED |
| 18 | Basic details | same | same | `{profileId, profile:{name, city, country, noticePeriod?, experience?, currency?, absoluteCtc?}, user:{mobile, residencePhone:{countryCode, areaCode, phoneNumber}}, profileAdditional:{fixedCtc, variableCtc, stocks, salaryDDId, profileId, locality}, noticePeriod?:{noticeEndDate}}` | DELTA | mnj_v320 @167443 `Ej=function(a,b){var c={profile:{name:a.fullName` | BUNDLE-VERIFIED |
| 19 | Mobile number only | same | same | `{profileId, user:{mobile}}` | DELTA | mnj_v320 @34660 `function X(a){var b={user:{mobile:a}};return b}` | BUNDLE-VERIFIED |
| 20 | Recommended details (resume-parser prefill) | same | same | `{profileId, profile?:{city, country, entityIndustryTypeId, entityDepartment, entityRole, entityRoleCategory, keySkills, summary, gender}, user?:{mobile}}` -- empty sub-objects are DELETED from the body before send | DELTA | mnj_v320, beautified line 18901, token `$.isEmptyObject(b(d.profile))&&delete d.profile` | BUNDLE-VERIFIED |
| 21 | Dashboard job preferences (different app, different route version) | POST? no -- literal `method:"PUT"` | `/cloudgateway-mynaukri/resman-aggregator-services/**v0**/users/self/fullprofiles` | `{profileId, profile:{absoluteExpectedCtc, newLocationPrefId, expectedCtcCurrency, desiredRoleTypeId}}` | DELTA | `325-eba35e0818b4f361.js` @2467 (`r.Pr`, export `JK` of module 51490); payload builder in `ni-gnb-drawer.a1d45bee088e740c.js` @6442 | BUNDLE-VERIFIED |

Note on row 21: this is the only write in the entire survey that uses a **literal `method:"PUT"`**
with no override header, and the only one that targets **v0** rather than v1. Its headers are
`{appid, systemid, clientId, "Content-Type":"application/json"}` with `enableAuthFlow:true` -- no
`Bearer` header is set by the caller.

### Section-name coverage against the brief

| brief asked for | resolved as | row |
|---|---|---|
| employment | `employments` | 9 |
| education | `educations` and `schools` (branching on education level) | 6, 7, 8 |
| itskills / keySkills | `itskills` (section) and `profile.keySkills` (string) -- these are two different things | 5, 3 |
| projects | `projects` | 10 |
| certification(s) | `certifications` | 11 |
| summary / profileSummary | `profile.summary` | 2 |
| careerProfile / preferences | `profile.*` desired-profile block | 4, 21 |
| language | `languages`, carried inside the personal-details save | 12 |
| personalDetails | `profile.*` + `userIdentification` + `disability` + `extendedProfile` | 12 |

**NOT-FOUND count: 0.** Every section named in the brief was resolved to a call site and a payload
builder.

---

## 4. Create vs update, and id fields

**Create and update go to the SAME route with the SAME method.** The only difference is the
presence of the row's id key inside the section object. There is no separate create endpoint.

| Section | id field | create | update |
|---|---|---|---|
| employments | `employmentId` | key OMITTED (sentinel `empId === "-1"` in the form) | `employmentId: <id>` |
| itskills | `sid` | key OMITTED | `sid: <itSkillId>` |
| educations | `educationId` | key OMITTED | `educationId: <prefillId>` |
| schools | `schoolId` | key OMITTED | `schoolId: <prefillId>` |
| projects | `projectId` | key OMITTED (sentinel `projectId === -1`) | `projectId: <id>` |
| certifications | `certificationsId` | (carried on the form object) | (carried on the form object) |
| onlineProfiles | `onlineProfileId` | -- (full list always sent) | `onlineProfileId` set from `row.id`; `id` and `profileId` are `delete`d off each row first |
| workSamples | `workSampleId` | -- (full list) | `workSampleId` set from `row.id` |
| presentations | `presentationId` | -- (full list) | `presentationId` set from `row.id` |
| publications | `publicationId` | -- (full list) | `publicationId` set from `row.id` |
| patents | `patentId` | -- (full list) | `patentId` set from `row.id` |
| languages | `languageId`, `entityLanguageId` | -- (full list) | both carried per row |

Verbatim, employment (offset 568934):

```
if (c.profileId = b, "-1" != a.empId && (c.employments[0].employmentId = a.empId), ...
```

Verbatim, IT skills (offset 37812) -- the id is merged in only when present:

```
}, a.itSkillId ? { sid: a.itSkillId } : {})]
```

### Row DELETION is a different route

Deleting a row does **not** go through `fullprofiles`. It is:

```
POST  https://www.naukri.com/cloudgateway-mynaukri/resman-aggregator-services/v0/users/self/profiles/{profileId}/{collection}/{rowId}
headers: X-HTTP-Method-Override: DELETE   (plus the same accept/Content-Type/clientid/appid/systemid/authorization set)
body:    none
```

**12 distinct collections** were found using this exact form, with their offsets in
`mnj_v320.min.js`:

| collection | offset |
|---|---|
| `itskills` | 518447 |
| `employments` | 574354 |
| `educations` | 620670 |
| `schools` | 620737 and 620769 |
| `onlineProfiles` | 643304 |
| `workSamples` | 659416 |
| `presentations` | 670359 |
| `publications` | 683703 |
| `patents` | 699121 |
| `certifications` | 720378 |
| `projects` | 758345 |
| `languages` | 796152 |

Two genuine `method:"DELETE"` calls also exist, both non-section: profile photo
(`{v0}/users/self/profiles/{profileId}/photo`) and text resume
(`{v0}/users/self/profiles/{profileId}/deleteResume`).

---

## 5. VERDICT: DELTA vs FULL-OBJECT

**Verdict: DELTA (merge). An omitted key is NOT a deletion. This holds for every section in the
table.** Below is exactly the code that settles it. Four independent lines of evidence, all read
directly out of `mnj_v320.min.js`.

### 5.1 The `strip` / `isDelete` pair -- the decisive one

The desired-profile and personal-details forms run every field through a deletion tracker before
building the payload. Beautified form, offsets 407806 (`Fo`) and 408048 (`Go`) in the minified file:

```js
Fo = function(a) {                          // a = field spec map
  var b = {};                               // b = snapshot of "did this field have a value on open"
  return {
    snapshot: function(c) {
      Object.keys(a).forEach(function(d) { b[d] = !!a[d].hasValue(c[d]) })
    },
    getDeletions: function(c) {
      var d = {}, e = {};
      return Object.keys(a).forEach(function(f) {
        var g = a[f];
        g.hasValue(c[f]) || (
          (e[g.section] = e[g.section] || []).push(g.apiField),        // -> strip
          b[f] && (d[g.section] = d[g.section] || []).push(g.apiField) // -> isDelete
        )
      }), { isDelete: d, strip: e }
    }
  }
},
Go = function(a, b) {
  if (!b) return a;
  var c = b.isDelete, d = void 0 === c ? {} : c,
      e = b.strip,    f = void 0 === e ? {} : e;
  return Object.keys(f).forEach(function(b) {
    a[b] && f[b].forEach(function(c) { delete a[b][c] })               // strip: REMOVE the key from the body
  }), Object.keys(d).forEach(function(b) {
    d[b].length && (a[b] = a[b] || {}, a[b].isDelete = d[b])           // isDelete: name the fields to clear
  }), a
}
```

Read that carefully:

* `strip` **removes the key from the outgoing JSON**. It is applied to every field that is empty in
  the form. If an omitted key meant "delete this field", `strip` would be a data-destroying
  operation applied to every blank input on the form, and it would make `isDelete` unreachable
  dead code.
* `isDelete` is applied only to fields that **had a value when the form opened and are now empty**
  -- i.e. the user actually cleared them. It emits `profile.isDelete = ["fieldA","fieldB"]`.

The only coherent reading is: the server merges what it receives, ignores what it does not receive,
and clears only what is explicitly listed in `isDelete`. That is DELTA by construction.

Producers of that structure, beautified lines 17318 and 31619:

```js
e.fieldDeletions = a.deleteTracker.getDeletions(a.state), a.props.submitDesiredProfile(c, e, ...)
fieldDeletions: this.deleteTracker.getDeletions(this.getDeleteTrackerState(this.state))
```

Consumers, offsets 36863 (`la`, career profile) and 790786 (`at`, personal details), both ending in:

```js
return Go({ profile: c }, b.fieldDeletions)      // la
return Go(c, a.fieldDeletions)                   // at
```

### 5.2 Section saves send exactly one section key

`da` sends `{profile:{resumeHeadline}}` and nothing else. `ma` sends `{itskills:[...]}` and nothing
else. `fs` sends `{certifications:[row]}` and nothing else. If the route were FULL-OBJECT, editing
a resume headline would wipe employment, education, skills, projects, certifications and personal
details in one request. The product plainly does not do that, and the bundle contains no code that
would re-send the untouched sections.

### 5.3 List sections send exactly one row -- and there are dedicated per-row delete routes

`employments`, `educations`, `schools`, `itskills`, `projects` and `certifications` are each sent as
a **single-element array** containing only the row being edited (offsets 568934, 38155, 38913,
37812, 755823, 718277). Under FULL-OBJECT semantics, saving one employment row would delete every
other employment row.

Independently: **12 dedicated per-row delete routes exist** (section 4). Under FULL-OBJECT
semantics, deleting a row would simply be "send the list without it" and not one of those 12 routes
would need to exist.

### 5.4 Deletion of a scalar is expressed as an explicit empty string, not as omission

Offsets 35970 (`ea`) and 36311 (`ha`):

```js
if ("save"   == b) var c = { summary:   a.profileSummary... };
else if ("delete" == b) var c = { summary:   "" };
...
if ("save"   == b) var c = { keySkills: a.keySkills };
else if ("delete" == b) var c = { keySkills: "" };
```

The delete path sends the key with `""`. If omission deleted, the delete path would simply omit the
key. It does not.

### Per-section verdicts

| Section | Verdict | Settled by |
|---|---|---|
| Resume headline | DELTA | 5.2 (builder `da`, @35852) |
| Profile summary | DELTA | 5.4 (builder `ea`, @35970) -- delete sends `summary:""` |
| Key skills | DELTA | 5.4 (builder `ha`, @36311) -- delete sends `keySkills:""` |
| Career profile / preferences | DELTA, with explicit `isDelete` | 5.1 (`la` @36863 -> `Go` @408048) |
| IT skills | DELTA, single row | 5.3 (`ma` @37812 + delete route @518447) |
| Education (degree) | DELTA, single row | 5.3 (`oa` @38155 + delete route @620670) |
| Education (class 10/12) | DELTA, single row | 5.3 (`pa` @38913 + delete route @620737) |
| Education (none) | DELTA, single row | 5.3 (`qa` @39371) |
| Employment | DELTA, single row | 5.3 (`rq` @568934 + delete route @574354) |
| Projects | DELTA, single row | 5.3 (`As` @755823 + delete route @758345) |
| Certifications | DELTA, single row | 5.3 (`fs` @718277 + delete route @720378) |
| Personal details | DELTA, with explicit `isDelete` | 5.1 (`at` @790786 -> `Go` @408048) |
| Languages | DELTA at section level; **FULL LIST inside the `languages` key** | 5.2 + builder `at` @790786 pushes every row of `languagesArr`; delete route @796152 |
| Online profiles | DELTA at section level; **FULL LIST inside the key** | builder `er` @640662; delete route @643304 |
| Work samples | DELTA at section level; **FULL LIST inside the key** | builder `pr` @656875; delete route @659416 |
| Presentations | DELTA at section level; **FULL LIST inside the key** | builder `Ar` @668250; delete route @670359 |
| Publications | DELTA at section level; **FULL LIST inside the key** | builder `Lr` @681299; delete route @683703 |
| Patents | DELTA at section level; **FULL LIST inside the key** | builder `Wr` @696760; delete route @699121 |
| Basic details | DELTA | 5.2 (`Ej` @167443) |
| Recommended details | DELTA | builder deletes empty sub-objects before send (beautified line 18901) |
| Dashboard job preferences (v0) | DELTA | 4 keys sent out of a much larger profile object (`ni-gnb-drawer` @6442) |

**UNSETTLED: none at the section level.**

**The one caveat that is NOT settled by the bundle, stated plainly:** all of the above establishes
what the *client* believes and does. It is very strong evidence about the server's contract --
`strip`, `isDelete`, the 12 delete routes and the single-row arrays would each be a live data-loss
bug under FULL-OBJECT semantics, and all four are present simultaneously -- but it is *derived*
from client code, not *observed* from the server. What would settle it beyond argument: a single
authenticated round trip that PUTs one scalar into one section and then re-reads the full profile
to confirm the sibling keys survived. That requires a write and was correctly out of scope here.

---

## 6. Two important gotchas for anyone porting this

### 6.1 The two sections that are FULL LIST inside their key

`onlineProfiles`, `workSamples`, `presentations`, `publications`, `patents` and `languages` send
**every row every time**. A caller that sends only the row it wants to add WILL delete the others.
These six are the only genuine data-loss hazard in the contract, and the hazard lives inside the
section, not at the top level.

Verbatim (`er`, offset 640662):

```js
er = function(a, b) {
  return a.forEach(function(a) { a.onlineProfileId = a.id, delete a.profileId, delete a.id }),
  { onlineProfiles: a, profileId: b }
}
```

`a` is the entire array. Same pattern at 656875, 668250, 681299, 696760, and inside `at` at 790786
for `languages`.

### 6.2 The method really is POST on the wire

`profile_update.py:608` says "The fullprofiles endpoint rejects all external API calls (405 from
CDN)." A 405 is consistent with sending a literal `PUT` to a route whose edge only accepts `POST`.
The bundle never sends a literal PUT to the v1 route -- it always sends `POST` plus
`X-HTTP-Method-Override: PUT`. Whether that alone lifts the 405 is untested here (testing it
requires a write).

`profile_update.py:624` and `:803` match `response.request.method == "POST"`, which is correct
against this contract and should not be "fixed" to PUT.

---

## 7. Required-but-non-obvious fields the builders always attach

* `profileId` at the top level of every body, always, merged in by the caller via `Object.assign`.
* `Authorization: Bearer <cookie nauk_at>` -- the token is read out of `document.cookie`, not from
  local storage. Header set at every `_c` site.
* `appid: "105"`, `systemid: "Naukri"`, and usually `clientid: "d3skt0p"`. The Next-app v0 write
  uses `appid`/`systemid`/`clientId` from a different constants module.
* Dates are always `YYYY-MM-01` strings, zero-padded via `("0"+m).slice(-2)`. Employment `endDate`
  is `""` when the row is current. Work-sample `endDate` is the literal `"0000-00-00"` when
  "currently working on" is checked.
* `employmentType` is the controlled string `"current"` or `"previous"`, derived from a `"yes"`/
  not-`"yes"` form value -- it is not a boolean.
* `experienceType` defaults to `"F"`; `"I"` selects a different field set (internship: `location`,
  `department`, `roleCategory`, `role`, `currency`, `salary`, `projectURL`) and SUPPRESSES
  `keySkills`/`designation`/`designationId`.
* `schoolLevel` is a controlled integer: `1` for class 10, `2` for class 12, `-1` for
  "no formal education" (education id 99).
* `courseType` passes through a lookup table `na(a)` -> `Dh[a]`, i.e. a controlled-vocabulary id,
  not the display string.
* `entityInstitute` defaults to `{id:"", value:""}` when absent -- the key is always present.
* Controlled-vocabulary ids are coerced with `parseInt` for `entityDepartment.id`,
  `entityRoleCategory.id`, `entityRole.id`, and `entityIndustry.id` is re-wrapped as
  `entityIndustryTypeId: {id: parseInt(...)}`.
* `newLocationPrefId` and `desiredRoleTypeId` are arrays of `{id, value}`, built by `ka()` from
  parallel id/value lists.
* `joinDate` is one of the literal string `"immediately"`, a `YYYY-MM-01` date, or `""`.
* Key skills are de-duplicated through a `Set`, trimmed, and HTML-unescaped (`&gt;`/`&lt;`) before
  being re-joined with `","`.
* Free-text fields (`resumeHeadline`, `summary`) are normalised: `/\n\s*\n/g -> "\n"` and
  `/ +/g -> " "`.
* When an employment row is marked current, the employment save ALSO carries `profile`
  (noticePeriod, currency, absoluteCtc, experience) and `profileAdditional` (fixedCtc, variableCtc,
  stocks, salaryDDId, profileId), and when `np_keys === "6"` a `noticePeriod` block with
  `noticeEndDate`, `offeredDesig`, `offeredDesigId`, `nextEmployer`, `nextEmployerId`, and
  optionally `salary:{currency, absoluteSalary}`.
* `disability.isDisabled` is the controlled string `"Y"`/`"N"`, and it is written into BOTH
  `profile.disability.isDisabled` and `disability.disabled`.
* `extendedProfile.workDiversity` uses `{isDelete: true}` as its clear form (beautified line 32320)
  rather than an empty array.
* The employment response is checked for partial success: the handler walks the response for a
  nested `error.fieldValidationErrors[0].customErrorCode`, meaning **HTTP 200 does not by itself
  mean the row was accepted**. Any port must inspect the body.

---

## 8. What I could not determine

Exhaustively:

1. **Server-side behaviour.** Everything here is derived from client code. I did not and could not
   confirm by observation that the server merges rather than replaces. See the caveat at the end of
   section 5.
2. **Whether `POST` + `X-HTTP-Method-Override: PUT` actually clears the 405 recorded in
   `profile_update.py:608`.** Confirming it needs a request that mutates data.
3. **The exact shape of the `certifications` row.** `fs` is a pure passthrough
   (`{certifications:[a]}`); the row object is assembled by the React form component, which I did
   not trace field by field. The row's id key is `certificationsId` (recovered from the delete
   route at offset 720378, not from the builder). This is the one row shape in the table that is
   BUNDLE-PARTIAL in its *field list*, though its envelope and route are BUNDLE-VERIFIED.
4. **The `Dh` course-type lookup table contents** (`na(a) -> Dh[a]`) and the `Gh` gender lookup
   table contents. Both are controlled vocabularies whose members I did not enumerate.
5. **Which fields the `deleteTracker` spec map covers per form** -- I read the tracker mechanism
   (`Fo`/`Go`) but not the per-form `{section, apiField, hasValue}` spec objects, so I cannot list
   which specific fields support `isDelete`.
6. **Response body schemas.** Only the employment error path was examined.
7. **Whether the v0 and v1 `fullprofiles` routes differ in semantics.** Both exist and both are
   live in shipped code; the v1 route is used by the profile editor for all sections, the v0 route
   by the dashboard preferences drawer with a literal PUT. I did not establish whether v0 accepts
   the section envelopes that v1 accepts, or whether v1 accepts a literal PUT.
8. **`resumeHeadline` vs `profile.resumeHeadline` on read.** I traced writes only; the read shape
   from `getMyProfile` was not examined.
9. **Rate limits, idempotency, and CSRF.** No CSRF token appears in any of the write call sites --
   auth is the `nauk_at` bearer plus cookies -- but I cannot rule out an edge-layer check not
   visible in JS.
10. **Whether `clientid: "d3skt0p"` is required.** It is absent at 2 of the section call sites and
    present at the rest, which suggests optional, but that is an inference from inconsistency, not
    a verified fact.
11. **The mobile web (`m.naukri.com`) contract**, if it differs. Not examined.

---

## 9. Reproduction

Every artefact is a public GET. To re-derive:

```
GET https://www.naukri.com/mnjuser/homepage                                  # 200, Next shell
GET https://static.naukimg.com/s/9/105/_next/app-build-manifest.json         # 200, 22 routes, no /mnjuser/profile
GET https://www.naukri.com/nlogin/login                                      # 200, points at /s/5/105/j/
GET https://static.naukimg.com/s/5/105/j/app_v467.min.js                     # 200, 180348 bytes, loader map
GET https://static.naukimg.com/s/5/105/j/mnj_v320.min.js                     # 200, 848718 bytes, THE bundle
```

The `mnj` version is not pinned forever: `app_v467.min.js` offset 23459 carries the current
`{mnj:"_vNNN"}` and the filename is `mnj` + that version + `.min.js`. Re-read that constant before
trusting the offsets above -- **all offsets in this document are valid only for `mnj_v320.min.js`
at 848718 bytes.**

No personal data is recorded in this document. The bundle is a generic asset served identically to
every visitor; no authenticated response was fetched.
