# Historical Messages reconstruction workplan

Updated 2026-11-08. This is the work order for finishing the configured archive from the source drives reviewed so far. The [architecture record](ARCHITECTURE.md) holds the evidence behind the counts. A historical *source snapshot* is an input to merge; it is not a backup of the current msgbrowse archive. Overlapping messages and media should be reconciled by identity and byte hash; unresolved, owner-eligible records can remain accessible under a clearly labeled review source.

## Current position

The configured archive is `/Users/drew_wilkins/Documents/Messages/archive`: **72,813 messages** after owner-directed transactional cleanup, restoration of a named personal conversation, removal of 18 dental-office Nexus messages, removal of 256 owner-selected ambiguous one-way messages, the iPhone filter-list cleanup, removal of 16 outgoing one-word `stop` messages from `orphaned`, and the checked unsubscribe cleanup as described in [the architecture record](ARCHITECTURE.md). It now contains **1,197 messages in `orphaned`**. Of **14,627** currently cataloged iMessage attachment GUIDs, **14,264** have retained bytes and **363** do not; **220** of the missing GUIDs have visible observations and **143** are hidden. The reduction from the prior 14,628 cataloged GUIDs reflects a later owner-directed message removal, not attachment recovery. A read-only network Mac source supplied 521 of the previously missing GUIDs as 459 distinct permanent files totaling 700,362,021 bytes; every new file passed its SHA-256 name check. The earlier full referenced-media check covered 7,765 unique files and 10,959,636,016 bytes, and the earlier July 2024 recovery checked another 123 unique files. These are separate checks, not a new full-archive hash audit. Possible cross-platform overlaps remain mapped to their verified Nexus source rather than appearing as a third message row. A full current-state backup and restore check completed on KINGSTON; see step 9.

| Source | Already handled | Remaining work |
| --- | --- | --- |
| Current Mac, prior Mac/iPhone snapshots, Seagate and Blue Seagate | Supported TXT/HTML exports and native attachments were merged into the configured archive. Older Drew-labeled backups also supplied 2,134 SMS text records through a private bridge. A July 2024 iPhone backup on Drew's network share supplied 164 more missing attachment GUIDs and 123 unique files. | Reconcile the 884 currently cataloged attachment GUIDs without bytes, skipped legacy rows, and any later source-specific variants. The 97 source-only content candidate GUIDs now have owner-exclusion ledger entries. |
| Black WD Time Machine, Drew profile | Added 600 Mac and 967 iPhone messages plus 196 native attachment variants. Small source database copies and reports remain locally. | Two encrypted iPhone backups were inventoried but not opened. The drive may still be needed for their source bytes. |
| Drew's Nexus SMS Backup & Restore XML on Black WD | Eleven XML files were copied locally with SHA-256 verification. Their 33,884 SMS observations reduce to 8,680 after phone normalization; 1,567 MMS observations reduce to 503 when participants and part hashes are compared. The archive contains 5,001 initially admitted SMS, 488 initially admitted MMS, and 897 recovery records under a visible review source. A private report maps 2,523 likely Apple overlaps; 1,190 duplicate Apple row pairs were removed with GUID tombstones. Forty-seven verified Nexus conversations link to contacts. | Review uncertain recovery conversations and remaining contact matches. Retain the verified source XML and candidate mapping until a full archive backup covers them. The SQLite-only checkpoints do not protect media. |
| Mac iChat archives | SHA-256 copies of 1,849 distinct `.ichat` files from Blue Seagate and Black WD are retained locally. Of 237 unrepresented GUIDs, 118 were imported into a synthetic review conversation with their original Apple GUIDs, two embedded images were retained, and 119 short-code GUIDs were excluded in the ledger. | Recover bytes for five observed transfer GUIDs and eight other attachment markers; find verified historical chats for the review records. These 2014–2024 files do not establish pre-iMessage `.chat` coverage. |
| Older iPhone `sms.db` and `msg_pieces` | Some legacy text and `madrid_attachment` material was recovered. Six Drew-labeled 2010–2013 backups supplied overlapping MMS parts. Checked merges added 65 legacy MMS parents, then 47 legacy SMS review texts and three legacy MMS review parents. The fourth held MMS parent augmented two existing Apple rows with one additional preview file. All 77 previously uncataloged MMS hashes now have permanent bytes. The 56 close SMS variants were verified as already archived. | Review seven unverified address formats, blank payloads, and the two MMS review parents with nonstandard direction flags. Historical Unknown Senders classification remains unavailable. |

The last combined orphan audit before Black WD found 110 conflicting destinations and 1,225 records without a verified nonconflicting chat link. Black WD produced four more conflicting proposals; none was moved. Recompute the categories after the remaining source work instead of adding these counts together. A separate metadata-only archive query found **1,286** groups with repeated time, conversation, sender, and body (1,280 pairs, three triples, three quadruples); every row in these groups has a distinct Apple GUID. **1,253** groups date from 2008–2011. These are duplicate candidates, not proof that different GUIDs represent the same source message. The 1,190 audited Apple pairs are now deduplicated with GUID tombstones; their Nexus metadata matches remain in the source and candidate map without a third visible row.

Phase 1 removed only byte-verified redundant local staging. `reconstruction/2026-11-06-backups` now measures about **4.2 GB** (down from about 30 GB), and the prior restore check is `archive-restore-check-dehydrated` at about **71 MB** (down from 9.1 GB). Neither dehydrated tree is directly restorable or importable. The **9.3 GB** full backup remains untouched. The configured archive is about **13 GB**. Local source databases, 182 source-iOS files with bytes absent from the archive, 78 export-only hashes, 14 isolated-archive-only files, and 78 restore-check-only files were retained for investigation. The Black WD Nexus XML copy is about **579 MB**; other retained Black WD sources and reports remain local. Although about **35 GB** of duplicate logical file sizes were removed, APFS free space did not increase in this check; local Time Machine snapshots or shared blocks may retain the underlying storage. Small SQLite-only checkpoints do not protect attachment bytes.

## Work order

Each source pass follows the same gate: confirm Drew ownership; inventory without exposing message text; compare source identity and media with the archive and exclusion ledger; test in an isolated archive; repeat the import; verify SQLite, counts, search, attachments, and hashes; then merge only the verified additions. A source that adds zero messages may still add attachment bytes. Ambiguous records remain counted and unmerged.

### 1. Reconcile local copies and reclaim space

Completed 2026-11-07: hashed local export, isolated archive, and restore-check media against permanent media. Removed 10,546 byte-verified duplicate export attachment files, 1,763 isolated-archive media duplicates, 574 duplicate derived previews, and 6,589 restore-check media duplicates. Private path/hash manifests and unique bytes remain in the reconstruction workspace. Kept the full backup and all source databases. The exports and isolated archives were marked `dehydrated` because they cannot be used as complete sources or restore targets without rehydration. Export transcripts contain 527 GUIDs absent from both archive and exclusion ledger; the isolated database has 859 rows without a simple body match. Both need reconciliation before further deletion.

**Exit:** exact cleanup candidates, their measured space, and a before/after disk-use report. This step does not create another full local backup.

### 2. Validate and ingest Nexus SMS/MMS

