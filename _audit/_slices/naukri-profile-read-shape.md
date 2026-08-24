# Naukri profile read contract - measured shape

Captured 2026-08-24 against the live authenticated session via
`naukri_server/auth_bridge.py` (REST mode, `get_auth_headers()` + aiohttp).
**Every request in this capture was a GET. Zero writes were issued.**

- Endpoint: `PROFILE_API` = `/cloudgateway-mynaukri/resman-aggregator-services/v2/users/self`
- Base: `https://www.naukri.com`
- Query param exercised: `expand_level` at `1`, `2`, `3`, `4`
- All four GETs returned HTTP 200. Zero failures, zero retries.

This document is STRUCTURE ONLY. No field values appear anywhere in it.
The raw PII-bearing bodies live outside this repo at
`mcp-servers/_audit/_data/naukri-fullprofile-raw.json` (gitignored at the
outer level) and are not to be committed here.

---

## Top-level sections at expand_level=4

**Count: 21 top-level sections.**

| # | Section key | JSON type | Elements / keys |
|---|---|---|---|
| 1 | `additionalDetails` | object | 2 keys |
| 2 | `certifications` | array-of-object | 1 element |
| 3 | `disability` | array-of-object | 1 element |
| 4 | `educations` | array-of-object | 1 element |
| 5 | `employments` | array-of-object | 3 elements |
| 6 | `extendedProfile` | object | 78 keys |
| 7 | `groupedEmployments` | array-of-object | 3 elements |
| 8 | `itskills` | array-of-object | 10 elements |
| 9 | `languages` | array-of-object | 5 elements |
| 10 | `lookupData` | object | 15 keys |
| 11 | `noticePeriod` | array | 0 elements (empty) |
| 12 | `onlineProfile` | array-of-object | 1 element |
| 13 | `patent` | array | 0 elements (empty) |
| 14 | `presentation` | array | 0 elements (empty) |
| 15 | `profile` | array-of-object | 1 element |
| 16 | `profileAdditional` | object | 4 keys |
| 17 | `projects` | array-of-object | 1 element |
| 18 | `publication` | array | 0 elements (empty) |
| 19 | `schools` | array-of-object | 2 elements |
| 20 | `user` | object | 15 keys |
| 21 | `workSample` | array | 0 elements (empty) |

Note the name collision: the TOP-LEVEL `noticePeriod` is an empty array, while
`profile[0].noticePeriod` is a populated controlled-vocabulary object. They are
different things.

---

## The expansion ladder (measured, not assumed)

Diffed path-by-path between consecutive levels.

| Level | Top-level section count | What is added relative to the level below |
|---|---|---|
| 1 | 2 | `profile`, `user`. `profile` is an array of length 1 whose single element is **`null`**. |
| 2 | 2 | Same two sections. `profile[0]` changes from `null` to an object with **54 keys**. No new sections. |
| 3 | 20 | Adds **18** sections: `additionalDetails`, `certifications`, `disability`, `educations`, `employments`, `groupedEmployments`, `itskills`, `languages`, `lookupData`, `noticePeriod`, `onlineProfile`, `patent`, `presentation`, `profileAdditional`, `projects`, `publication`, `schools`, `workSample`. |
| 4 | 21 | Adds exactly **1** section: `extendedProfile`. |

Three further measured facts about the ladder:

1. `user` is identical at all four levels (15 keys throughout). It does not
   participate in expansion.
2. Between L2 and L3, `profile[0]` keeps the same 54 keys but its `cvInfo`
   sub-object fills in: `moderationStatus` goes null -> int, `source` goes
   null -> string, `textUploadDate` is ADDED, and `isAvailable` /
   `isTextResume` change. These were four sequential GETs seconds apart in one
   process, so a clock-driven artifact is unlikely but is not excluded by this
   capture. `cvInfo` is the only sub-object inside `profile` that differs
   between L2 and L3.
3. L3 -> L4 changes nothing else at all: the only differing path in the whole
   document is the added `extendedProfile`.

**Consequence for a write layer:** `expand_level=4` is a strict superset of
1, 2 and 3. Nothing is lost by always reading at 4.

---

## Key inventory by section

Legend: `null=N/M` means the field was present but JSON-null in N of M rows.
**In every section measured, `absent=0` for every field: the API returns the
complete key set on every row and represents unset data as JSON `null`, never
by omitting the key.** A write layer can therefore rely on key presence, and
nowhere has to distinguish "null" from "absent".

