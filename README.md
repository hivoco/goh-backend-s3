# Grains of Hope — Backend (FastAPI)

India Gate Basmati's **"Grains of Hope"** campaign. Two things happen on the
microsite:

1. **Share** — one tap of the Share button = **one plate of food for a child**.
   Every tap posts here and increments the counter.
2. **Join the initiative** — a form (photo + name + gender + language + mobile +
   T&C). The photo must show **exactly one person looking straight into the
   camera**. If the number is already OTP-verified the entry is queued
   immediately; otherwise a WhatsApp OTP is sent and the entry waits.

This service is the hub: the share counter, photo validation, OTP/auth, entry
creation, the admin/jobs APIs, the versioned pipeline config, and reporting. The
actual photo/video/stitch rendering is done by a **separate worker** that picks
up `queued` jobs (MySQL lock-and-advance) and writes asset URLs back.

## Stack
FastAPI · SQLAlchemy · MySQL 8 (`grains_of_hope`) · **AWS ElastiCache (Redis OSS)** ·
S3 (boto3) · Groq/OpenAI/Gemini vision · WhatsApp (Gupshup)

## Layout
```
app/
  core/        config, database, redis (ElastiCache), security, s3, otp, admin_auth,
               timezone, geoip (offline IP → city)
  models/      user, user_verification, user_otp, job, job_assets, pipeline_config,
               config_audit, vision_config, app_settings, share_event, admin_user,
               job_device
  routers/     share (public + admin), video (submit), auth (otp), photo_validation,
               jobs (admin + reports), config, vision, settings, admin_auth, admins
  services/    config_service, vision_service, settings_service, otp_service, admin_service,
               share_service (share ↔ entry attribution)
  main.py      app wiring + startup checks + seeds (config, vision, settings, super-admin)
sql/schema.sql            canonical bootstrap for a FRESH database
migrations/001_*.sql      share_events + app_settings + vision_config
migrations/002_*.sql      admin_users (panel logins)
migrations/003_*.sql      job_devices (links an entry to the browser that shared)
migrations/004_*.sql      adds the `client` value to the jobs.status ENUM
migrations/005_*.sql      repeat entries: `repeat` status, repeat_count/_of, job_face_embeddings
migrations/006_*.sql      jobs.ip_address / city / consent_version / consent_ts
scripts/create_admin.py   create / reset / list admin accounts from the CLI
tests/smoke_test.py       188 end-to-end checks against a throwaway SQLite DB
inspect_schema.py         read-only "does the code match the DB?" diff
```

The **face-embedding worker** that detects repeat entries is not in here. It is
a **separate application** in `../embedding-worker/` with its own settings,
engine, models and tests, deployed to its own instance. The two share no code —
only the MySQL schema, defined by `migrations/*.sql`. Nothing in this backend
reads `job_face_embeddings`; it only exposes `repeat_count` / `repeat_of_job_id`
on the jobs API and lets the panel edit the similarity threshold.

---

## Setup

### 1. Run the migrations (do this first)
The campaign database `grains_of_hope` already exists with `users`,
`user_verification`, `user_otp`, `jobs`, `job_assets`, `pipeline_config`,
`config_audit` and `story_durations`. This codebase adds five tables it doesn't
have yet:

| Table | Migration | Why |
|---|---|---|
| `share_events` | 001 | one row per Share tap — the plates-of-food counter |
| `app_settings` | 001 | admin-editable runtime settings (single JSON row) |
| `vision_config` | 001 | the versioned photo-check model + prompt |
| `admin_users` | 002 | panel logins — the super-admin and the admins it creates |
| `job_devices` | 003 | ties an entry to the browser it came from, so shares and entries can be matched up |
| `job_face_embeddings` | 005 | one face vector per entry — written by the embedding worker, not by this API |

```bash
PYTHONPATH=. .venv/bin/python scripts/run_sql.py migrations/001_share_and_admin_tables.sql
PYTHONPATH=. .venv/bin/python scripts/run_sql.py migrations/002_admin_users.sql
PYTHONPATH=. .venv/bin/python scripts/run_sql.py migrations/003_job_devices.sql
PYTHONPATH=. .venv/bin/python scripts/run_sql.py migrations/004_jobs_status_client.sql
PYTHONPATH=. .venv/bin/python scripts/run_sql.py migrations/005_repeat_entries.sql
PYTHONPATH=. .venv/bin/python scripts/run_sql.py migrations/006_jobs_ip_city_consent.sql
```

