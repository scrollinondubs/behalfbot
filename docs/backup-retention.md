# Backup retention (grandfather-father-son)

The nightly S3 backup (`chassis/scripts/backup-to-s3.sh` →
`chassis/scripts/encrypted-s3-upload.sh`) writes one encrypted object per night
under `nightly/<date>/`. Left alone, that either grows forever or gets a flat
"expire after N days" rule that throws away all long-term history.

This doc describes the optional **grandfather-father-son (GFS)** scheme: keep a
dense recent window plus a sparse long tail, so you can restore last night
instantly and still reach back years for the "corruption we didn't notice for
three weeks" case, without paying to keep every nightly forever.

## The scheme

Two moving parts, and you need **both** or it doesn't work:

1. **Fanout (script side).** With `BACKUP_GFS_RETENTION=true` in `.env`, the
   uploader also uploads each nightly artifact into extra prefixes on calendar
   boundaries. It re-uploads the local encrypted file (already on disk from the
   nightly run) rather than server-side-copying the S3 object, because a
   server-side S3-to-S3 copy needs `s3:GetObject` on the source and the
   backup-writer key is `PutObject`-scoped. Re-uploading needs only
   `PutObject`. Cost is one extra ~250-300 MiB PUT per boundary (three on
   Jan 1) - trivial, and monthly.

2. **Retention (bucket side).** Lifecycle rules on each prefix tier the copies
   into Glacier Deep Archive and expire them on their own schedule. Lifecycle
   rules can only expire or tier objects that already exist - they cannot
   create the monthly/quarterly/yearly copies. That is the fanout's job.

If you enable the fanout but never apply the lifecycle rules, the
monthly/quarterly/yearly copies accumulate forever (slowly - a handful of
objects per year - but unbounded). That is why the fanout is **opt-in**.

## Prefixes and retention

| Prefix | Written | Storage class | Retention | Retrieval |
|--------|---------|---------------|-----------|-----------|
| `nightly/<date>/` | every night | Standard | 30 days | instant |
| `monthly/<YYYY-MM>/` | 1st of each month | Deep Archive (day 1) | 12 months | 12-48h |
| `quarterly/<YYYY-QN>/` | 1st of Jan/Apr/Jul/Oct | Deep Archive (day 1) | 3 years | 12-48h |
| `yearly/<YYYY>/` | Jan 1 | Deep Archive (day 1) | forever | 12-48h |

The last 30 days always sit in instant-retrieval Standard, so the common
"restore from a recent night" path is fast. Anything older is a real DR event,
where a 12-48h Deep Archive retrieval is acceptable, and stored at
~$0.001/GB/mo.

Jan 1 is simultaneously a month, quarter, and year boundary, so that night's
object is copied into all three prefixes. Intended - each prefix is retained
independently.

## Enabling it

### 1. Turn on the fanout

In your install `.env`:

```
BACKUP_GFS_RETENTION=true
```

Takes effect on the next nightly tick. The first `monthly/` copy appears on the
next 1st-of-month; quarterly/yearly on their respective boundaries.

### 2. Apply the lifecycle rules to the bucket

**This needs a credential with `s3:PutLifecycleConfiguration`.** The chassis
backup-writer key (`AWS_BACKUP_WRITE_*`) is scoped to `PutObject` only and
cannot do this - use an admin profile or the AWS Console.

**Option A - helper script** (needs admin AWS creds in the ambient environment
or an `AWS_PROFILE`):

```bash
AWS_PROFILE=<admin-profile> bash chassis/scripts/apply-s3-lifecycle.sh
```

It reads `S3_BACKUP_BUCKET` / `S3_BACKUP_REGION` from `.env`, applies the
four-rule policy below, and reads it back to confirm. The policy is a full
replacement of the bucket's lifecycle config - it includes the `nightly-expire`
rule so the existing 30-day daily window is preserved.

**Option B - AWS Console.** S3 → your bucket → Management → Lifecycle rules.
The three long-tail rules are on distinct prefixes (`monthly/`, `quarterly/`,
`yearly/`), so they do not overlap `nightly-expire` and produce no warnings.
For each: filter on the prefix, add a transition to Glacier Deep Archive after
1 day, and set expiration (365 / 1095 days; none for `yearly/`).

## The lifecycle policy

`chassis/scripts/apply-s3-lifecycle.sh` applies exactly this (also usable
directly with `aws s3api put-bucket-lifecycle-configuration`):

```json
{
  "Rules": [
    {
      "ID": "nightly-expire",
      "Status": "Enabled",
      "Filter": { "Prefix": "nightly/" },
      "Expiration": { "Days": 30 },
      "NoncurrentVersionExpiration": { "NoncurrentDays": 30 }
    },
    {
      "ID": "monthly-12mo-deep-archive",
      "Status": "Enabled",
      "Filter": { "Prefix": "monthly/" },
      "Transitions": [ { "Days": 1, "StorageClass": "DEEP_ARCHIVE" } ],
      "Expiration": { "Days": 365 }
    },
    {
      "ID": "quarterly-3yr-deep-archive",
      "Status": "Enabled",
      "Filter": { "Prefix": "quarterly/" },
      "Transitions": [ { "Days": 1, "StorageClass": "DEEP_ARCHIVE" } ],
      "Expiration": { "Days": 1095 }
    },
    {
      "ID": "yearly-forever-deep-archive",
      "Status": "Enabled",
      "Filter": { "Prefix": "yearly/" },
      "Transitions": [ { "Days": 1, "StorageClass": "DEEP_ARCHIVE" } ]
    }
  ]
}
```

All retentions exceed Deep Archive's 180-day minimum-storage-duration charge,
so nothing gets hit with an early-deletion penalty.

## Restoring from a long-tail snapshot

`chassis/scripts/restore-from-s3.sh` lists and pulls from `nightly/` by default.
To restore from a monthly/quarterly/yearly snapshot, retrieve the specific
object first (Deep Archive needs a restore request + wait), then decrypt it the
same way:

```bash
# 1. Kick off a Deep Archive restore (Standard tier ~12h; Bulk ~48h, cheaper)
aws s3api restore-object --bucket "$S3_BACKUP_BUCKET" \
  --key "monthly/2026-01/<name>-<ts>.tar.gz.age" \
  --restore-request Days=3,GlacierJobParameters={Tier=Standard}

# 2. Once restored, download + decrypt (age key from your secret store)
aws s3 cp "s3://$S3_BACKUP_BUCKET/monthly/2026-01/<name>-<ts>.tar.gz.age" ./restore.tar.gz.age
age -d -i ~/.jax-backup-age.key ./restore.tar.gz.age | tar xzf - -C ./restore/
```

Each long-tail snapshot ships its own `manifest.json` (copied alongside the
tarball) with the encrypted `sha256` for integrity verification.

## Cost

At the reduced per-backup footprint (~250-300 MiB after the `data/` exclusions
in `backup-to-s3.sh`), steady state is roughly 30 daily (Standard) + 12 monthly
+ 12 quarterly + N yearly (nearly all Deep Archive). That is a few dollars a
year in storage - dominated by the 30 Standard nightlies, with the entire
archival tail costing pennies.