### `profile` (array-of-object, 1 element) - 54 keys

Row id field: **`profileId`, type `string`, 64 characters, non-numeric.**
The same `profileId` value appears on every row of every other section, so it
is the profile-level anchor, not a per-row id.

| Field | Type | Null |
|---|---|---|
| `absoluteCtc` | string | 0/1 |
| `absoluteExpectedCtc` | string | 0/1 |
| `birthDate` | string | 0/1 |
| `category` | object (vocab) | 0/1 |
| `city` | object (vocab) | 0/1 |
| `contactAddress` | string | 0/1 |
| `country` | object (vocab) | 0/1 |
| `ctc` | object (nested vocab pair) | 0/1 |
| `currency` | string | 0/1 |
| `cvInfo` | object | 0/1 |
| `desiredEmploymentType` | array-of-string (1) | 0/1 |
| `desiredIndustryTypeId` | array (empty) | 0/1 |
| `desiredJobType` | array-of-string (1) | 0/1 |
| `desiredRole` | array-of-object (3, vocab) | 0/1 |
| `disability` | object | 0/1 |
| `entityDepartment` | object (vocab) | 0/1 |
| `entityIndustry` | object (vocab) | 0/1 |
| `entityIndustryTypeId` | object (vocab) | 0/1 |
| `entityRole` | object (vocab) | 0/1 |
| `entityRoleCategory` | object (vocab) | 0/1 |
| `expectedCtc` | object (nested vocab pair) | 0/1 |
| `expectedCtcCurrency` | string | 0/1 |
| `experience` | object `{month:string, year:string}` | 0/1 |
| `functionalArea` | object (vocab) | 0/1 |
| `gender` | string | 0/1 |
| `incompleteSections` | null | 1/1 |
| `industry` | object (vocab) | 0/1 |
| `isActiveProfile` | bool | 0/1 |
| `joinDate` | string | 0/1 |
| `keySkills` | string | 0/1 |
| `lastModAgo` | string | 0/1 |
| `lastModTime` | string | 0/1 |
| `locationPrefId` | array-of-object (8, vocab) | 0/1 |
| `mailCity` | string | 0/1 |
| `maritalStatus` | object (vocab) | 0/1 |
| `name` | string | 0/1 |
| `negPrefIndustries` | null | 1/1 |
| `newLocationPrefId` | array-of-object (8, vocab) | 0/1 |
| `noticePeriod` | object (vocab) | 0/1 |
| `pc` | int | 0/1 |
| `photoInfo` | object | 0/1 |
| `pincode` | string | 0/1 |
| `predictiveFuncAreaId` | int | 0/1 |
| `predictiveResumeHeadline` | string | 0/1 |
| `profileFlag` | string | 0/1 |
| `profileId` | string (64 ch) | 0/1 |
| `profileName` | string | 0/1 |
| `resumeHeadline` | string | 0/1 |
| `role` | object (vocab) | 0/1 |
| `shiftPrefTime` | object (vocab) | 0/1 |
| `summary` | string | 0/1 |
| `videoProfile` | object `{exists:bool, status:null}` | 0/1 |
| `workPermitForCountry` | array (empty) | 0/1 |
| `workStatus` | null | 1/1 |

Sub-object shapes measured inside `profile`:

- `cvInfo` = `{cvFormat:string, fileName:string, isAvailable:bool, isTextResume:bool, moderationStatus:int, size:null, sizeInKB:null, source:string, textUploadDate:string, uploadDate:string}`
- `photoInfo` = `{isAvailable:bool, photoFormat:string, photoURL:string, status:string, uploadDate:string}`
- `disability` = `{isDisabled:string}` (a STRING, not a bool)
- `ctc` and `expectedCtc` = `{lacs:{id:int,value:string}, thousands:{id:int,value:string}}` - a pair of vocabulary objects, not a number
- `experience` = `{month:string, year:string}` (strings, unlike the int-typed durations elsewhere)

Id-shaped fields in `profile` that are NOT row ids: `desiredIndustryTypeId`
(array), `entityIndustryTypeId` (object), `locationPrefId` (array),
`newLocationPrefId` (array), `predictiveFuncAreaId` (int). Only `profileId` is
a row identifier.