`run_sql.py` applies a file through PyMySQL — the same driver the API uses — and
`--dry-run` lists the statements without executing them. Use it rather than the
`mysql` CLI: Homebrew MySQL 9.x dropped the `mysql_native_password` plugin this
RDS user needs, so `mysql -h … < file.sql` fails on macOS with
`ERROR 2059 … Authentication plugin 'mysql_native_password' cannot be loaded`.
Both files also `USE grains_of_hope;` explicitly, so pasting them into a GUI
client can't silently no-op with 1046 "No database selected".

Both are `CREATE TABLE IF NOT EXISTS` only — they change no existing column and
are safe to re-run. **Until they're run**, the API still boots and OTP/entries
work, but the share counter, the photo check and the admin login don't (startup
prints a `[WARN]` naming the missing migration).

For a *fresh* environment (staging clone, local copy) use `sql/schema.sql`
instead — it builds everything from scratch, including the in-DB watchdog.

### 2. Install and configure
```bash
cd backend
python3.10 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

cp .env.example .env        # then fill it in
# Fernet key:
#   .venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# The bootstrap super-admin: either paste a bcrypt hash into
# SUPERADMIN_PASSWORD_HASH, or skip the hash and create the account after the
# first boot with `scripts/create_admin.py` (see "Admin accounts" below).

./start_server.sh           # uvicorn on 127.0.0.1:8000 (docs at /docs in dev)
                            # PORT=8001 ./start_server.sh to move it
```

Check the code and the database agree at any time:
```bash
PYTHONPATH=. .venv/bin/python inspect_schema.py
```

### 3. Verify
```bash
PYTHONPATH=. .venv/bin/python tests/smoke_test.py     # 188 checks, no external services needed
```

---

## AWS ElastiCache (Redis OSS)

Redis backs rate limits, the photo-validation feature flag, OTP/pending caches,
Groq key round-robin, and the share counter's read cache. This campaign runs it
on **ElastiCache Serverless** (`test-du95w6.serverless.aps1.cache.amazonaws.com`,
Redis OSS 7.1, ap-south-1), which has three hard requirements:

```ini
REDIS_HOST=test-du95w6.serverless.aps1.cache.amazonaws.com   # no :6379 suffix
REDIS_PORT=6379
REDIS_SSL=true          # mandatory — Serverless refuses plaintext
REDIS_DB=0              # only db 0 exists; SELECT doesn't exist in cluster mode
REDIS_PASSWORD=         # AUTH token / RBAC password, blank if none
#REDIS_CLUSTER=         # leave unset — auto-detected from the hostname
```

**Serverless always runs in cluster mode**, so the client has to be
cluster-aware. `app/core/redis.py` builds `RedisCluster` when
`settings.redis_use_cluster` is true and plain `redis.Redis` otherwise, and that
flag is inferred from the endpoint (`.serverless.` or a `clustercfg.` prefix)
unless `REDIS_CLUSTER` overrides it.

This matters more than it looks: a **standalone client against a cluster
endpoint appears to connect**, then errors on any key that doesn't hash to the
node it landed on. Because every helper here degrades to a no-op, that failure
mode looks exactly like "the cache is switched off" — rate limiting and the
share counter would quietly stop working with nothing in the logs pointing at
the cause. Hence the auto-detect, and the startup banner that prints the
resolved mode (`cluster` / `standalone`, `TLS` / `plaintext`).

Other notes:

- **Adding Redis commands?** Every pipeline in `redis.py` batches commands on a
  *single* key, so none can trip `CROSSSLOT`. Keep it that way, or route
  explicitly. Server commands like `TIME` are avoided for the same reason —
  they have no single target in a cluster.
- The cache lives inside the VPC and is **not reachable from a laptop**. Use a
  local Redis for development (`REDIS_SSL=false`, standalone auto-detected) and
  the serverless endpoint on EC2, whose security group must be allowed inbound
  on the cache's.
- **Nothing here is load-bearing.** Every helper degrades to a no-op / "allow"
  when the cache is unreachable, and after a failed connect the client stops
  re-dialling for 15s so an outage can't stall requests on connect timeouts.