The original Black WD path is `/Volumes/Black WD 1TB- Various/CLOUD ONLY/ Tech Devices Backups and Archives/Nexus/Contact and SMS Backups`; 11 SMS/MMS XML files are hash-verified in `reconstruction/2026-11-07-black-wd-various/source-nexus`. The **9,665** raw date/address/type/body SMS keys reduce to **8,680** after North American phone normalization; the initial isolated run was rebuilt when this additional overlap was found. Sixty normalized date/address/type groups have differing bodies. The earlier raw-key comparison found 2,647 exact Apple time/body/direction candidates, 53 within two seconds, and 530 more with matching phone evidence within five minutes; the normalized importer holds any body/direction match within one day. It also holds identical source messages within five minutes for review rather than deciding whether they are genuine repeats. Its source-specific SHA-256 identity is never an Apple GUID. The **602** MMS records by raw address reduce to **503** when recipient order, phone formatting, and part hashes are compared. Three normalized date/participant/box groups have two distinct part signatures, so both variants remain. The **278** distinct inline assets are absent from permanent media.

The reconstruction-only [`scripts/nexus_sms_import.py`](../scripts/nexus_sms_import.py) keeps Nexus records under their own source and never assigns an Apple GUID. It refuses writes outside a reconstruction path. All **28,846** SMS observations and **1,567** MMS observations with a `readable_date` exactly match Toronto wall time derived from their numeric timestamps; older SMS without that field remain unverified by this check. Its read-only audit classified **5,001 provisional SMS additions** and **3,679 held** from the 8,680 normalized records: 3,008 possible Apple overlaps, 238 short codes, 33 owner-blocked addresses, three owner opt-out records, 130 same-timestamp body conflicts, 76 near-source duplicate candidates, 58 empty bodies, and 133 unverified address shapes. The isolated SQLite copy grew from **68,159 to 73,160** messages. A full repeat and all 11 single-snapshot reruns added zero. A first prototype had admitted 12 held records on partial reruns; persisting held source identities fixed that failure. A second rebuild corrected the raw-address identity; the earlier isolated pass had 971 extra provisional additions from unnormalized phone variants. SQLite integrity, foreign keys, search indexing, loopback home/conversation/search routes, unchanged Apple rows, and unchanged attachment references passed on the corrected copy.

Of the 5,001 isolated SMS additions, 4,452 had a nonnumeric contact name in at least one XML snapshot, 38 had only an address-like label, and 511 had no named-contact evidence. Missing contact metadata is **not** Apple's Unknown Senders classification. A comparison against 695 ledger-excluded Apple rows available in an older restore copy found no Nexus SMS with matching time, body, and direction. All **278** distinct inline MMS assets (**129,486,695 bytes**) were decoded into `staged-nexus-media` and verified by SHA-256.

The 188 exclusion-ledger GUIDs absent from that restore copy were all located in locally retained read-only Apple databases. Thirty-six date from 2010–2014, inside the Nexus source range; each has source text. Six of those Apple GUIDs match four distinct Nexus SMS identities by body, direction, and time within one day. **All four Nexus identities were already held by the importer**, so none entered the isolated archive. The other 152 GUIDs date from 2022–2026, after the Nexus source ends in 2017. This closes the identified ledger coverage gap for the retained source snapshots; it does not establish identity for text altered between platforms.

A metadata-only contact audit found 428 single-address Nexus conversations in the isolated copy. Their addresses matched exactly one existing archive contact identifier in 101 cases (4,138 messages), multiple contacts in 12 cases (26 messages), and no contact in 315 cases (1,092 messages). All 101 uniquely matched archive contacts currently have phone-shaped display names; 63 of those Nexus addresses have a named contact label in an XML snapshot, so zero byte-equal name matches do not establish a conflict. No Nexus conversation was linked to a contact by this audit. Handle reuse, the 12 ambiguous matches, and group identity still require review before contact assignment.

The reconstruction-only [`scripts/nexus_mms_import.py`](../scripts/nexus_mms_import.py) admitted **488** of 503 normalized MMS identities to the same isolated archive, holding eight with unverified participants, four empty records, one possible Apple overlap, and two near-source duplicate candidates. It created **284** attachment references to **275** independently copied files (**127,611,260 bytes**); the other staged bytes remain available for held MMS. All copied files match the SHA-256 in their filenames. A full repeat and 11 single-snapshot reruns added zero messages and zero attachments. The isolated archive now has **73,648** total messages, including **5,489** Nexus SMS/MMS; SQLite integrity, foreign keys, FTS, and the original Apple rows and attachment references passed. The rebuilt local browser returned HTTP 200 for home, a Nexus conversation, search, media gallery, and one MMS attachment; the served attachment bytes matched their SHA-256. Nexus media resolve from `media/nexus`, separate from the Apple media tree. The aggregate private report is `reconstruction/2026-11-07-black-wd-various/reports/nexus-isolated-validation.json`.

Held-record review kept **3,679 SMS** and **15 MMS** out of the merge. Among the 3,008 possible Apple SMS overlaps, 2,904 also match the same one-to-one handle within one day (2,360 exact second, 48 within two seconds, 495 within five minutes, one later that day); 104 match only by body, direction, and time and remain unresolved. The other SMS holds are 238 short codes, 33 owner-blocked addresses, three opt-out records, 130 same-timestamp body conflicts, 76 near-source duplicate candidates, 58 empty bodies, and 133 unverified addresses (90 emails, 40 blank, three other numeric lengths). Four of the 15 held MMS contain inline media; three distinct files totaling **1,875,435 bytes** occur only in held MMS and remain in the verified local staging. None of these held records was added to the configured archive.