### `user` (object, 15 keys)

**No id field of any kind.** This section carries no row id, so it cannot be
addressed per-row on write.

| Field | Type | Null |
|---|---|---|
| `alternateEmail` | string | 0 |
| `canChooseProfileDuringApply` | bool | 0 |
| `communicationSettings` | object | 0 |
| `creationDate` | string | 0 |
| `email` | string | 0 |
| `isEmailVerified` | bool | 0 |
| `isMobileVerified` | bool | 0 |
| `isPremium` | bool | 0 |
| `isSecondaryEmailVerified` | bool | 0 |
| `lastThirtyDaysApplicationCount` | string | 0 |
| `mobile` | string | 0 |
| `resdexVisibility` | string | 0 |
| `residencePhone` | object | 0 |
| `userProperties` | object | 0 |
| `username` | string | 0 |

- `communicationSettings` = `{jobAlert:bool, naukriProductEmail:bool, naukriProductSms:bool, recruiterContact:bool, resumeServiceSmsOrCall:bool}`
- `residencePhone` = `{areaCode:null, countryCode:null, phoneNumber:null}`
- `userProperties` = `{dnaEmv:string, hasCertifications:bool, hasFreeCvSearch:bool, hasInbox:bool, hasVoiceProfile:bool, isFeatured:bool, isVerifiedViaCrederity:bool}`

Note `lastThirtyDaysApplicationCount` is a **string**, not an int.

### `employments` (array-of-object, 3 elements) - 24 keys

Row id field: **`employmentId`, type `string`, 64 characters, non-numeric.**

| Field | Type | Null |
|---|---|---|
| `currency` | null | 3/3 |
| `department` | object (vocab) | 0/3 |
| `designation` | string | 0/3 |
| `designationId` | string (17 ch) | 0/3 |
| `employerVerificationMethod` | null | 3/3 |
| `employerVerificationStatus` | null | 3/3 |
| `employerVerificationTime` | null | 3/3 |
| `employmentId` | string (64 ch) | 0/3 |
| `employmentType` | string | 0/3 |
| `endDate` | string | 0/3 |
| `experienceDuration` | object `{months:int, years:int}` | 0/3 |
| `experienceType` | string | 0/3 |
| `jobDescription` | string | 0/3 |
| `keySkills` | string | 2/3 |
| `location` | object (vocab) | 0/3 |
| `organization` | string | 0/3 |
| `organizationId` | string (7 or 17 ch) | 0/3 |
| `profileId` | string (64 ch) | 0/3 |
| `projectURL` | null | 3/3 |
| `projects` | array (empty) | 0/3 |
| `role` | object (vocab) | 0/3 |
| `roleCategory` | object (vocab) | 0/3 |
| `salary` | null | 3/3 |
| `startDate` | string | 0/3 |

`designationId` and `organizationId` are CATALOG ids (short), distinct from the
64-character row id `employmentId`. `organizationId` was measured with two
different formats across the 3 rows: 7 characters digits-only, and 17
characters non-digit. A write layer must not assume one format.

### `groupedEmployments` (array-of-object, 3 elements) - 7 keys

**No per-row id of its own.** Only `organizationId` (string, 7 or 17 ch).
This section is a derived VIEW over `employments`: its `roles` array holds
elements with the same 24 keys as an `employments` row, including
`employmentId`. Writes should target `employments`, not this.

| Field | Type | Null |
|---|---|---|
| `experienceType` | string | 0/3 |
| `groupEndDate` | string | 0/3 |
| `groupStartDate` | string | 0/3 |
| `organizationId` | string | 0/3 |
| `organizationName` | string | 0/3 |
| `roles` | array-of-object (mirrors an `employments` row) | 0/3 |
| `totalExperience` | object `{months:int, years:int}` | 0/3 |

### `educations` (array-of-object, 1 element) - 14 keys

Row id field: **`educationId`, type `string`, 64 characters, non-numeric.**