- The share count always has MySQL as its source of truth; Redis only saves a
  `COUNT(*)` per page view and is re-seeded from the DB whenever the key is cold.

---

## Key endpoints

### Public (the microsite)
| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/share` | Record one share → returns the running total + meals |
| GET | `/api/v1/share/count` | The public counter (`?fresh=true` bypasses the cache) |
| POST | `/api/v1/photo-validation/check_photo` | Validate the photo → signed token |
| POST | `/api/v1/video/submit` | Submit the form + photo; sends the OTP / creates the entry |
| POST | `/api/v1/auth/verify-otp` · `/resend-otp` | OTP verification |
| GET | `/api/v1/settings/photo-validation-status` | Is the photo check currently on? |

### Admin (JWT via `/api/v1/admin/login`)
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/jobs/list` · `/{id}` · `/stats/summary` | Entries |
| PATCH | `/api/v1/jobs/update-job` · `/{id}/fields` · `/{id}/video-url` | Edit an entry |
| GET | `/api/v1/jobs/reports/stats` · `/trend` · `/traffic-sources` · `/csv` | Reporting |
| GET/PATCH | `/api/v1/jobs/settings/photo-validation` | Photo-check on/off toggle |
| GET | `/api/v1/share/stats` | Share analytics for the Reports page |
| PATCH | `/api/v1/jobs/settings/photo-validation` | **Super-admin:** turn the photo check on/off (reading it is open to any admin) |
| GET | `/api/v1/share/participation` | Shared-only / shared+requested / requested-only, per device |
| GET/POST | `/api/v1/config/...` | Pipeline config: active/list/audit/options, create, rollback, pause |
| GET/POST/DELETE | `/api/v1/vision/...` + `/api/v1/change/prompt/vision` | Vision model config |
| GET/PATCH | `/api/v1/settings/backend` | Runtime settings (API-only, no admin page) |
| POST | `/api/v1/admin/login` · GET `/me` | Sign in, whoami (any role) |
| POST | `/api/v1/admin/change-password` | **Super-admin:** change your own password |
| GET/POST | `/api/v1/admins` | **Super-admin:** list / create panel logins |
| PATCH | `/api/v1/admins/{id}/password` · `/active` · `/role` | **Super-admin:** reset a password, enable/disable, promote/demote |
| DELETE | `/api/v1/admins/{id}` | **Super-admin:** delete an account |

`POST /api/v1/jobs/{id}/send-video` is separate: it's guarded by a static
`X-API-Key` (`SEND_VIDEO_API_KEY`) so the worker or an external system can
trigger WhatsApp delivery with just an entry id.

Roles: a plain **admin** manages entries — nothing else. Only a **super-admin**
can edit the pipeline config, the vision model, the backend settings, or any
account (including setting every password on the panel).

---


## Admin accounts

Panel logins live in the **`admin_users` table**, not in `.env`. There is exactly
one bootstrap step, then everything happens in the panel:

1. On first boot, if the table has no active super-admin, one is seeded from
   `SUPERADMIN_USERNAME` / `SUPERADMIN_PASSWORD_HASH`. After that those env
   values are ignored.
2. That super-admin signs in and uses the panel's **Admins** page to create as
   many admins (or further super-admins) as needed, set their passwords, disable
   them, or delete them.
3. **A plain admin cannot change their own password.** Every password is set by
   a super-admin — at creation, and afterwards via
   `PATCH /api/v1/admins/{id}/password`. `POST /api/v1/admin/change-password` is
   super-admin-only (and still demands the current password), so the super-admin
   isn't stuck asking someone else to rotate their own credential.

No bcrypt hash to hand? Create or reset an account interactively instead:

```bash
PYTHONPATH=. .venv/bin/python scripts/create_admin.py --username super-admin --role superadmin
PYTHONPATH=. .venv/bin/python scripts/create_admin.py --username priya --reset
PYTHONPATH=. .venv/bin/python scripts/create_admin.py --list
```
The password is prompted for, never passed as an argument — an argument would
land in shell history and in `ps` output.

**How sessions are revoked.** A JWT carries the account's `token_version`, which
is bumped on every password change. Each request re-reads the account from a
30-second cache of the DB (`ADMIN_CACHE_TTL`), so:

| Action | Effect on existing sessions |
|---|---|
| Change / reset a password | signed out **immediately** (version no longer matches) |
| Deactivate or delete | signed out within 30s |
| Promote / demote | new role applies within 30s, no re-login needed |

The role is always re-read from the database — never trusted from the token
alone.

**Lock-out guards.** A super-admin cannot deactivate, demote or delete their own
account, and the *last active super-admin* cannot be removed, demoted or
disabled. Passwords are 8–72 characters (bcrypt silently ignores anything past
72 bytes, so longer ones are rejected rather than truncated).

---

## WhatsApp (Gupshup)

Four messages, one endpoint — only the template id and its params differ:

| message | template | params |
|---|---|---|
| OTP | `GUPSHUP_OTP_TEMPLATE_ID` | `[otp]` |
| confirmation (on submit) | `GUPSHUP_CONFIRM_TEMPLATE_ID` | `[name]` |
| video delivery | `GUPSHUP_VIDEO_TEMPLATE_ID` | `[name]` + a video header |
| failure notice | `GUPSHUP_FAILED_TEMPLATE_ID` | none — `params` is omitted entirely |

```
POST https://api.gupshup.io/wa/api/v1/template/msg
apikey: <key>            Content-Type: application/x-www-form-urlencoded
channel=whatsapp  source=<business number>  destination=<user>  src.name=<app>
template={"id":"...","params":["..."]}
message={"type":"video","video":{"link":"..."}}      # media templates only
```

Things worth knowing:

- **`params` is POSITIONAL.** It fills `{{1}}`, `{{2}}`… in the template as
  approved by Meta, so the order here must match the registration.
- **Numbers go out international, no `+`.** The campaign stores 10 digits;
  `_format_phone` prepends `GUPSHUP_COUNTRY_CODE` and copes with values that
  already carry it, or a leading 0.
- **A 200 is not proof of delivery.** Gupshup can answer 200 with
  `{"status":"error"}`, so the body is checked too — otherwise a job would be
  marked `sent` for a message that never arrived.
- **A blank template id disables that message** (skipped and logged) rather than
  firing a request that can only fail.
- **A template with no variables omits `params`** rather than sending `[]` —
  that's the shape Gupshup accepted for the failure notice.
- **The video link must be publicly reachable** — Gupshup fetches it
  server-side. The caller passes the CloudFront URL, not the S3 one.
- Nothing raises: a WhatsApp outage never fails a submit or a delivery.

## Rate limits

Anti-abuse only, and fail-open: `check_rate_limit` returns "allowed" when Redis
is unavailable, so a cache outage can never stop the campaign.

| Endpoint | Limit | Per | Window |
|---|---|---|---|
| `POST /video/submit` | **15** | mobile number | 5 min |
| `POST /auth/verify-otp` | 10 | mobile number | 5 min |
| `POST /auth/resend-otp` | 3 | mobile number | 10 min |
| `POST /share` | `SHARE_MAX_PER_MINUTE` | device id | 1 min |
| `POST /video/submit` (global) | 2,000,000 | everyone | 1 min |

The submit limit is generous on purpose: a participant retaking a photo that
keeps failing the check burns several attempts legitimately, and a real person
hitting the wall is a worse outcome than a scripted one getting a few extra
tries. It's a **fixed** window, not rolling — the counter starts on the first
submit and expires 5 minutes later, so someone who spends all 15 in ten seconds
is free again well before the message's countdown suggests.

Only `SHARE_MAX_PER_MINUTE` is in `.env`; the rest are constants at the top of
their routers.

## Share counting

**Every tap counts. There is no de-duplication.** A repeat tap from the same
device is a real share and earns a real plate — that was the product decision.
`device_id` is still recorded, so the admin Reports page shows unique devices
next to the raw total.

The only limit is anti-flood: `SHARE_MAX_PER_MINUTE` (default 60) requests per
device per minute, so a script can't mint a million plates in a second. Under
that cap, nothing is dropped.

**A video request earns a plate too.** Requesting a video adds one plate on top
of any share taps, so someone who shares once and then joins generates two. It
is written as a `share_events` row with channel `video_request`, at the moment a
new `jobs` row is created — and nowhere else, so re-sending an OTP or hitting a
"still processing" response can't farm the counter.