Contact-label review of the 428 one-to-one Nexus conversations found 118 addresses with one stable named label across imported SMS (4,424 messages), one address with conflicting labels (35 messages), and 309 without a named label (542 messages). The earlier identifier audit found 101 unique existing-contact matches, 12 ambiguous matches, and 315 without a match. A private, metadata-only review table is `reconstruction/2026-11-07-black-wd-various/reports/nexus-contact-review.csv`. No cross-source contact link was assigned on name or phone alone; the merge keeps Nexus contact links unset so they can be reviewed later. None of the provisional SMS carried the four business labels Drew specifically named (Verizon, Lowe's, United Airlines, AT&T) in its source contact name.

The [`scripts/nexus_checked_merge.py`](../scripts/nexus_checked_merge.py) plan reclassifies all 11 XML files against the current configuration and archive, compares every preexisting message, attachment, and excluded-message ledger row to the isolated baseline, verifies all source media hashes, and records source and Nexus-row digests. Applying a saved plan requires a fresh SQLite-only checkpoint and repeats the checks before inserting. In a separate validation copy, it added exactly **5,489 messages**, **284 attachment references**, and **275 media files**; all 5,489 were FTS-indexed, old rows were bytewise unchanged, SQLite integrity and foreign keys passed, a repeated plan added zero, and a stale plan was rejected. The saved production-target plan at `reconstruction/2026-11-07-black-wd-various/reports/nexus-checked-merge-plan.json` was then applied to the configured archive on 2026-11-07. It added the same **5,489 messages**, **284 attachment references**, and **275 media files** (127,611,260 bytes). A fresh plan found zero messages to add. All 5,489 rows are FTS-indexed; SQLite integrity and foreign keys passed; every new media file matched its SHA-256 filename. The premerge SQLite-only checkpoint is `archive-backups/nexus-premerge-20261107-213938.sqlite` (44 MB), and the merge did not create a full media backup. Black WD is not needed for this local Nexus step; the older Seagate `msg_pieces` databases remain a separate source track.

The initially admitted Nexus records are in the configured archive; the subsequent recovery and contact assignment results are recorded below. The 1,071 Apple attachment GUIDs without bytes and older `msg_pieces` work remain separate tracks.

Follow-up held-record and contact review on 2026-11-07 reclassified all 11 locally copied Nexus XML snapshots against the configured archive after the legacy MMS merge. The accepted set is still exactly **5,489** records already present: **zero newly verified Nexus messages** are awaiting merge. The **3,679 held SMS** and **15 held MMS** remain held. Of the 104 Apple-overlap SMS without a same one-to-one handle match, 103 have an archived group-message candidate with the same body, direction, and nearby time; that is evidence to keep them held, not a safe basis to import another copy. All 90 held email-address SMS have a unique iMessage contact identifier, but 83 also have a body/direction/time Apple overlap. The other seven occur within five minutes of an archived message for the same handle, including five at the exact second; their source snapshots provide no named contact label. They remain held pending identity review. Of eight held MMS with unverified participants, five have a single email participant but no matching archived contact identifier or media bytes; three have group participants whose historical group identity remains unresolved. The other seven MMS holds remain classified as empty, near-source duplicates, or possible Apple overlap. No held MMS was released by this review.

For contact assignment, **47** one-to-one Nexus conversations (**3,864** already archived messages) have three independent matches: one unique archived contact identifier for their normalized address, one stable named label across imported Nexus source observations, and that same name in iMessage sender metadata for the same handle. Source XML was reread to confirm every label. A private, metadata-only checked plan is `reconstruction/2026-11-07-black-wd-various/reports/nexus-contact-checked-plan.json`; its isolated SQLite validation is `nexus-contact-checked-validation.json`. The isolated update linked 47 conversations and added 47 Nexus contact identifiers, changed no messages, attachments, exclusion ledger rows, or contact display names, passed integrity and foreign-key checks, and made zero changes on repeat. The configured archive now has those 47 contact links and 47 Nexus contact identifiers. The remaining contact matches stay unassigned; uncertain held messages were handled by the recovery pass below. This review used local verified copies; Black Seagate was not needed for it.

The Nexus recovery pass compared all source identities to the configured Apple archive by normalized one-to-one handle, displayed second, direction, and exact body. It identified **2,523 possible Apple overlaps**, not proven cross-platform identities: 1,333 match one Apple row, while 1,190 match two rows in the same Apple chat with different GUIDs. The verified local XML and private metadata-only candidate mapping retain this evidence. An initial correction briefly added the 2,523 as separate visible Nexus rows; after Drew identified likely old Nexus-to-Mac sync duplication, a checked rollback removed exactly those additions and their 81 newly created review conversations. No preexisting message, contact, attachment, or media row changed in that rollback.

The archive retains **882 other SMS plus 15 MMS** under `nexus_recovery` in 63 review conversations. The first isolated import and configured checked merge added 897 messages, four attachment references, and three SHA-256-verified media files (1,875,435 bytes); repeating the import added zero. **274** short-code, blocked-sender, or STOP/QUIT source identities remain excluded, including the four Nexus identities matched to excluded Apple GUIDs. The source identity accounting is 5,489 initially admitted + 897 recovery + 2,523 possible Apple overlaps held with source and mapping + 274 owner-excluded = 9,183 normalized SMS/MMS identities.

Drew requested deduplication of the 1,190 Apple row pairs. A metadata and child-row audit found identical conversation, displayed second, direction, text, and other message fields. The ten pairs carrying attachments use the same kinds and retained byte paths; the other 1,180 have none. No pair has links, reactions, or derived message data on the removed row. [`scripts/dedupe_apple_pairs.py`](../scripts/dedupe_apple_pairs.py) made a checked SQLite-only checkpoint, retained the lower-ID Apple row, recorded each removed GUID and hash in `imessage_excluded_messages` to prevent reimport, moved 11 source assets and 11 attachment observations onto kept rows, and removed 1,190 redundant message rows and 11 redundant attachment references. No media bytes were deleted. Isolated and configured checks passed SQLite integrity, foreign keys, FTS, attachment hashes, and representative conversation/search counts. The configured archive had **73,420 messages** at that point. Private plans and validation reports are in `reconstruction/2026-11-07-black-wd-various/reports`. This reviewed rule is limited to the audited 1,190 pairs; it does not claim that every future similar-looking record is the same event.

The 47 verified contact links above apply to the initially admitted `nexus` conversations only. The 897 recovery messages occupy 63 review conversations; 43 one-to-one review conversations (808 messages) have one current Apple conversation with the same handle but remain separate until historical identity is checked.

**Exit:** counts for overlap, excluded, added, ambiguous, and missing media; no additional Nexus messages on a fresh plan, and all newly retained media hash-verified. A full milestone backup and restore remain future work.

### 3. Finish older Apple SMS and MMS

Reaudit skipped legacy `sms.db` rows by address, direction, date, text, group identity, and owner evidence. The 2,134 older SMS records already imported are the comparison baseline. Compare SMS-Tools or another extractor with the raw tables if it helps, but require a field-by-field and byte-for-byte match before relying on its output. Keep parts whose parent, sender, or date is uncertain out of the archive.

Black Seagate was remounted after the Nexus merge preparation. Its June 2017 Time Machine snapshot has 19 MobileSync backup folders; ten carry a Drew label, and the other nine are excluded from this pass because ownership is different or unverified. Eight Drew-labeled backups date from 2009–2013. Their read-only SMS databases are accessible; the 2010–2013 databases contain overlapping `msg_pieces` tables with 88, 131, 165, 275, 305, and 451 rows across successive backups. These are source observations, not six independent message sets.

The 552 image/video part observations all link to a source parent. `Manifest.mbdb` and April 2010 `.mdinfo` metadata resolved every part to file or inline bytes. Their 165 distinct SHA-256 hashes include 101 already cataloged in the archive, each with byte-verified permanent media, and 64 uncataloged. The latter total **14,559,497 bytes** and are retained in `reconstruction/2026-11-07-seagate/staged-legacy-mms`; all 64 stage files passed SHA-256 verification. Six source SMS databases and their file indexes were copied locally (**8,291,635 bytes** including the indexes and owner metadata), rehashed, and passed SQLite integrity checks. The private part report contains no message bodies or addresses. The March 2013 snapshot alone has 149 mapped parts: 148 hashes have archived bytes and the sole uncataloged image belongs to a parent with nonstandard direction flag 33. This supersedes the earlier 147-of-149 file-mapping estimate.

The 64 uncataloged hashes appear in 156 part observations from 94 snapshot-specific parents. Of those, 91 have no archived non-group message with the same exact local timestamp, one-to-one handle, and verified flag-derived direction; three have nonstandard direction flags. Thus the remaining work is primarily a checked import of missing MMS parents, not attachment augmentation of already identified messages. The no-match classification is limited to that exact key and does not prove an archive-wide absence. Reconcile cross-snapshot identity, contact and exclusion rules, group assignments, and each parent/part mapping before a checked merge. No MMS parent or part was added to the configured archive during this audit.

The 94 image/video parent observations collapse to **57** date/flag/normalized-handle keys. Nineteen keys recur across snapshots, with no differing text-part digest or uncataloged-media hash set in this check. Fifty-five keys have the direction flags verified against archived SMS controls (37 incoming, 18 outgoing) and cover 62 of the 64 uncataloged hashes. The other two keys and two hashes retain nonstandard flags. The 55 clear keys resolve to unique existing contact identifiers, although those contacts currently have phone-number display names; this is not evidence of Apple's historical Unknown Senders classification. None of the 94 parent observations uses a five- or six-digit short code, matches an explicit current conversation exclusion, or matches an archived thread with a one-word outgoing `stop`/`quit`.

A follow-up all-type pass found 76 card/location part observations: 33 distinct hashes, including **10 uncataloged** card hashes (47,560 bytes) under **11 additional** parent keys with no exact non-group archive candidate. Among 202 blank-MIME rows, 200 have indexed files, two have inline PNG bytes as well as indexed files, and two have no available payload. Twenty-one of the blank-type uncataloged hashes already occurred in the image/video staging; the remaining inline PNG adds **one** hash (14,363 bytes). Source `text/plain` and `application/smil` payloads remain in the verified local databases. At this stage, staging held **75 distinct files (14,621,420 bytes)**, all SHA-256 verified. The full-payload pass below superseded this count, and the later checked merge applied exclusions and repeat-import tests to the 65 eligible parents.

After the Nexus merge, a second read-only pass accounted for indexed and inline bytes separately when both occur on one image part. Two distinct inline PNG hashes (26,358 bytes) were absent from the prior catalog. The complete private `black-seagate-legacy-mms-full-part-catalog.json` now records **835 payload observations**, **201 distinct hashes**, and two blank-MIME rows without available payloads. Of the 201 hashes, **124** are already cataloged with byte-verified permanent media and **77** are uncataloged. All 77 uncataloged files (**14,647,778 bytes**) are SHA-256 verified in local staging. Their 126 source-parent observations collapse to **69** date/flag/normalized-handle keys; 26 keys recur across snapshots with no full-payload or text-part digest conflict. Sixty-seven keys have direction flags 2/3; two have nonstandard flags and remain held. None of the 67 has an exact timestamp, direction, and one-to-one-handle match in the configured archive after the Nexus merge, and no staged hash overlaps Nexus media. This did not yet prove all 67 safe to import: source text/subject representation, preview handling, cross-source near duplicates, and exclusions still needed an isolated importer and checked merge. Those later checks and the resulting merge are recorded below.

A further parent-level check found **65 provisional** keys and **four held** keys among the 69. Hold reasons overlap: two have nonstandard direction flags, two lack a unique existing contact identifier, one has a nonempty subject whose display representation needs checking, and one shares its body, direction, and handle with an archived message within one day. The 67 standard-flag keys have no configured explicit-conversation, short-code, owner opt-out, or exact `appointment reminder` match in their decoded text parts. That check cannot reconstruct Apple's historical Unknown Senders classification and is not a complete spam review. All 77 staged payloads were preserved while a source-specific identity, preview representation, and isolated repeat import were validated before the 65-parent merge.

The isolated [`legacy_mms_import.py`](../scripts/legacy_mms_import.py) pass reproduced the **65 eligible / four held** classification from copied source databases. It resolved outgoing recipients through `group_member`, checked current exclusions and contact identifiers, and kept a separate `legacy_mms` source identity. Distinct indexed and inline media variants are retained by hash; a secondary inline image variant is exposed as a file rather than a second gallery image. The isolated import added **65 messages**, **74 attachment references**, and **73 files**; an immediate repeat added zero. Existing message and attachment rows were unchanged, all 65 new rows appeared in FTS, SQLite integrity and foreign keys passed, and every copied file matched SHA-256.

The [`legacy_mms_checked_merge.py`](../scripts/legacy_mms_checked_merge.py) plan was applied to the configured archive after a SQLite-only checkpoint at `archive-backups/legacy-mms-premerge-20261107-170759.sqlite`. It added the same **65 messages**, **74 attachment references**, and **73 new files** (**14,026,822 bytes**). A fresh plan found zero more additions. The live archive had **73,713 messages** at that point; all 73 referenced files passed SHA-256 checks, all 65 messages were FTS-indexed, SQLite integrity and foreign keys passed, and the rebuilt local browser returned HTTP 200 for home, a legacy conversation, media gallery, and a sampled attachment whose served bytes matched its hash. The four held parent keys were not merged in that pass. This was a byte-changing milestone with only a SQLite checkpoint; the full DB-plus-media backup and independent restore are still due. The current snapshot manager includes these legacy files under `media/imessage` but does not yet copy the separate Nexus media tree.

A follow-up read-only comparison collapsed **13,686** standard-direction SMS observations to **5,153** distinct keys across the six copies. The final classifier accounts for 4,750 exact archive matches, 246 blank texts, 28 five- or six-digit short codes, 19 owner-blocked addresses, 56 close text variants, seven unverified address formats, and **47** materially different texts. Nine apparent group mismatches were international-number punctuation variants that match archived rows after normalization. The latter were checked in an isolated SQLite copy and merged under `legacy_sms_review` after a SQLite-only checkpoint at `archive-backups/legacy-sms-review-premerge-20261107-2335.sqlite`. Repeat import added zero, all 47 are FTS-indexed, and a fresh plan before the later MMS merge proposed zero more additions. This is a review source: nearby Apple rows with different text remain visible and no Apple GUID or contact match was invented. The 56 close variants were held in the verified source copies for the representation audit below. A private candidate map at `reconstruction/2026-11-07-seagate/reports/legacy-sms-close-variant-candidates.json` records source hashes, nearby archive row IDs, similarity, and lengths without message bodies or addresses.

The full MMS catalog contains **182** date/flag/handle parent keys. Of the **113** keys whose bytes were already cataloged, **112** have an exact-time archive row sharing a media hash; one group or nonstandard-direction key lacks an exact-time destination and still needs review. Of the four keys held from the missing-byte pass, three media-only parents had no exact archived destination and were imported under `legacy_mms_review` without asserting an uncertain contact or direction. The subject-bearing fourth key already matched two equivalent Apple rows at the same second and with the same retained image hash; its distinct inline PNG was attached to both rows as a file, with no third visible message. An isolated pass and disposable checked merge each added three messages, five attachment references, and four distinct files (**620,956 bytes**); repeating the isolated pass added zero. The configured checked merge used a SQLite-only checkpoint at `archive-backups/legacy-mms-gap-premerge-20261107-2343.sqlite`. The archive now has **73,470 messages**, all four files pass SHA-256, FTS indexes all three review messages, SQLite integrity and foreign keys pass, and a fresh MMS plan proposes zero more additions. The rebuilt local browser serves a review conversation and sampled file with HTTP 200 and matching SHA-256. Historical Unknown Senders metadata is still unavailable; review labels are not a claim of sender identity. No full DB-plus-media backup covers the current state. The disposable SQLite and media validation copies were removed after checks, removing 201,192,248 logical bytes; the source copies, reports, and two SQLite-only premerge checkpoints remain. Saved plans require a new isolated copy before reuse.

The read-only representation audit resolved the **56** close SMS variants: each maps to a distinct archived row with the same normalized address, direction, and exact second. Fifty-three target rows are iMessage imports and three are Nexus imports. All 56 texts match after removing an optional leading multipart SMS marker and normalizing whitespace; 14 source texts have that marker. Twenty-three sources have more than one nearby candidate, but their best matching archived rows are distinct. No additional SMS row is justified for these variants. The private metadata-only verification is `reconstruction/2026-11-07-seagate/reports/legacy-sms-variant-validation.json`; the configured archive was unchanged.

**Exit:** an accounted-for result for each skipped text row and MMS part, with repeat-import and retained-byte checks for anything merged.

### 4. Validate Mac `.ichat` and search for older `.chat`

Decode the 1,849 locally retained Apple keyed `.ichat` files in an isolated environment. Use overlapping archived GUIDs to verify timestamp, direction, sender, body, conversation identity, and attachments before considering the 237 unrepresented GUIDs. Apply the same exclusions and preserve message IDs when augmenting an existing GUID. A filename or familiar contact name alone cannot assign a chat. Search separate older Mac sources for genuine pre-iMessage `.chat` files; the retained `.ichat` set starts in 2014, and a scan of Drew's November 2013 Black WD profile found no `.chat` or `.ichat` file. Do not claim pre-iMessage Mac history is complete from those observations.

A read-only Black Seagate filename scan of Drew's complete home folder in all nine 2014 Time Machine snapshots and the June 2017 snapshot found no genuine `.chat` or `.ichat` transcript. Each 2014 snapshot contains only two `.chat` files, 580 bytes total, under a copied `SetupAssistantSupport.framework` resource tree; the 2017 snapshot has none. This closes the filename-based Mac chat lead on this drive, but does not prove that every old conversation is absent from other formats or sources.

The first read-only keyed-archive pass decoded all 1,849 files without parse errors. It found 19,974 distinct message GUIDs: 19,573 already in the configured archive, 164 in the exclusion ledger but absent from the archive, and 237 in neither. All 237 have a nonempty keyed string, including attachment object markers; none has an exact timestamp-and-body match to an archived message. Comparing each file's archived GUIDs to chat IDs initially suggested one in-file chat for **14** of the 237, no archived chat in the file for **222**, and mixed chat evidence for **one**. These are destination candidates, not imported messages; the follow-up validation below narrows what that file evidence proves.
The keyed archive's `PresentityIDs` give one nonowner handle for 226 of the 237, but matching that handle to a current archive conversation is insufficient: 105 yield one apparent one-to-one chat, yet 12 such matches disagree with the chat proven by archived GUIDs in the same file. Keep handle-only matches unassigned until the historical chat mapping is explained.
Matching whole participant rosters across the retained files gives 36 of the 222 without in-file chat evidence one apparent chat, 54 conflicting chats, and 132 with no match. It also conflicts with in-file chat evidence for 13 of the 14 stronger candidates. Treat roster matching as a clue for investigation, not an automatic destination rule.
All 14 stronger candidates are one-to-one chats and have nonempty keyed-archive text. Thirteen use a flag value whose direction is consistent across archived control messages, and 12 use a sender identifier seen with that direction in controls. They occur in three files with 12 archived control messages: all control timestamps and 10 of 12 control bodies match the archive byte-for-byte. The file containing 12 of the 14 candidates has a body mismatch in its sole archived control, so validate its text representation before importing those candidates.

The read-only GUID validation found 237 decodable GUIDs in 238 source observations. One GUID appears twice with the same body and sender but timestamps 11 seconds apart. Their sender `AccountID` and service identifiers occur in archived GUID controls. Flags establish direction for 236; one attachment-only record uses a flag without an archived direction control. **119** have five- or six-digit incoming sender IDs and meet Drew's short-code exclusion. The remaining **118** do not match configured blocked participants, an archived STOP/QUIT participant, or the literal appointment-reminder rule in this source. Fifteen of the 118 contain an attachment object marker. Seven expose a file-transfer GUID, but none of those GUIDs has an attachment observation in the configured archive; byte recovery remains unverified. Of the purported 14 single-chat file matches, **13 point only to the synthetic `orphaned` conversation**, not a verified historical destination. The other points to a named chat but is attachment-only with the uncalibrated direction flag. No `.ichat` record was imported or excluded in the archive by this audit. The 118 need a checked review-source import design or independent historical chat evidence; do not use a current handle or roster to assign a destination. Private metadata-only reports are `reconstruction/2026-11-07-seagate/reports/ichat-validation-metadata.json`, `ichat-room-validation.json`, and `ichat-exclusion-validation.json`.

The checked `.ichat` review import is complete for this retained source set. It placed the 118 owner-eligible GUIDs in the synthetic `iChat review (unassigned)` conversation under the existing `imessage` source, retaining the Apple GUIDs so later exports cannot make duplicate cards. It decoded and retained two embedded images (4,449,688 bytes), recorded seven transfer-GUID observations, and entered the 119 five- or six-digit sender GUIDs in the exclusion ledger. Five observed transfer GUIDs still lack bytes; eight other attachment object markers expose no transfer GUID. The isolated pass, repeat pass, SQLite-only checkpoint, configured checked merge, FTS, integrity, foreign keys, retained-media hashes, and loopback browser checks passed. The checkpoint is `archive-backups/ichat-review-premerge-20261107.sqlite`; private plans are in `reconstruction/2026-11-07-seagate/reports`. A fresh configured plan proposes zero more additions. These review records still need independently verified historical chat assignment; the synthetic conversation is not a real chat. The current archive has **73,588 messages**. No full media backup covers this state.

A follow-up metadata-only assignment audit checked 44 locally copied Apple message databases (36 had usable GUID/chat schemas), the active Mac's Messages database in read-only mode, and 5,258 retained exporter HTML files. **None of the 118 review GUIDs appeared** in those databases or HTML exports, so there is no verified GUID-to-chat move from these sources. The `.ichat` participant roster and filename remain insufficient because their mapping to archived chats has documented conflicts. No archive message or attachment row changed in this audit. Five missing transfer GUIDs matched no attachment row in 37 readable copied databases; no exact-named source file exists in the local reconstruction workspace. One archived attachment has the same filename, but its identity is unverified and it was not linked. Private metadata-only reports are `reconstruction/2026-11-07-seagate/reports/ichat-chat-link-audit.json`, `ichat-export-html-audit.json`, and `ichat-missing-transfer-guid-audit.json`. Keep the review messages and source `.ichat` files until further historical evidence appears.

**Exit:** the retained `.ichat` GUIDs are accounted for as archived review messages or ledger exclusions; unresolved attachment bytes and historical chat assignments remain explicit follow-up work. The separate `.chat` source search has not established complete pre-iMessage Mac coverage.

### 5. Review encrypted iPhone backups without blocking other work

The Black WD review found encrypted iPhone backups dated June 2015 and June 2016. Keep their ownership and manifest metadata in the source catalog. If Drew can unlock them through a secure local prompt, compare their GUIDs and attachment bytes with the archive before staging any import. Do not put a backup password in chat, shell history, or a report. If they remain locked, record the unverified date range and continue the other source tracks.

**Exit:** either a validated overlap/media result or an explicit locked-source limitation.

### 6. Resolve `orphaned` after source reconciliation

Recompute the full multi-snapshot GUID-to-chat audit after new imports. Reject conflicting destinations, including the four Black WD proposals, and short-code targets. Move only records with one nonconflicting source chat link, using a consistent SQLite checkpoint and checked before/after comparison of message IDs and child attachment, link, and reaction references. Present the remainder by missing evidence, conflict, and other reason so Drew can review it. Keep the question of spam deletion separate from chat reassignment; a familiar sender in `orphaned` is not proof of a destination, and an unnamed sender is not proof of spam.

**Exit:** a verified move count, unchanged message and media identities, and a reviewable residual set for Drew's final cleanup.

### 7. Remove owner-identified unknown senders from the remaining `orphaned` records

On 2026-11-07, Drew reviewed every nonnumeric sender label in the remaining
`orphaned` bucket. He chose to keep all named senders except `Lejtman Saul`,
and separately chose to retain the 575 outgoing `Me` rows because their
recipients are unverified. A checked SQLite-only transaction removed **122**
incoming rows: three labeled `Lejtman Saul`, 117 with number-like sender labels,
and two with email-like labels. It first recorded each distinct Apple GUID and
archive hash in `imessage_excluded_messages`, then deleted the rows and their
20 link and six attachment-observation children. None had retained attachment
bytes. At that stage, the 638 selected named-sender rows and all 575 `Me` rows
remained in `orphaned` pending verified chat assignment. The private review and deletion
plans are in `reconstruction/2026-11-07-seagate/reports`; a 49,963,008-byte
SQLite-only predelete checkpoint is in `archive-backups`.

On 2026-11-08, Drew additionally directed removal of outgoing `orphaned`
messages consisting only of `stop` or `remove`. Sixteen exact `stop` rows and
zero `remove` rows met that rule. A checked SQLite-only transaction removed
those 16 and recorded their Apple GUIDs, hashes, and noncolliding TXT
fingerprints in the ledger. The remaining **1,197** `orphaned` messages are
638 selected named-sender rows and **559** outgoing `Me` rows. The private plan
and approximately 50 MB checkpoint are under the Messages reconstruction
directory; no attachment bytes were changed.

On 2026-11-08, a separate unsubscribe cleanup removed 15 messages across 11
conversations: 11 with the exact word `unsubscribe` or `unsubscribed`, plus
four other rows in two short confirmation threads, including the two owner
replies preceding those confirmations. Six conversations became empty and were
removed. A named personal conversation lost only its one matching message.
The 1,197 orphaned messages were unchanged. The private identity-only plan and
SQLite checkpoint are in `reconstruction/2026-11-08-unsubscribe`. The
transaction added 29 GUID/hash/content-fingerprint ledger entries. All
surviving message rows were unchanged; no attachment or source-asset rows and
no permanent media bytes were removed. The archive now has **72,813 messages**
and **2,023 conversations**.

The cleanup also records 122 `content-v1:` fingerprints in the same ledger.
They hash the parsed message second and body, independent of conversation and
sender labels. The 122 fingerprints are unique and have no match among
retained iMessages. The TXT-only importer checks these fingerprints when no
Apple GUID is available, while a paired import uses the GUID first so a
distinct Apple message is not suppressed by a text match.

After the transaction, SQLite integrity, foreign keys, and absence of deleted
IDs from FTS passed. In an isolated copy, a forced TXT/HTML reimport of a real
historical export containing 48 of the removed GUIDs restored none of them.
A separate isolated TXT-only import of the same historical transcript restored
47 removed hashes without a ledger and zero with the ledger. Renaming that
transcript caused the same 47 rows to reappear without the fingerprint ledger;
with the fingerprint ledger, zero reappeared. The other 74
removed GUIDs were absent from retained local `orphaned.html` files, so their
reimport was verified through ledger identity checks and importer code, not a
real-export replay. A TXT-only export that changes message time or body can
still evade content matching; future historical imports must pair TXT with
HTML GUIDs for full deletion safety. Do not claim the TXT-only path gives the
same guarantee.

Drew then authorized a local, archive-wide transactional-text sweep. Two checked
passes initially removed **321** clear notices, codes, reminders, surveys,
campaign or marketing texts, deliveries, and owner-opt-out threads. After he
confirmed that personal conversations should be protected, a checked audit
found 50 removed rows with a person-shaped sender label, Tamar Asayan. All 50
messages and their 14 links were restored with their original IDs; their 100
cleanup-ledger entries were removed. **271 messages remain excluded: 261
iMessage and 10 Nexus.** The classifier now defers conversations with
person-shaped sender labels as well as substantive two-way exchanges, groups,
and the owner-reviewed keep list. A name-like label is a conservative reason
to retain a conversation, not proof of contact identity. One-way numeric and
email-only senders may still lack enough archive metadata to prove whether
they are personal contacts.

The two private identity-only removal plans and a checked personal-restore plan
are in the reconstruction workspace. One verified approximately 50 MB
SQLite-only checkpoint from before both removal passes remains in
`archive-backups`; the redundant second checkpoint was removed to save local
space. Schema v29 guards excluded hashes across sources and excluded Apple
GUIDs. No media bytes were deleted. The archive had **73,195** messages at that checkpoint,
including **1,213** in `orphaned`; the named keep-list and outgoing `Me` rows
there are unchanged. SQLite integrity, foreign keys, FTS row count, targeted
regression tests, doctor, and the local browser check passed. A broader
keyword audit left **267** one-way messages with isolated transactional words
unresolved because those words alone do not prove the message is
transactional. New unseen transactional messages can still enter and need
another review pass.

On 2026-11-08, Drew explicitly selected the ambiguous one-way review set for
removal. Of the original **267** review rows, eight had already been removed
with the dental-office conversation. Three now have exact vCard matches to
personal contacts and were preserved under Drew's standing protection for
personal conversations. The remaining **256** rows were in one-way,
non-group conversations and had no person-shaped contact name. An isolated
SQLite pass removed exactly those IDs and 170 conversations left empty. The
owner-exclusion ledger received **256** exact message hashes and **222**
distinct `content-v1:` fingerprints; 188 of the removed messages also had
Apple GUIDs. The configured pass produced the same counts: **72,921
messages**, **2,039 conversations**, and **3,576** ledger rows. Ten attachment,
13 source-asset, 24 missing-attachment observation, and 46 link references
were removed with their parent messages; no media files were deleted. All
surviving message IDs, bodies, hashes, and conversation assignments were
unchanged. SQLite integrity, foreign keys, FTS counts, exact-hash and GUID
reinsert rejection, and the three personal-message checks passed. The private
plan is `owner-ambiguous-one-way-removal-plan-20261108.json`; the retained
checkpoint for this operation is a roughly 50 MB SQLite-only file in
`archive-backups`. The 35 removed iMessages without GUIDs rely on exact
hashes and TXT content fingerprints; changed source text or timestamps can
evade that fallback. Future paired imports retain the stronger Apple GUID
guard. New unseen one-way messages still require a fresh review.

After the source imports and verified chat reassignment, Drew plans to remove the remaining orphaned messages he identifies as transactional mail or spam. Present the residual records for review by sender and source evidence, with a preview of the exact message and attachment counts affected. Use Apple's Unknown Senders classification only where it can be proven from source metadata; exporter TXT lacks that flag. A missing contact name, a numeric address, or residence in `orphaned` alone must not select a record for deletion.

For the approved set, record Apple GUIDs and content hashes in `imessage_excluded_messages` **before** removing archive rows. Keep the ledger in future backups and make every applicable importer honor it so another source snapshot cannot recreate excluded messages. Verify the transaction's row counts, SQLite integrity, and a repeat import. Check whether media files are shared by retained messages before considering any byte removal. This cleanup affects only the local msgbrowse archive, never Apple Messages or iCloud.

**Exit:** a reviewed removal list, exact deleted and retained counts, preserved exclusion identities, no reappearance on import, and an explained residual `orphaned` count.

### 8. Reconcile completeness across every source

Compare the union of source GUIDs, Android identities, exclusion-ledger entries, archive rows, and attachment observations. For the current 884 attachment GUIDs without bytes, report which source lacks a path, points to a missing file, or has not yet been examined; distinguish visible missing assets from hidden payloads. Check for message revisions, renamed conversations, duplicate content hashes, and unimported group or attachment-only rows. Run a full referenced-media hash check, SQLite integrity, foreign keys, search, and browser spot checks. State every remaining limitation before treating the archive as complete.

A new read-only audit of the current archive found **1,059** observed attachment
GUIDs without retained bytes: 597 visible and 462 hidden. Across 47 locally
copied message databases, 46 were readable. For 900 missing GUIDs, at least
one source records a path but the file is not available at that direct path;
132 have no path in the local copies, and 27 have no observation in those
copies. This does not rule out bytes elsewhere in the retained backup trees.
A full hash pass verified all **7,765** unique referenced media files
(**10,959,636,016 bytes**) with no missing file, symlink, invalid path, or
SHA-256 mismatch. Private reports are in
`reconstruction/2026-11-07-seagate/reports/current-*-audit-20261107.json`.

The same source copies contain **71,400** distinct modern Apple message GUIDs:
64,444 are archived. At the 2026-11-07 audit, 2,511 were in the exclusion
ledger and 4,445 were in neither. Metadata triage narrowed those 4,445 to
2,926 associated
reaction/reply records, 444 nonzero item types, 865 chat-linked short-code
content records, 90 unlinked short-code content records, 23 records with no
text or attachment, and **97 chat-linked content candidates not flagged by
the source-sender short-code test**. Ninety-five of the 97 occur only in the
retained January 2026 Mac database, across 27 chats and dated 2024–2025.
None of those 95 matches an archived message with the same text, direction,
and timestamp within a minute; 93 have no same-direction archive row within a
minute. A follow-up source-chat audit found that **94** belong to 26 chats
where Drew sent a standalone `STOP` or `QUIT`; the remaining **one** has a
five-digit chat identifier. They meet the earlier owner exclusions even
though the first source-sender test did not flag all of them. On 2026-11-08,
all 95 were entered in the exclusion ledger without importing them, so a
future partial export does not need to contain the opt-out reply to reject
those Apple GUIDs.

The other **two** GUIDs occur in older iPhone backups under one eight-digit
source identifier. They have identical text two seconds apart, no nearby
archive row, and do not meet the current strict transactional classifier.
Their eight-digit source address is already in Drew's whole-conversation
blocked list. Both GUIDs were entered in the exclusion ledger on 2026-11-08
without importing a message. The source GUID union now has **2,608** ledgered
GUIDs and **4,348** in neither the archive nor ledger; the remaining classes
above still need interpretation, not blanket import. The private metadata-only reports are
`source-gap-chat-review-20261107.md`,
`source-gap-cross-guid-overlap-20261107.json`,
`source-gap-exclusion-metadata-20261107.json`, and
`source-gap-older-iphone-review-20261107.json` in the same reports directory.

The 267 ambiguous messages at the 2026-11-07 audit were grouped into 199 sender groups
in `transactional-ambiguous-sender-review-20261107.md`, without message
bodies. The direct macOS Contacts request was unavailable. On 2026-11-08,
Drew supplied a local 2,735-card vCard. An exact phone/email cross-check
matched **one** of the 271 previously removed messages to an
automated-style contact label and **11** of the 267 retained ambiguous
messages to four contact labels. Three of those retained rows match
person-shaped contacts; eight match a dental-office contact. None of the
27 January source-candidate chats or the two older iPhone candidates has an
exact vCard identifier match. No archive or exclusion-ledger row changed.
The private contact-name review is
`local-vcard-contact-crosscheck-20261108.md` in the reports directory; its
machine-readable companion has exact matches and no message bodies. A missing
vCard entry does not establish Apple's Unknown Senders classification or spam.

Drew then approved removal of the dental-office conversation. Its **18**
incoming Nexus SMS records occupied one conversation with no attachments,
links, outgoing rows, or other dependent records. A checked SQLite-only
transaction added all 18 source hashes to the exclusion ledger, removed the
18 rows and empty conversation, and left media bytes untouched. The configured
whole-conversation exclusion now covers both the exact phone identifier and
the contact-name export label. The same transaction entered the 97 verified
source-only Apple GUID exclusions described above. An isolated check and the
configured transaction passed SQLite integrity, foreign keys, FTS counts,
and hash/GUID reinsertion guards. The archive has **73,177 messages**, **2,209
conversations**, and **3,098 ledger entries**. The private plans are
`boulevard-east-dental-owner-exclusion-plan-20261108.json` and
`source-guid-owner-exclusion-plan-20261108.json`; the approximately 50 MB
SQLite-only checkpoint is in `archive-backups`.

Exact vCard identifiers also verified three personal contacts. A checked
metadata-only transaction linked **seven** one-to-one conversations covering
**187** unchanged messages to those three contacts, creating one contact and
adding four source-specific identifiers. No message, attachment, or search
record changed. The local browser shows their contact names and returns 404
for the removed dental conversation. The private plan is
`exact-vcard-personal-contact-assignment-plan-20261108.json`; a separate
SQLite-only premerge checkpoint is in `archive-backups`.

A July 2024 iPhone backup on the mounted `drew_wilkins` share has a Messages
database byte-identical to the retained local 2024 source. Its file index
contained **164** exact attachment GUIDs missing from the archive, with
**124,870,938** source bytes. These attachment rows were unlinked to messages
in that backup, so the ordinary source-assets command could not import them.
Every GUID mapped to exactly one existing archive observation and its hidden
flag agreed with the source; the file ID, size, and SHA-256 were checked. An
isolated run added 164 assets, including 98 hidden payloads and 66 visible
ones, and 57 new gallery entries; a repeat added zero. The configured merge
gave the same results. The 164 assets use **123 unique content-addressed
files** totaling **102,742,359 bytes**. SQLite integrity, foreign keys, all
164 mappings, and the retained file hashes passed. Message and conversation
counts did not change. The remaining gap is **895** attachment GUIDs (**531**
visible, **364** hidden). Private reports are
`remote-2024-missing-attachment-index-audit-20261108.json`,
`remote-2024-media-staging-manifest-20261108.json`, and
`remote-2024-media-checked-merge-20261108.json`; disposable source and trial
copies were removed after verification.

A separate read-only index check of the October 2026 iPhone backup on the
same mounted share found **no present files** for the 895 remaining missing
GUIDs: 644 GUIDs occurred in its Messages database, but their recorded paths
were absent, outside the expected SMS attachment path, or lacked a matching
backup manifest entry. This does not prove those bytes are absent from other
backups or exports. The private report is
`remote-2026-missing-attachment-index-audit-20261108.json`.

On 2026-11-08, a read-only copy of the network Mac's `chat.db` and its
attachment tree supplied **521** exact missing GUIDs. A targeted dry run
found 522 source message-to-attachment links, all archived and available;
one attachment was shared by two messages. The checked import added 522
source-asset links and 291 visible gallery references, retaining 459 distinct
content-addressed files (700,362,021 bytes). All 459 paths exist and match
their SHA-256 names. Messages stayed at 72,813; SQLite integrity and foreign
keys passed. The remaining **363** GUIDs are **220 visible** and **143 hidden**.
In the live Mac database, 145 have no path, 17 have a path to an absent file,
and 201 are not present. In the network Mac database, 58 have no path, two
point to absent files, and 303 are not present. Across the earlier 47 local
source-database copies, 97 have no path, 239 have recorded paths whose files
were unavailable in that audit, and 27 were not observed. These source
categories overlap; do not add them. Of the 363, 331 belong only to messages
older than one year, and 149 of those still have a live Mac attachment row
without a local file. The locally retained 2022 iPhone backup
has no present file for these 363 GUIDs. The July 2024 and October 2026
network iPhone backups were already checked against the larger prior gap.

A targeted follow-up on the 2025–2026 subset found **68** missing GUIDs:
**43** from 2025 (31 visible, 12 hidden) and **25** from 2026 (three visible,
22 hidden). An exact attachment-GUID scan of the October 18, 2026 iPhone
backup has **zero matching files**:
47 GUIDs are absent from its attachment table, 20 point to transient
`~/tmp/` paths that are not backed up, and one has no path. The live Mac has
no exact-GUID file for any of the 68; 50 are absent from its attachment
table, 17 point to absent files, and one has no path. **Thirty of the 34
visible IDs are synthetic `at_` IDs**, so a GUID-only lookup overstates the
actual byte gap. A message-GUID comparison found four native-GUID gaps whose
messages each have one other retained same-MIME attachment; those four files
passed SHA-256 checks. One synthetic visible gap has a live Mac attachment
row with an iCloud record identifier but no local file. The owner located it
on the iPhone, but an attempted download returned “Unable to Download Item.”
No bytes were recovered; the cause is unknown. The owner does not need this
image, so it remains an accepted unrecovered gap. The other 29 synthetic
visible source messages are absent from the live Mac database.
A message-GUID scan of the October 2026 backup found one present file among
the 34 visible gaps, already retained for its message. A private,
content-free exact-GUID report is at
`reconstruction/2026-11-08-network-mac-attachments/recent-gap-audit.json`.

Drew elected to stop active recovery of the remaining attachment gaps after
an iPhone download attempt for one visible item failed. Preserve the missing
observations and any later exact owner-eligible bytes, but do not hold the
milestone full backup for further attachment hunting. A later historical
source can still be compared for overlapping messages and media. KINGSTON
remains write-protected by Drew's explicit instruction.

The current Apple-filter check removed one exact screenshot-verified Spam
message and recorded four unarchived GUID guards. A live read-only scan of
the Mac Messages database, including its write-ahead log, finds 692 filtered
GUIDs: 685 ledgered and seven retained under Tamar Asayan. The first scan
had ignored the log and must not be used as a current count. Tamar's mixed
archive conversation has 15 CarePlus-branded messages from `+15512665526`
and 35 other appointment reminders from `+13142607118` (including the
`(smsft)` variant). Preserve all 50 until Drew separates the CarePlus
contact; then review exact identities for number-scoped removal. Do not
remove the entire mixed conversation. Mac filter values are private and do
not prove complete iPhone-category coverage.

For future imports, the configured archive requires paired TXT/HTML and the
matching read-only Apple source database. The importer blocks the five
owner-verified `chat.is_filtered` values (1, 2, 3, 4, 52) by GUID and records
new exclusions in the ledger. New GUIDs with other nonzero values are held for
review; old databases without that optional field remain classified by TXT
rules alone. Do not use the TXT-only export/import path for this archive.

Drew then removed all 50 verified messages from `+15512665526` and
`+13142607118`, with their 14 link rows and no attachment files. The checked
transaction added 100 exact/TXT-fallback ledger entries plus two source-only
GUID guards. The configured archive now has 72,870 messages and 3,684 ledger
entries. Both numbers are configured as exact Apple sender-handle exclusions
for future paired imports. Current Mac filter values 1–4 have no remaining
archived GUIDs and are fully ledgered. Do not blanket-delete the 4,299 archived
GUIDs marked value 1 in at least one older snapshot: 3,940 are value 0 in
another source, so the historical flag is not stable enough for that action.

**Exit:** a source-by-source completeness report with no unexplained losses or duplicate identities.

### 9. Make one milestone backup and prove restoration

Once the reconstruction and cleanup settle, create a full backup containing SQLite, configuration, exclusion ledger, and permanent media at a destination Drew has approved. Restore into a separate directory and verify SQLite integrity, counts, searches, offline browsing, and every referenced file checksum. Then decide which earlier local full copies can be removed and set an ongoing backup cadence based on actual growth. Do not make another 9 GB local full backup after each intermediate import.

**Completed 2026-11-08:** The milestone copy is `/Volumes/KINGSTON/msgbrowse-milestone-2026-11-08` (about 14 GiB on disk). It contains a consistent SQLite snapshot, the private config, both iMessage and Nexus permanent media, derived files, and a SHA-256 manifest for **9,637 files**. Every copied non-DB file matched its source hash. A separate temporary restore on KINGSTON matched every manifest hash, passed SQLite integrity and foreign-key checks, and contained **72,813 messages**, **72,813 FTS rows**, and **6,051 attachment references** whose files all existed in the restored media trees. The restored local browser served home, media, and search with HTTP 200. The temporary restore was removed; the primary backup remains. This full-tree copy was used because the built-in backup manager omits `media/nexus`. Later archive changes need a newer backup; this snapshot covers only the state at creation. Decide later whether older local full copies can be removed and what ongoing backup cadence to use.

**Exit:** an independently restorable archive on an owner-approved medium and a documented ongoing backup procedure.

## Rules throughout

- Read Apple Messages, iCloud, historical backups, and source archives without modifying them. Keep the configured archive append-only except for explicit owner-directed exclusions recorded in the exclusion ledger and checked metadata-only reassignment.
- Exclude other owners' backups. Do not infer Apple's historical Unknown Senders classification from missing Contacts records or a generic filtered flag.
- Use isolated validation for new formats; compare before and after counts, GUIDs, message IDs, and media checksums. Do not expose personal message bodies in logs or reports.
- Avoid routine full local media backups during intermediate work. Use a small consistent SQLite checkpoint for metadata-only moves and reserve a full backup for the approved milestone.
- Record what was verified, what remains uncertain, and the storage impact after each phase. Do not commit or push reconstruction changes unless Drew asks.