| Field | Type | Null |
|---|---|---|
| `course` | object (vocab, 3-key) | 0/1 |
| `courseType` | string | 0/1 |
| `educationId` | string (64 ch) | 0/1 |
| `educationType` | object (vocab, 3-key) | 0/1 |
| `entityInstitute` | object (vocab, string id) | 0/1 |
| `grade` | object (vocab, 3-key) | 0/1 |
| `institute` | string | 0/1 |
| `isPrimary` | int | 0/1 |
| `marks` | string | 0/1 |
| `profileId` | string (64 ch) | 0/1 |
| `projects` | array (empty) | 0/1 |
| `specialisation` | object (vocab, 3-key) | 0/1 |
| `yearOfCompletion` | int | 0/1 |
| `yearOfStart` | int | 0/1 |

`isPrimary` is an **int**, not a bool.

### `schools` (array-of-object, 2 elements) - 12 keys

Row id field: **`schoolId`, type `string`, 48 characters, non-numeric.**

| Field | Type | Null |
|---|---|---|
| `absPercentage` | float | 0/2 |
| `educationType` | object (vocab, STRING id) | 0/2 |
| `profileId` | string (64 ch) | 0/2 |
| `projects` | array (empty) | 0/2 |
| `schoolBoard` | object (vocab) | 0/2 |
| `schoolCompletionYear` | int | 0/2 |
| `schoolEnglishMarks` | string | 0/2 |
| `schoolId` | string (48 ch) | 0/2 |
| `schoolLevel` | int | 0/2 |
| `schoolMathematicsMarks` | string | 0/2 |
| `schoolMedium` | object (vocab) | 0/2 |
| `schoolPercentage` | object (vocab) | 0/2 |

`schools.educationType` uses a **string** vocab id, whereas
`educations.educationType` uses an **int** vocab id. Same field name, different
id type, in two sections. A shared serializer would be wrong here.

### `itskills` (array-of-object, 10 elements) - 10 keys

Row id field: **`skillId`, type `string`, 64 characters, non-numeric.**
`entitySkillId` is the CATALOG id (string, 5 digits-only or 17 non-digit).

| Field | Type | Null |
|---|---|---|
| `entitySkill` | string | 0/10 |
| `entitySkillId` | string (5 or 17 ch) | 0/10 |
| `experienceTime` | object `{month:int, year:int}` | 0/10 |
| `lastUsed` | object (vocab, int value) | 0/10 |
| `mod_dt` | string | 0/10 |
| `profileId` | string (64 ch) | 0/10 |
| `skill` | string | 0/10 |
| `skillId` | string (64 ch) | 0/10 |
| `src` | string | 0/10 |
| `version` | string | 0/10 |

Note `mod_dt` is the only snake_case key in the entire document apart from
`extendedProfile.tags.data[].source_type`.

### `languages` (array-of-object, 5 elements) - 6 keys

Row id field: **`languageId`, type `string`, 64 characters, non-numeric.**
`entityLanguageId` is the CATALOG id (string, 1-2 characters, digits-only).

| Field | Type | Null |
|---|---|---|
| `ability` | array-of-string (3 elements) | 0/5 |
| `entityLanguageId` | string (1-2 ch, digits) | 0/5 |
| `lang` | string | 0/5 |
| `languageId` | string (64 ch) | 0/5 |
| `proficiency` | object (vocab) | 0/5 |
| `profileId` | string (64 ch) | 0/5 |

`ability` is a free array of strings, not a vocab object - the only such field
in the document.

### `certifications` (array-of-object, 1 element) - 14 keys

Row id field: **`certificationId`, type `string`, 48 characters, non-numeric.**

| Field | Type | Null |
|---|---|---|
| `bodytype` | null | 1/1 |
| `certificationBody` | string | 0/1 |
| `certificationCompletionYear` | object (vocab, int value) | 0/1 |
| `certificationId` | string (48 ch) | 0/1 |
| `completionId` | string, **empty** (0 ch) | 0/1 |
| `course` | string | 0/1 |
| `entityCertificateId` | string (17 ch) | 0/1 |
| `expiryDate` | null | 1/1 |
| `issueDate` | null | 1/1 |
| `metadata` | null | 1/1 |
| `profileId` | string (64 ch) | 0/1 |
| `url` | string | 0/1 |
| `vendor` | string | 0/1 |
| `vendorId` | **int** | 0/1 |

`vendorId` is the only int-typed id in this section; every other id here is a
string. `completionId` came back as an empty string, not null.

### `projects` (array-of-object, 1 element) - 19 keys

Row id field: **`projectId`, type `string`, 48 characters, non-numeric.**