**The Reports chart plots the two apart.** `share/stats.trend` counts share-BUTTON
taps only; the per-entry plates belong to the entries series next to it. Counting
them in both would draw every entry twice and the share bar could never sit below
the entry bar. `total_shares`, `meals` and `by_channel` still include them — those
are the campaign totals, and `by_channel` is where you see the split.

Two knobs shape the public number without touching the data:
- `MEALS_PER_SHARE` — plates credited per share (default 1).
- `SHARE_COUNT_OFFSET` — added to the **public** counter only (e.g. plates
  already pledged offline). Admin reports always show the true count, and the
  Reports page calls out the offset when it's non-zero.

### Matching shares to entries

A share is anonymous (no phone yet) and an entry is identified (phone → user →
job). The only value present at **both** moments is the `device_id` the frontend
keeps in `localStorage`, so that is the join key: `/api/v1/video/submit` accepts
it and stores it in `job_devices` alongside `shares_before` — how many times that
device had already shared when the request came in, frozen at request time.

`GET /api/v1/share/participation` turns that into the three groups: shared only,
shared **and** requested, requested only — plus `no_device_recorded` for entries
made before this existed or from browsers that block storage.

> ⚠️ **The trap.** The video-request plate is itself a `share_events` row, so
> "is this device in share_events?" marks every requested-only visitor as a
> sharer. Use `services.share_service.real_share_only()`, which excludes the
> `video_request` channel; never re-write that filter inline. It's why
> `video_request` is deliberately absent from `SHARE_CHANNELS` too — a client
> posting it to `/api/v1/share` is normalised to `other` instead.

`device_id` identifies a browser, not a person: cleared storage, incognito, or
sharing on a phone and filling the form on a laptop splits one human across two
groups. Treat the numbers as a trend, and keep the caveat next to them wherever
they're shown.

## Photo validation

The uploaded photo must contain **exactly one person**, face clearly visible, and
**looking straight into the camera** — not tilted up or down, not turned left or
right. The vision model (provider/model/prompt from the active `vision_config`
row, editable in the admin panel) fills a structured schema:

```
number_of_people · face_visible · looking_at_camera · head_direction
eyes_open · quality_ok · is_real_photo · is_appropriate · face_unobstructed
```

