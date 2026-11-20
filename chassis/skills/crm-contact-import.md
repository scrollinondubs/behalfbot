# CRM contact import

Use when the CRM is being set up on an install (the dashboard's CRM tab works:
DSN set, migration applied, see `chassis/docs/dashboard.md`), or whenever the
person asks to bring their contacts in from somewhere else. Offer it once, as
an option. The CRM works fine empty and fills up as they use it.

Do not use it for: anything that needs a login the person has not given you,
reading message history, or pulling contacts out of a site by scrolling or
scraping. Only the platform's own export feature, run from the person's
own account.

## The offer

Say it in your own words, keeping these points. A version to start from:

> Your CRM is set up. If you want, I can fill it with people you already
> know from LinkedIn, Google or iCloud contacts, Outlook, Facebook or
> Instagram. Each one has its own export. I'll open the right page in your
> browser and set it up, and you press the last request button yourself.
> Then send me the contacts file and I'll show you what I'd add before I
> write anything.
>
> What I read: names, plus emails, phone numbers, profile links, company and
> job title where the export has them. LinkedIn, Facebook and Instagram send
> a zip that also holds your messages. Unzip it on your computer and send me
> just the contacts file. I don't open the zip or anything else in it.
>
> You can skip this. If you want to do it, which one first?

If they say no or not now, drop it. Don't bring it up again unless they ask.

## Per platform

Runbooks run through Autopilot: mint a code for the runbook id, the person
pastes it in the extension, you drive the `do` steps and they press every
`human` step. The final export or request button is always theirs.

| Platform | How | File they send you | What it gives |
|---|---|---|---|
| LinkedIn | Runbook `linkedin-connections-export` | `Connections.csv` from inside the archive | Name, profile URL, company, position. Email only for connections who allow it |
| Google Contacts | Runbook `google-contacts-export` | `contacts.vcf` or `contacts.csv` | Name, emails, phones, company, title |
| iCloud / iPhone / WhatsApp | Runbook `icloud-contacts-export` | one `.vcf` | Name, emails, phones, company, title |
| Mac Contacts | Manual, below | one `.vcf` | Same as iCloud |
| iPhone without iCloud | Manual, below | one `.vcf` | Same as iCloud |
| Outlook.com / Outlook on the web | Runbook `outlook-contacts-export` | `contacts.csv` | Name, emails, phones, company, job title |
| Outlook desktop (Windows) | Manual, below | `.csv` | Same |
| Facebook | Manual, below | `your_friends.json` | Names only |
| Instagram | Manual, below | `followers_1.json`, `following.json` | Handles only |

### LinkedIn

The "select particular files" list no longer includes Connections (checked
2026-11-20: it offers Articles, Invitations, Profile, Recommendations and
Registration). Connections come only with **Download larger data archive**,
which LinkedIn emails within about 24 hours. The runbook picks that option and
the person presses Request archive. By hand: Me > Settings & Privacy > Data
privacy > Get a copy of your data > Download larger data archive > Request
archive.

When the email comes, the zip holds messages too. Ask for `Connections.csv`
alone. If they send the zip, the importer refuses it; ask again for the one
file and don't unzip it yourself. The CSV starts with a few "Notes:" lines
before the header; the importer skips them.

### Google Contacts

The runbook uses contacts.google.com: Selection actions > All, More actions >
Export, vCard or Google CSV. Google Takeout (takeout.google.com, deselect all,
tick Contacts only) gives the same vCard if the person prefers it.

### Apple: iCloud, Mac, iPhone, and WhatsApp

WhatsApp has no contact export of its own. Its contacts are the phone's
address book, so bring them in through the phone's contacts:

- **iCloud** (contacts synced to iCloud): runbook `icloud-contacts-export`.
  icloud.com/contacts > select menu > Select All Contacts > Actions > Export vCard.
- **Mac**: Contacts app, All Contacts, Cmd-A, File > Export > Export vCard.
- **iPhone**: Contacts app > Lists, touch and hold All Contacts (or All
  iPhone) > Export, pick the fields, Done, then share the file to the
  person's computer or straight to you.
- **Android**: the phone's contacts are usually Google contacts; use the
  Google path.

Never use WhatsApp's chat export or "Request account info" for this. Those
are about conversations and the account, not an address book.

### Outlook

Web (Outlook.com, Hotmail, Microsoft 365 in the browser): runbook
`outlook-contacts-export`, People > Manage contacts > Export contacts > All
contacts > Export. A work account starts at outlook.office.com/people.

Desktop Outlook for Windows: File > Open & Export > Import/Export > Export to
a file > Comma Separated Values > the Contacts folder > Finish.

### Facebook and Instagram

No runbook: Meta keeps renaming this flow (Accounts Center, now Meta Account),
so the person follows it with you on a call or chat:

1. Facebook or Instagram > Settings > Accounts Center (Meta Account) > Your
   information and permissions > Export your information (formerly Download
   your information) > Create export.
2. Pick the profile, then Export to device.
3. Customise information: clear everything, then tick only **Friends and
   followers** (Facebook) or **Followers and following** (Instagram).
4. Date range All time, format **JSON**. Start export.
5. When it is ready (Available downloads, kept for 4 days), download and
   unzip it, and send only:
   - Facebook: `connections/friends/your_friends.json`
   - Instagram: `connections/followers_and_following/followers_1.json` (and
     `followers_2.json` and so on if there are more) and `following.json`

The menu path above matches Facebook's help page as of 2026-11-20. The
category names and the file paths inside the zip are from past exports and
may have moved; if the file isn't there, ask them to search the unzipped
folder for `friends` or `followers` and send the JSON they find. The
importer accepts both the older Instagram shape (handle in `value`) and the
newer one (handle in `title`).

Tell them up front what they get: Facebook friends come as names only;
Instagram as handles only (the contact is named `@handle` with the profile
link). Neither has emails or phone numbers.

## Importing a file

```bash
python3 chassis/scripts/crm_import.py ~/Downloads/Connections.csv            # dry run
python3 chassis/scripts/crm_import.py ~/Downloads/Connections.csv --apply    # write
```

It detects the format; `--format linkedin|google|outlook|vcard|facebook|instagram`
overrides. It uses the same DSN variables as the CRM tab.

1. Always dry run first and tell the person the counts: new, enriched,
   unchanged, skipped. Apply only after they say yes.
2. Duplicates are matched on email, then a social profile URL (LinkedIn,
   Instagram and so on), then phone when the first names also agree, then the
   name for rows that have none of those. A company website is noted, never
   matched on. A row that has one of those also matches a contact entered
   with only a name, when exactly one has that full name (two or more:
   added as new, reported as ambiguous). A matched contact is enriched, never
   overwritten: an empty email or phone is filled, and notes lines it lacks
   (source, profile, role, connected date, and `also: <email or phone>
   (<source>)` when the export has a different one) are appended.
3. New contacts get notes like `source: linkedin (imported 2026-11-20)`,
   `profile: https://linkedin.com/in/...` and `role: Founder at Acme`.
   Re-running the same file, on any day, changes nothing.
4. Once it is imported, offer to delete the copy they sent you (to the
   trash, not `rm`). The file holds other people's details.

Exit code 2 means the file was not a supported export; 3 means the CRM is not
reachable (the message says what to set).