| Field | Type | Null |
|---|---|---|
| `clientName` | string | 0/1 |
| `designation` | null | 1/1 |
| `eduExpFlag` | int | 0/1 |
| `eduExpId` | string (1 ch, digits) | 0/1 |
| `employmentType` | string | 0/1 |
| `endDate` | null | 1/1 |
| `isOnSiteProject` | bool | 0/1 |
| `metadata` | null | 1/1 |
| `profileId` | string (64 ch) | 0/1 |
| `projectClientId` | string (8 ch, digits) | 0/1 |
| `projectDetails` | string | 0/1 |
| `projectId` | string (48 ch) | 0/1 |
| `projectLocation` | null | 1/1 |
| `projectTitle` | string | 0/1 |
| `projectUrl` | null | 1/1 |
| `role` | null | 1/1 |
| `skills` | null | 1/1 |
| `startDate` | string | 0/1 |
| `teamSize` | object (vocab, int value) | 0/1 |

`eduExpId` links the project to an education or employment row, but it is a
1-character digit string, NOT one of the 48/64-character row ids. Its
resolution rule is not established by this read capture.

### `onlineProfile` (array-of-object, 1 element) - 5 keys

Row id field: **`id`, type `string`, 48 characters, non-numeric.** This is the
only section whose row id is named plainly `id`.

| Field | Type | Null |
|---|---|---|
| `description` | string | 0/1 |
| `id` | string (48 ch) | 0/1 |
| `profile` | string | 0/1 |
| `profileId` | string (64 ch) | 0/1 |
| `url` | string | 0/1 |

### `disability` (array-of-object, 1 element) - 10 keys

Row id field: **`disabilityId`, type `string`** - but it came back as an
**empty string**, and so did `profileId` in this row. Every other field is
null. This is a blank placeholder row, not real data: the element count of 1
does not mean a disability record exists.

| Field | Type | Null |
|---|---|---|
| `certificateId` | null | 1/1 |
| `certificateType` | null | 1/1 |
| `disabilityId` | string, empty (0 ch) | 0/1 |
| `disabilityPercent` | null | 1/1 |
| `disabilityReason` | null | 1/1 |
| `disabilityReasonId` | null | 1/1 |
| `disabilityType` | null | 1/1 |
| `profileId` | string, empty (0 ch) | 0/1 |
| `rawSubDisabilityId` | null | 1/1 |
| `subDisabilityType` | null | 1/1 |

### `profileAdditional` (object, 4 keys)

Carries `profileId` (string, 64 ch) as its anchor; no separate row id.

| Field | Type | Null |
|---|---|---|
| `desiredJobTypeList` | array-of-string (2 elements) | 0 |
| `fixedCtc` | string | 0 |
| `profileId` | string (64 ch) | 0 |
| `salaryDDId` | **int** | 0 |

`salaryDDId` is an int and looks like a dropdown/vocabulary key.

### `additionalDetails` (object, 2 keys)

No id field. Read-only eligibility flags.

| Field | Type | Null |
|---|---|---|
| `curEmpVerEligibility` | bool | 0 |
| `isAIResumeEligible` | bool | 0 |

### `lookupData` (object, 15 keys)

No id field. Account/entitlement state, not profile content.

| Field | Type | Null |
|---|---|---|
| `ffRDSubExp` | string | 0 |
| `int360RoleExp` | string | 0 |
| `isBogoEligible` | bool | 0 |
| `isFreeServiceEligible` | bool | 0 |
| `isInt360paidUser` | bool | 0 |
| `isJobseekerAgentEligible` | bool | 0 |
| `isLocalityEligibleUser` | bool | 0 |
| `isN360ProEligible` | bool | 0 |
| `isN360ProFreeTrial` | bool | 0 |
| `isNonConFreTrial` | bool | 0 |
| `isPaidUser` | bool | 0 |
| `lastLoginTime` | int | 0 |
| `ppReviewed` | bool | 0 |
| `prevLoginTime` | int | 0 |
| `resumeScore` | int | 0 |

### `extendedProfile` (object, 78 keys)

No id field anywhere in this section. **72 of the 78 keys are JSON null.**
The 6 non-null keys are all wrappers of the identical shape `{"data": [ ... ]}`:

| Key | `data` element count | `data` element keys and types |
|---|---|---|
| `careerBreak` | 1 | `{breakDuration:null, comingFromBreak:bool, description:null, reasonOfBreak:null}` |
| `jbSearchStatus` | 1 | `{id:int, value:string}` (vocab) |
| `mergedPlat` | 2 | `{flag:int, isExternalPlatform:null, label:null, mergedDate:int, platform:string}` |
| `profileSegment` | 1 | `{modHistory:null, profileSegment:string}` |
| `superPremium` | 1 | `{superPremiumFlag:string}` |
| `tags` | 4 | `{id:int, source_type:string, value:string}` (vocab) |

The 72 null keys are domain-specific extension slots (medical, legal,
military, hospitality, logistics, teaching and similar verticals). They are
present-but-null for this account, so their populated types are UNMEASURED by
this capture. Listing them by name:
`accountingType`, `achievements`, `agriRuralDevType`, `auditType`, `awards`,
`bankingSpecialization`, `cemSegment`, `clinicalDuty`, `clinicalSpec`,
`clinicalSubSpec`, `communicationType`, `constProjectCategories`,
`constProjectSubCategories`, `consultingDomain`, `councilRegistration`,
`creativeSpec`, `cuisineCategory`, `cuisineType`, `customAccomplishments`,
`digitalMktSpec`, `dischargeDate`, `energyProjectType`, `enrollmentDate`,
`envProjectType`, `equipment`, `establishmentType`, `exams`, `facilityMgmt`,
`facilitySize`, `facilityType`, `hasCouncilRegistration`,
`hospitalBedCapacity`, `importExportProcess`, `inventoryActivities`,
`logisticsType`, `militaryExperience`, `modeOfTeaching`, `needAccommodation`,
`needStaffMeals`, `newClientType`, `newSalesChannel`, `payrollScope`,
`pharmacySetting`, `pmMethodology`, `portType`, `practiceArea`,
`prefPlaceOfWork`, `procurementCategory`, `productToSell`,
`profileHighlights`, `projectDomain`, `qualityStandards`, `rank`,
`recruitmentType`, `regulatoryBody`, `regulatoryStandard`, `resdexAddPrefs`,
`rndStage`, `salesType`, `sectorFocus`, `securityDomain`, `serviceNumber`,
`serviceType`, `sexualPref`, `subjectSpec`, `teachingBoard`, `treasuryTools`,
`urbanDevType`, `uxSpec`, `vendorSize`, `waterProjectType`, `workDiversity`.

### Empty sections

`noticePeriod`, `patent`, `presentation`, `publication`, `workSample` are all
empty arrays. Their element shape and their row-id field name are **UNMEASURED**
by this capture - an empty array proves only that the section exists.

---

## Row-id summary for a write layer

| Section | Row id field | Type | Measured length | Digits only |
|---|---|---|---|---|
| `profile` | `profileId` | string | 64 | no |
| `employments` | `employmentId` | string | 64 | no |
| `educations` | `educationId` | string | 64 | no |
| `itskills` | `skillId` | string | 64 | no |
| `languages` | `languageId` | string | 64 | no |
| `certifications` | `certificationId` | string | 48 | no |
| `projects` | `projectId` | string | 48 | no |
| `schools` | `schoolId` | string | 48 | no |
| `onlineProfile` | `id` | string | 48 | no |
| `disability` | `disabilityId` | string | 0 (empty) | n/a |
| `profileAdditional` | (none; anchored by `profileId`) | - | - | - |
| `groupedEmployments` | (none; derived view) | - | - | - |
| `user` | (none) | - | - | - |
| `additionalDetails` | (none) | - | - | - |
| `lookupData` | (none) | - | - | - |
| `extendedProfile` | (none) | - | - | - |
| `noticePeriod`, `patent`, `presentation`, `publication`, `workSample` | UNMEASURED (empty) | - | - | - |

**Every row id measured is a STRING, never an int.** The only int-typed
`*Id`-named fields in the whole document are `certifications[].vendorId`,
`profile[0].predictiveFuncAreaId`, and `profileAdditional.salaryDDId` - and
none of those is a row id; they are catalog keys.

---

## Controlled vocabularies

These fields come back as an id plus a label. They **cannot be free-texted on
write**; a write layer must send the id from Naukri's catalog.

Seven distinct vocab shapes were measured:

| Shape | Where measured |
|---|---|
| `{id:int, value:string}` | `profile`: `category`, `country`, `entityDepartment`, `entityIndustry`, `entityIndustryTypeId`, `entityRole`, `entityRoleCategory`, `functionalArea`, `industry`, `noticePeriod`, `role`, `desiredRole[]`; `languages.proficiency`; `schools.schoolBoard`, `schoolMedium`, `schoolPercentage`; `extendedProfile.jbSearchStatus.data[]` |
| `{id:int, subValue:string, value:string}` | `profile.city`; `educations`: `course`, `educationType`, `grade`, `specialisation` |
| `{id:string, value:string}` | `profile.maritalStatus`, `profile.shiftPrefTime`; `educations.entityInstitute`; `schools.educationType` |
| `{id:int, value:int}` | `certifications.certificationCompletionYear`; `itskills.lastUsed`; `projects.teamSize` |
| `{id:int, value:null}` | `employments`: `department`, `location`, `role`, `roleCategory` |
| `{id:int, type:null, value:string}` | `profile.locationPrefId[]`, `profile.newLocationPrefId[]` |
| `{id:int, source_type:string, value:string}` | `extendedProfile.tags.data[]` |

Three consequences worth flagging:

1. **The vocab id type is not uniform.** Most are int, but `maritalStatus`,
   `shiftPrefTime`, `entityInstitute` and `schools.educationType` use a string
   id. A write layer that types every vocab id as int will be wrong in four
   places.
2. **`employments` vocab objects return `value: null`.** The read gives the id
   but withholds the human-readable label for `department`, `location`, `role`
   and `roleCategory`. Those labels must come from a separate catalog lookup;
   they cannot be round-tripped from this read.
3. **`ctc` and `expectedCtc` are vocab pairs, not numbers**: each is
   `{lacs:{id:int,value:string}, thousands:{id:int,value:string}}`. Salary is
   selected from dropdowns, not typed.

Separately, the CATALOG (as opposed to row) id fields are `entityCertificateId`
(17 ch), `entitySkillId` (5 or 17 ch), `entityLanguageId` (1-2 ch digits),
`designationId` (17 ch), `organizationId` (7 ch digits or 17 ch non-digit) and
`projectClientId` (8 ch digits). Their formats are inconsistent with each other
and with the row ids.

---

## Method probes

Three plain GETs against the `fullprofiles` route family, captured with raw
aiohttp so status, full response headers and body were all readable.

| url | status | `Allow` header | body excerpt |
|---|---|---|---|
| `https://www.naukri.com/cloudgateway-mynaukri/resman-aggregator-services/v0/users/self/fullprofiles` | 405 | **absent** | `{"message":"HTTP 405 Method Not Allowed","statusCode":405,"validationErrors":[],"data":null}` |
| `https://www.naukri.com/cloudgateway-mynaukri/resman-aggregator-services/v1/users/self/fullprofiles` | 405 | **absent** | `{"message":"HTTP 405 Method Not Allowed","statusCode":405,"validationErrors":[],"data":null}` |
| `https://www.naukri.com/cloudgateway-mynaukri/resman-aggregator-services/v2/users/self/fullprofiles` | 405 | **absent** | `{"message":"HTTP 405 Method Not Allowed","statusCode":405,"validationErrors":[],"data":null}` |

The body excerpts above are the COMPLETE bodies, not truncations: all three
were 92 bytes.

`Allow` is absent on all three responses. This was checked case-insensitively
against the complete header set, which for all three responses was exactly:
`Server`, `Content-Type`, `Content-Length`, `X-EdgeConnect-MidMile-RTT`,
`X-EdgeConnect-Origin-MEX-Latency`, `Date`, `Connection`, `Set-Cookie`,
`X-Content-Type-Options`, `Strict-Transport-Security`. No `Allow` under any
casing.

**What these probes do and do not establish:** they establish only that all
three path variants reject GET with a 405 and that the server omits the
`Allow` header that RFC 9110 requires on a 405, so they yield ZERO evidence
about whether the write method is POST, PUT or PATCH - and because the 405
body, the header set and the `Content-Length` are identical across v0, v1 and
v2 (including versions that may not be routed at all), these responses do not
even establish that a `fullprofiles` write route exists at any specific
version.

Determining the write verb therefore needs a different instrument than a 405
header read. That instrument is not built here, and nothing in this capture
should be read as narrowing the verb.