`decide()` in `routers/photo_validation.py` turns that into an accept/reject and
a specific, actionable message ("You're looking down — please lift your chin and
look straight into the camera"). A pass issues a short-lived HMAC token that
`/video/submit` requires, so the photo can't be swapped after the check.

Admins can toggle the check off from the Entries page; it also **auto-disables**
if the vision provider is fully saturated or erroring. With the check off, new
entries land in `unverified` instead of `queued` so nothing unvetted reaches the
renderer unnoticed.

## When the vision provider is slow

The photo check is the only request in this service that waits on a third party
for seconds at a time, and it is the one that takes the whole service down with
it if it is allowed to wait forever. Two guards, both added after an outage:

- **`VISION_TIMEOUT_SECONDS` / `VISION_MAX_RETRIES`** in
  `app/routers/photo_validation.py`. Both were previously unset, and langchain's
  clients default to `max_retries=6` with exponential backoff and no timeout. A
  provider answering `503 UNAVAILABLE` therefore took **99s and 181s** to fail
  rather than ~2s — measured. With the caps the same failure returns in 1.8s.
- **The DB session is closed before the call.** `check_photo` needs the session
  only to read the active vision config; holding it across the model call meant
  requests that were merely waiting on a third party consumed the pool (10 + 20
  overflow), and every other endpoint blocked on checkout.

Both matter beyond this endpoint, because **Starlette's threadpool is 40 threads
shared by every `def` endpoint and every `run_in_threadpool` call.** A vision
call that occupies a thread for three minutes starves OTP verification, the
share counter and the admin panel alike. That is not hypothetical — it is what
"OTP verification is timing out" turned out to be.

**Diagnosing a slow provider.** `analyze_photo` logs elapsed seconds on both
success and failure, which is the first thing to read: it separates "the
provider refused us" (fast) from "the provider is degraded" (slow). If every
model 503s, test a **text-only** call with the same key before blaming the key —
a key that does text in ~2s but 503s on every image is being throttled on
multimodal specifically, not deauthorised.

## Deleting one generated asset

`DELETE /api/v1/jobs/{job_id}/assets/{field}` removes a single rendered photo or
video from S3 and clears its column. Nothing else on the job is touched.

**Only six fields are deletable**: `photo_url_1..3` and `video_url_1..3`. The
participant's `selfie_url` and the finished `final_video_url` are refused with a
400 — they are what the entry *is*, they cannot be regenerated, and no admin
workflow needs to prune them. Everything deletable can be rebuilt by re-running
the job. In the panel the same rule is structural rather than a second list: a
tile shows the delete cross only if it is given an `onDelete`, and those two are
not given one.

Three details are deliberate:

- **The client sends the field name, not the URL.** The panel receives CDN URLs
  while the database stores S3 ones, so a URL round-tripped from the browser
  would never match.
- **S3 first, then the DB.** `delete_object` is idempotent, so if the commit
  fails afterwards the whole call can simply be retried. The other order drops
  the URL and leaves an object with nothing pointing at it.
- **A shared object is never deleted from the bucket.** A repeat entry reuses an
  earlier job's rendered assets, so the same URL can appear on more than one
  row; the endpoint checks every asset column of every other job first, and if
  the object is shared it clears this job's reference and keeps the file. The
  response says which happened via `s3_deleted`, and the panel's toast reflects
  it.

The delete is recorded like any other panel edit — `approved_by` / `approved_at`
on the job get the admin's name.

## The name is capped at 8 characters, and the API is what enforces it

The participant's name is burned into the rendered video, so it is limited to
what the template can show. `NAME_MAX_LENGTH` in `app/routers/video.py` is the
single source of that number.

**It truncates, it does not reject.** A long name is not the participant doing
something wrong, and a 400 over it would cost a real signup. `"Aishwarya"` is
stored as `"Aishwary"`; the truncation is logged so it is visible in the entry
log rather than silent. The trailing `.strip()` after the cut matters — slicing
`"Ravi Kumar"` at 8 can leave a space on the end, which the renderer would draw
as a gap.

**The frontend's `maxLength={8}` is a convenience, not the enforcement.** It
stops the typing, but a direct POST to `/video/submit` or an edited attribute
would otherwise put up to `VARCHAR(120)` in front of the renderer. If the two
numbers ever diverge, this one wins and the browser one is merely the nicer
experience — keep them in step.

**Note what a cap this tight excludes.** Aishwarya, Siddharth, Venkatesh,
Rajeshwari, Ramachandran and any two-part name are all cut. That is a deliberate
trade against the template's width, not an oversight; if the template ever gains
room, this constant and the frontend's `maxLength` move together.

**Not covered: the admin panel's name edit.** `PATCH /jobs/{id}/fields` still
accepts any length, so an admin correcting a name can exceed what the renderer
can draw. Decide whether that should share the cap.

## Entry lifecycle
`wait → (queued | process_stop | unverified) → photo_processing → photo_done →
video_processing → video_done → stitching → uploaded → sent` (or `failed` with a
`failed_stage`).

- `repeat` — the same person came back: face + number + name + gender + language
  all match an earlier **`sent`** entry, so its video is copied across instead of
  rendered again. Never auto-sent — delivery is manual from the dashboard. See
  `../embedding-worker/README.md`.
- `client` — from a number on the **client list** (Backend Config in the panel),
  so a client demo is never mixed into the real queue. Decided only once the OTP
  is verified: before that the entry sits in `wait`. `unverified` outranks it —
  if the photo gate was off, nothing checked the photo, and that has to stay
  visible even for the client's own entry.
- `wait` — submitted, OTP not yet verified.
- `unverified` — OTP verified but the photo was never validated (check was off).
- `process_stop` — held for manual review (a number on the `held_numbers` list).

The in-DB watchdog (`sql/schema.sql`) rewinds stuck `*_processing` rows and fails
them past `max_retry`, surviving an all-EC2-down outage. It needs
`event_scheduler = ON` in the RDS parameter group — see the notes at the bottom
of `sql/schema.sql`.

## Timestamps — everything is IST

`app/core/database.py` sets `time_zone = '+05:30'` on **every** MySQL connection.
Without it timestamps split two ways: columns the models fill (`default=get_ist_now`)
got IST, while columns MySQL fills (`DEFAULT CURRENT_TIMESTAMP`, `ON UPDATE
CURRENT_TIMESTAMP`) got UTC — RDS's default — 5h30m apart in the same row.

The in-DB watchdog can't rely on that, because the event scheduler runs in the
**global** zone, not our session's. It derives IST explicitly:

```sql
SET v_now_ist = CONVERT_TZ(UTC_TIMESTAMP(), '+00:00', '+05:30');
```

That bug was worth catching: comparing an IST `locked_at` against a UTC `NOW()`
skews 5h30m in the direction where nothing ever *looks* stuck, so the watchdog
would have silently never recovered a job.

⚠️ **Changing the session zone on a database that already holds rows shifts how
existing `TIMESTAMP` columns read back** (they're stored as epoch and rendered in
the session zone). It was set here while the tables were still empty. If you ever
change it again with live data, shift the existing values in the same migration.

The startup banner prints the resolved zone — `[OK] MySQL (tz +05:30, now …)` —
so drift is visible rather than silent.

## Consent

The T&C checkbox is enforced at submit and now **recorded on the entry**:
`consent_version` (which text was accepted) and `consent_ts` (when), alongside
`ip_address` and the GeoIP-derived `city`. It used to be logged only — fine for
debugging, weak under DPDP, where "show me this person's consent" has to be
answerable from the database rather than from rotated log files.

All four are nullable: entries created before migration 006 have no honest
value, and inventing a consent timestamp would be worse than an empty one.

`city` comes from `app/core/geoip.py` — an offline DB-IP City Lite lookup, no
per-request network call. The ~130 MB `.mmdb` is **not** in git; the API
downloads it in the background on first boot and the banner says whether it's
present. Until it is, `city` is simply NULL — geolocation never blocks a submit.

## MySQL ENUMs and the model's tuples must match exactly

`pipeline_config` (and every other ENUM column) is declared twice: once in MySQL
and once as a Python tuple in the model. **If the DB holds a value the tuple
omits, SQLAlchemy raises `LookupError` while materialising the row**, so the
whole query fails — the endpoint returns 500 rather than that one field
misbehaving.

That is not hypothetical, and it has happened twice — the same missing value,
propagating downstream:

1. `pipeline_config.video_quality` was `enum('480p','720p','1080p')` in MySQL and
   `("720p","1080p")` in the model, and both stored rows were `480p`. **Every**
   pipeline-config read 500'd at once: `/config/list`, `/config/active`,
   `/config/{id}`, `/config/pipeline/paused`, and the create and rollback
   endpoints (which read the previous active config first).
2. The active config being `480p` then meant the worker stamped `jobs.quality =
   '480p'` on the rows it picked up — and `QUALITIES` in the job model omitted it
   too, so **`/jobs/list` started 500ing the moment a job was processed.** Two
   rows were enough to take the whole admin panel's job list down.

Note the shape of the second one: nothing was wrong until a *worker* wrote a
legal DB value that the API's model did not know about. A read-only API can be
broken by a writer it never talks to.

`/config/audit` and `/config/options` kept working throughout, because neither
touches the table — when one endpoint 500s and its neighbours do not, that split
is the giveaway.

Two rules follow:

- **Order matters as well as membership.** MySQL stores an ENUM by ordinal
  position, so the tuple has to list the values in the DB's own order — `480p`
  first, here.
- **Changing an ENUM is a two-file change.** A migration that adds a value and a
  model that does not list it is a 500 the moment a row uses it, and nothing
  fails at deploy time to warn you.

**Audit every model at once** rather than the table you happen to be looking at
— import every module under `app.models`, then for each table in
`Base.metadata` compare `SHOW COLUMNS` against
`Base.metadata.tables[t].columns[c].type.enums`. Both bugs above would have been
caught by one run of that before they reached production, and it takes seconds.
A mismatch on a value any row already holds is not a latent risk; it is an
outage that has not been requested yet.

## Worker (separate, not in this repo)

The renderer should: lock a `queued` entry (`FOR UPDATE SKIP LOCKED`), read the
active `pipeline_config` (snapshotting `config_id` + provider/model/quality onto
the row — `services/config_service.snapshot_config_onto_job` does exactly this),
use `job.stage_key` (`<language>_<gender>`, e.g. `hindi_female`) to look up the
prompt blobs, write asset URLs to `job_assets`, and advance the status.
# goh-backend-s3
