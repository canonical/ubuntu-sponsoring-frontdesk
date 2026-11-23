# Checks reference

What Frontdesk looks for, what it will say to you, and what it does to your
bug or merge proposal. This is the contributor-facing list: if the bot
commented on something you filed, the wording it used is quoted below, and
you can see exactly which check produced it.

It is also where the wording itself gets reviewed, so if a comment reads
badly, this page is the place to propose better words -- the text here is
the text that gets posted.

`doc/GLOSSARY.md` defines the vocabulary and `doc/design_journal.md` carries
the rationale behind each check (the design entry numbers below).

## What happens to your submission

```mermaid
flowchart TD
    start["You attach a debdiff to a bug, or propose a merge,<br/>and subscribe ~ubuntu-sponsors"]
    something{"Is there still<br/>something to review?"}
    closed["The bot comments and unsubscribes.<br/>Already uploaded, already merged,<br/>or nothing attached yet"]
    review["Automated review runs<br/>- the checks below"]
    found{"What did it find?"}
    blockers["ONE comment: 'needs fixing'<br/>Merge proposal: voted Needs Fixing<br/>Bug: set to Incomplete"]
    back["You address the points.<br/>On a bug, set it back to New<br/>to re-enter the queue"]
    stale["No reply for 30 days:<br/>unsubscribed from the queue"]
    suggest["ONE comment: 'please verify'<br/>and 'nice to have'.<br/>Nothing is blocked, no status change"]
    human["Nothing posted.<br/>Waiting for a human sponsor"]
    retry["Nothing posted.<br/>Something could not be read,<br/>so it is retried next pass"]

    start --> something
    something -->|no| closed
    something -->|yes| review
    review --> found
    found -->|"blockers"| blockers
    found -->|"suggestions only"| suggest
    found -->|"nothing to say"| human
    found -->|"could not tell"| retry
    blockers --> back
    back --> review
    blockers -.->|"no response"| stale
    suggest --> human

    classDef ok fill:#C8E6C9,stroke:#333;
    classDef warn fill:#FFE0B2,stroke:#333;
    classDef quiet fill:#ECEFF1,stroke:#333;
    class closed,human ok;
    class blockers,back warn;
    class retry,stale,suggest quiet;
```

Three things that surprise people:

- **A bot comment is not a rejection.** Even a blocking one is a request for
  a change, and the item comes straight back into the queue once addressed.
- **An Incomplete bug needs setting back to New.** That is what re-enters it
  into the sponsoring queue; the bot says so in its comment.
- **Silence usually means it is queued for a human.** The bot only comments
  when it has something to say, and it deliberately stays quiet once a human
  reviewer is involved.

This is the contributor's view. For the bot's own control flow -- the order
checks run in, what short-circuits what -- see `doc/flow.dot` and the
rendered `doc/flow.svg`.

## How a pass ends

Every check returns one of three tiers, and the tier decides the action:

- **closing** -- the item is already resolved. The check posts its own
  comment, unsubscribes `~ubuntu-sponsors`, and the pass ends there.
  Anything found earlier in the pass is deliberately discarded: telling you
  about changelog style on a request that's already uploaded is noise.
- **incomplete** -- a blocker. All blockers found in one pass are merged
  into a *single* comment, under a "Needs fixing before this can be
  sponsored" heading. On a merge proposal that comment carries a
  `Needs Fixing` vote; on a bug the matching tasks are set **Incomplete**
  (only the tasks the reviewed change actually targets, not sibling series)
  and `~ubuntu-sponsors` stays subscribed. Set the bug back to New once
  you've addressed the points and it re-enters the queue.
- **question** -- non-blocking. Appears in the same comment under either
  "Please verify" (`kind="verify"`: if real it would need fixing, but the
  check isn't confident enough to block) or "Nice to have"
  (`kind="advisory"`). No vote, no status change.

So a contributor sees at most one comment per pass. The intro, the section
headings and the closing line come from `render_findings_comment`
(`checks.py:57`); the quoted wording below is the bullet each check
contributes.

Two cases close without a status write: Check 4 and the "already uploaded"
branch of Check 6 comment and leave the MP open, because git-ubuntu merge
proposals reject status writes from the bot (a human closes them out).

### A bug with debdiffs for several series

An SRU often attaches one debdiff per series. The checks that read a
debdiff (5, 6 and 8-16) then run once per series, on the newest debdiff
targeting it, and the comment is grouped by series (design #154): findings
about the bug as a whole first, then one block per series that has
something to report, oldest first, each under its own heading:

> === {label} ===

A series with nothing to report gets no block (#156). A series whose debdiff is already in the archive drops out of the review:

> Already uploaded as {upload}, nothing left to do for this series.

> Already uploaded as {upload} and waiting in the upload queue, nothing
> left to do for this series.

A finding identical to one already shown under an earlier series (same
check, tier and wording -- a check that doesn't look at series-specific
content, like Check 12, can fire the same way on several) is shortened to a
reference instead of repeated (#164):

> Same as {series}.

When every series' debdiff is already uploaded, the bug is closed with
Check 6's "already uploaded" comment, naming each upload. Only the tasks of
series with a blocker are set Incomplete; a blocker about the bug as a whole
(the SRU template, say) blocks every series still under review. A bug whose
debdiffs all target one series gets the plain comment, without headings.

## The checks

| # | Check | Scope | What it detects | What the bot does |
|---|---|---|---|---|
| 1 | [Administrative state](#check-1-administrative-state) | MP + bug | Every relevant Ubuntu task is already closed and something landed, or the MP is Merged. A still-live linked MP blocks closing. | closing: comments and unsubscribes. A fully Fix Released item closes **silently**. |
| 1b | [Nothing to sponsor](#check-1b-nothing-to-sponsor) | bug | No patch and no linked MP; or every open series is already covered by a linked MP's review; or a needs-packaging bug is already published or queued. | closing: comments (one of three wordings) and unsubscribes. |
| 2 | [Target branch](#check-2-target-branch) | MP | A merge MP targeting `ubuntu/devel` instead of `debian/*`; an SRU-shaped MP targeting the wrong series branch; a release-pocket branch that is behind what's published. | incomplete, **except** advisory when a release-pocket branch isn't actually behind. Firing here suppresses Check 3. |
| 3 | [Merge conflicts](#check-3-merge-conflicts) | MP | The branch cannot be merged cleanly into its target. | incomplete. Skipped entirely when Check 2 fired, since a wrong target branch is usually the cause. |
| 4 | [Empty diff](#check-4-empty-diff) | MP | The preview diff is empty: the change already landed in the target. | closing: comments, no status write. |
| 5 | [Changelog bug reference](#check-5-changelog-bug-reference) | MP + bug | The changelog cites `LP: #N` for a bug not reported against this source package. | incomplete; question/verify when the bug cited is the one the debdiff is attached to. |
| 6 | [Version vs. the archive](#check-6-version-vs-the-archive) | MP + bug | The proposed version against everything published for the target series: already uploaded, same version with different content, older than the archive, or waiting in the upload queue. | Varies: closing when already uploaded; incomplete for a collision or a stale version; or defers the item silently (waiting for the archive importer, or an MP waiting in the upload queue). A bug in the upload queue is commented on and unsubscribed (#187). The only check that notifies the operator. A bounce here skips Check 13. |
| 7 | [Newer series first](#check-7-newer-series-first) | MP + bug | An SRU with no evidence the fix landed in the development release or newer supported series. Evidence can be a task status, a linked MP, a patch attachment, or the bug text. | question / verify. Runs late in the pass because it can spend an LLM call. |
| 8 | [Direct source edit](#check-8-direct-source-edit) | MP + bug | Files outside `debian/` edited directly instead of via `debian/patches`. Merges, new upstream versions and native packages are exempt. | incomplete. |
| 9 | [Missing changelog entry](#check-9-missing-changelog-entry) | MP | The diff adds no `debian/changelog` entry. | incomplete. |
| 10 | [Plain patch, not a debdiff](#check-10-plain-patch-not-a-debdiff) | bug | The attached patch touches no `debian/` file, so it isn't sponsorable as-is. | incomplete. |
| 11 | [PPA version suffix](#check-11-ppa-version-suffix) | MP + bug | The proposed version carries a `~ppaN` suffix. | incomplete. |
| 12 | [XSBC-Original-Maintainer](#check-12-xsbc-original-maintainer) | MP + bug | A package's first Ubuntu delta whose `debian/control` doesn't preserve the Debian maintainer. | question / advisory. |
| 13 | [SRU version convention](#check-13-sru-version-convention) | MP + bug | The SRU version string doesn't follow the recommended convention. Delegates to `ubuntu-lint`; silently skipped when `python3-ubuntu-lint` isn't installed. | question / advisory. Skipped when Check 6 already bounced the version. |
| 14 | [SRU version precedence](#check-14-sru-version-precedence) | MP + bug | A newer series publishing a *lower* version (so the fix hasn't landed there yet), or a proposed version already used somewhere in archive history. | incomplete. |
| 15 | [No-change rebuild version](#check-15-no-change-rebuild-version) | MP + bug | A no-change rebuild (only `debian/changelog`, plus `update-maintainer`'s `debian/control` lines) using an `ubuntuN` instead of a `buildN` revision. | incomplete. |
| 16 | [DEP-3 patch header](#check-16-dep-3-patch-header) | MP + bug | A newly added `debian/patches/` patch with no DEP-3 header (no `Description:`/`Subject:`). Merges skipped; modified patches not judged. | incomplete. |
| 17 | [Released changelog entry edited](#check-17-released-changelog-entry-edited) | MP + bug | The diff adds a new changelog entry but also changes an older, already-uploaded one -- whitespace included. Merges and `UNRELEASED` entries skipped. | incomplete. |
| 18 | [Patch pasted in a comment](#check-18-patch-pasted-in-a-comment) | bug | No patch attached and no merge proposal, but the description or a comment carries a pasted unified diff. Skipped for sync requests and when a human reviewer is engaged. | incomplete. |
| 19 | [Development version for a released series](#check-19-development-version-for-a-released-series) | MP + bug | The new changelog entry targets a released series but the version is a plain `ubuntuN` -- numbered like a development-release upload. Checks 13/14 skipped for that target, Check 7 when every target fires. | question / advisory (#189). |

## What the bot posts

Placeholders in braces are filled in from the item being reviewed.

### Check 1: administrative state

`check_administrative_state`, design #1/#112/#126. Only the Fix Committed
branch comments; an item where every task is Fix Released is closed without
a word.

> This request has been uploaded and is awaiting release, so there is
> nothing left for a sponsor to do here. Cleaning up the queue by
> unsubscribing ~ubuntu-sponsors.

### Check 1b: nothing to sponsor

`check_nothing_to_sponsor`, design #68/#75. Three wordings. When the work is
being reviewed on linked merge proposals:

> The fix proposed here is being reviewed on the merge proposal{plural}
> linked to this bug, so there is no need for a separate sponsoring-queue
> entry for the bug itself. Cleaning up the queue by unsubscribing
> ~ubuntu-sponsors; the review continues on the merge proposal{plural}.

When there's nothing attached at all:

> There doesn't seem to be a patch or merge proposal attached to this bug
> yet, so there is nothing for the sponsors team to review at this point.
> Cleaning up the queue by unsubscribing ~ubuntu-sponsors -- please
> subscribe them again once a proposed fix is available.

Not when a comment (or the description) carries a pasted patch: that's
Check 18's case (#179).

When a needs-packaging bug is already published or sitting in the queue:

> {note}, so there is nothing left for a sponsor to do here. Cleaning up
> the queue by unsubscribing ~ubuntu-sponsors; should the upload be
> rejected, please subscribe ~ubuntu-sponsors again to get back in the
> review queue.

### Check 2: target branch

`check_target_branch`, design #4/#128/#196. A merge MP pointed at the wrong
branch:

> This Merge Proposal is a merge (rebase onto a newer Debian revision) but
> targets `ubuntu/devel`. According to our workflow, merge MPs should target
> {target_phrase} instead (workaround for LP: #1976112). Please update the
> target branch.

An SRU-shaped MP whose target branch is for another series than its
changelog:

> This Merge Proposal's changelog entry targets `{suite}`, but the MP itself
> targets `{target_branch_name}` instead of `{expected}`. Please update the
> target branch to match the series you're fixing -- this is very likely
> also the cause of any reported merge conflicts, since the branch is being
> compared against the wrong history.

A pocket branch of the right series -- `ubuntu/{suite}`, or its
`-updates`, `-proposed` or `-security` branch (#196; security uploads too:
only the changelog picks the landing pocket) -- that is genuinely behind
(blocking); when it isn't behind, the bot recommends `-devel` as a
non-blocking advisory instead:

> The branch this Merge Proposal targets (`{target_branch_name}`) is behind:
> it carries `{release}` while `{newest}` is already published, so this
> change would be based on outdated content. Please target `{expected}`
> instead -- it includes any SRUs already in `-updates` (and anything staged
> in `-proposed`) -- and rebase on it.

### Check 3: merge conflicts

`check_mp_conflicts`, design #5.

> This Merge Proposal has merge conflicts and cannot be cleanly merged.
> Please rebase your branch, resolve the conflicts, and push the updated
> branch.

### Check 4: empty diff

`check_empty_diff`, design #25.

> Thanks for your contribution! The proposed change seems to have landed in
> the target Vcs, so the merge request can be closed.

### Check 5: changelog bug reference

`check_changelog_bug_reference`, design #40.

> The bug reference(s) in {where} ({bug_list}) don't appear to be reported
> against `{package}`. Please double-check the bug number(s) are correct.

When the cited bug is the one the debdiff is attached to, the number can't
be a typo: the bug's task is on another package name, or the debdiff is for
the wrong package. Asked, not blocked (design #149, bug #2009138):

> {filed}, but the attached debdiff is for `{package}`, so uploading it
> won't close the bug automatically. If the debdiff is for the right
> package, the task needs moving to `{package}` (a sponsor can do this when
> uploading).

`{filed}` is "This bug's Ubuntu task is filed against `{name}`", or "This
bug has no Ubuntu task".

### Check 6: version vs. the archive

`check_stale_version`, design #12/#122. Already uploaded, so the MP can be
closed:

> Thanks for your contribution! This change was already uploaded to the
> archive as `{package} {version}` ({url}), so this merge proposal can be
> closed.

The same situation on a bug:

> Thanks for your contribution! It seems that this change was already
> uploaded to the archive as {upload}, so there is nothing left
> to sponsor here. Cleaning up the queue by unsubscribing ~ubuntu-sponsors.

`{upload}` is `` `{package} {version}` ``. When every series' debdiff of a
multi-series bug is already uploaded (#154):

> Thanks for your contribution! It seems that these changes were already
> uploaded to the archive as {uploads}, so there is nothing left to sponsor
> here. Cleaning up the queue by unsubscribing ~ubuntu-sponsors.

Proposed version older than what's published:

> The proposed version (`{proposed_version}`) is older than the one already
> in the archive (`{archive_version}` in {target_series}). Please rebase on
> top of the current archive version.

"Same content" tolerates a sponsor's edit before upload (#191): the entries
match apart from whitespace and the ` -- ` trailer; or the upload was created
by the MP's author / the debdiff's uploader; or the header and `LP: #`
references match, the line count is the same and every line is nearly
identical. Anything else is a collision.

Same version, different content -- in the archive:

> An upload with the same version (`{version}`) but different content
> already exists in the archive. Your change needs to be rebased (with a new
> version number) and resubmitted.

...or still in the upload queue:

> An upload with the same version (`{proposed_version}`) but different
> content is already waiting in the {target_series} upload queue. Your
> change needs to be rebased (with a new version number) and resubmitted.

On a bug, the same upload already waiting in the upload queue (for a
multi-series bug: every series is published or queued) is commented on and
~ubuntu-sponsors unsubscribed (#187); a queue rejection is the uploader's to
follow up. "SRU team review" becomes "archive review" when a development
release upload is among them. MPs still defer silently.

> {verb} already waiting in the {where} {queue} for {reviewer}
> ({listed}), so there's nothing left to sponsor; unsubscribing
> ~ubuntu-sponsors.

e.g. "This upload is already waiting in the resolute upload queue for SRU team
review (`xwayland 2:24.1.10-1ubuntu0.1`), ..."; with several, "These uploads
are ... in the resolute and stonking upload queues ...".

### Check 7: newer series first

`check_sru_newer_series`, design #58/#115/#118/#134/#180/#183. On a bug,
the SRU target is the oldest stable series among the attached debdiffs; only
when no debdiff names a supported series do the bug's tasks decide (#183). A
bug whose debdiffs all target the development release isn't an SRU yet, even
with stable-series tasks open for a planned one. When the bug's
tasks, MPs and attachments don't show a newer series handled, the LLM reads
the bug text and answers per series: fixed there, explicitly declined with a
reason, or neither. A fixed series isn't mentioned. For the others:

> SRU policy requires the fix to land in newer supported series first, and
> there is no evidence of it being resolved in {series_words}. Please check:
> if it isn't fixed there yet, {those series need / that series needs} to be
> updated before this SRU can be sponsored; if it is, please reflect that
> explicitly in the bug description or the bug status.

For a series the description explicitly skips, giving a reason (#180):

> The description explains why {series_words} won't get this fix. SRU
> policy normally requires newer supported releases to be fixed first, so
> the SRU team will need to agree to skipping {it / them}.

### Check 8: direct source edit

`check_direct_source_edit`, design #60/#62. The opening words vary ("The
changes edit" for an MP, "The attached debdiff edits" for a bug).

> {intro} upstream source files directly ({shown}{more}). Changes to
> upstream code must be provided as patches under `debian/patches` instead,
> so they stay visible and survive new upstream versions

### Check 9: missing changelog entry

`check_missing_changelog_stanza`, design #61.

> The merge proposal doesn't add a `debian/changelog` entry describing the
> changes. Please add a new changelog entry with an incremented version
> number{lp_clause}

### Check 10: plain patch, not a debdiff

`check_patch_not_debdiff`, design #63.

> The attachment is a plain code patch. Thank you for working on a fix! To
> be ready for sponsoring it needs to be turned into a source package update
> (debdiff): include the patch under `debian/patches` and add a new
> `debian/changelog` entry with an incremented version number describing the
> change

### Check 11: PPA version suffix

`check_ppa_version_suffix`, design #90.

> The proposed version (`{proposed_version}`) has a `~ppaN` suffix, which
> belongs to a PPA build, not an archive upload. Please drop it and use a
> normal archive version string

### Check 12: XSBC-Original-Maintainer

`check_xsbc_original_maintainer`, design #93.

> This looks like the package's first Ubuntu delta, but `debian/control`
> doesn't add an `XSBC-Original-Maintainer` field preserving the Debian
> maintainer

...followed by: "Please add it if you can; otherwise a sponsor can do it
before upload."

### Check 13: SRU version convention

`check_sru_version_suffix_convention`, design #109/#119/#122/#129/#173/#195. The
reason comes from `ubuntu-lint` (>= 0.3; #195 removed #129's skip of
dotted Debian revisions, fixed there).

> The proposed version doesn't follow Ubuntu's recommended SRU
> version-string convention: {reason}. This isn't necessarily wrong (what
> actually matters is that the version sorts ahead of the archive and
> doesn't collide with anything), but following the convention avoids
> surprises

Not raised on a multi-release SRU (#173) that uses the per-release
`ubuntu0.YY.MM.N` form for every series when `ubuntu-lint` itself expects
that form for at least one of them -- its base version is in several
releases. The documented rule requires it only there, but one consistent
scheme across the update is harmless.

### Check 14: SRU version precedence

`check_sru_version_newer_series_precedence`, design #110/#116/#117/#124/#175.
Up to two parts, each with its own bullet list of findings:

> The fix doesn't appear to have landed in a newer series yet, which SRU
> policy requires before it can land here:

A newer series doesn't count as "not landed" while a fix for it is in
flight: a linked merge proposal (#124), or its own debdiff on the same bug
with a version sorting at or above the proposed one (#175).

> The proposed version can't be used:
> {bullets}
> Please pick a different version.

Besides a version already published in another series, a bullet there
says when the same bug proposes the identical version for another series
too (#175) -- only one of them could ever be uploaded:

> is also proposed for `{other}` on this bug -- a version can only be
> uploaded once across all series, so each series' upload needs its own

### Check 15: no-change rebuild version

`check_no_change_rebuild_version`, design #132.

> This is a no-change rebuild, so the version should be `{expected}`, not
> `{proposed_version}`: an `ubuntuN` revision declares an Ubuntu delta that
> later merges would try to preserve.

### Check 16: DEP-3 patch header

`check_dep3_patch_header`, design #151.

> The new {noun} {names} {verb} no DEP-3 header. Please add one describing
> the change: at least `Description:`, plus `Origin:` (or `Author:`),
> `Bug-Ubuntu:` and `Forwarded:` where they apply. See {url}

`{noun}`/`{verb}` read "patch ... has" or "patches ... have"; `{url}` is
https://ubuntu.com/project/docs/how-ubuntu-is-made/concepts/patches/

### Check 17: released changelog entry edited

`check_released_changelog_edit`, design #155.

> The diff also changes {what} that {verb} already uploaded. Released
> entries are a record of past uploads and shouldn't be edited: please limit
> your changelog changes to the new entry at the top.

`{what}` is "an existing `debian/changelog` entry (`{pkg} {version}`)" or
"existing `debian/changelog` entries (...)", naming each entry whose header
is visible in the diff; `{verb}` is "was"/"were".

### Check 18: patch pasted in a comment

`check_patch_in_comment`, design #179. Runs right after Check 1b, which no
longer closes such a bug as "nothing to sponsor".

> The proposed fix appears to be pasted into a comment. Thank you for
> working on it! To be ready for sponsoring it needs to be attached as a
> source package update (debdiff) -- include the patch under
> `debian/patches` and add a new `debian/changelog` entry with an
> incremented version number describing the change -- or proposed as a
> merge proposal. See
> https://ubuntu.com/project/docs/contributors/updating/work-with-debian-patches/

### Check 19: development version for a released series

`check_devel_version_on_released_series`, design #182/#189. Every SRU version
has a dotted Ubuntu part (the version-strings table), so a plain `ubuntuN` for
a released series is either a development upload with the wrong series or an
SRU without an SRU version. Advisory (#189): the sponsor can fix either before
uploading. Runs before Check 13; when it fires, Checks 13 and 14 are skipped
for that target (they would only restate it from the SRU angle), and Check 7
when it fires on every target. The example is the version ubuntu-lint expects
(it knows whether the base is shared across releases); left out when
ubuntu-lint gives none.

> The changelog entry targets `{series}`, a released series, but
> `{proposed_version}` is numbered like a development-release upload. If it's a
> stable release update, the version needs the SRU form{example}; if it's
> meant for the development release, the changelog should target `{devel}`
> instead (`dch` reuses the previous entry's series). The sponsor can adjust
> this before uploading, but please keep it in mind for next time. See
> https://ubuntu.com/project/docs/how-ubuntu-is-made/concepts/version-strings/

`{example}` is " (e.g. `<version>`)".

## LLM-assisted reviews

These live in `llm_reviewer.py` and ask a model to judge free text a regex
can't: whether a description is actually meaningful, not whether it matches
a pattern. They run after the deterministic checks and only when the item
looks relevant. If the model is unavailable or its answer can't be parsed,
they fail safe: nothing is posted and the item is left for a human.

The prompts embed contributor-written text, so they run under a tool-less
opencode agent and a reply containing any tool use is discarded outright
(design #99).

| Review | Scope | What it judges | What the bot does |
|---|---|---|---|
| [SRU template](#sru-template) | bug + MP | Whether an SRU bug description really follows the SRU template (not just that headings exist). | incomplete when a section is missing (on an MP it short-circuits the rest of the MP review); question / advisory when all are there but some are thin (#197). |
| [Changelog stanza quality](#changelog-stanza-quality) | MP | Whether the changelog entry describes the change usefully. | question / advisory. |
| [Changelog vs. diff](#changelog-vs-diff) | MP | Whether the changelog matches what the diff actually does. | question / verify. |
| [Feature Freeze](#feature-freeze) | MP | Whether the change adds a feature while Feature Freeze is in effect. Silent when an FFe bug is already linked. | question / verify. |
| [Sync requests](#sync-requests) | bug | Whether a sync request is already satisfied, needs more justification, or should go to a human. | closing (Fix Released), incomplete, or hand over to a human. |
| Fixed in a newer series | bug | Whether the bug text shows the fix already landed in a newer series. | No comment of its own: it suppresses Check 7. |
| Already covered by a reviewer | MP + bug | Whether a human reviewer's comments already make the same points. | No comment of its own: it drops findings that would repeat the reviewer. |

### SRU template

A section absent (or the template not used) is blocking -- incomplete, and
on an MP it short-circuits the rest of the MP review:

> This looks like an SRU, but the bug description doesn't follow the
> official SRU bug template ({SRU_TEMPLATE_LINK}):

followed by one `- [Section] <what's wrong>.` line per section, e.g.
`- [Test Plan] is missing.`

Every section there but some too thin is advice ("nice to have"; no
Incomplete, no vote, the MP review goes on; #197):

> This looks like an SRU, and the bug description follows the SRU bug
> template ({SRU_TEMPLATE_LINK}). Some sections could be more detailed, which
> will make the SRU team's review easier (see {SRU_REQUIREMENTS_LINK}):

followed by one paragraph per thin section, separated by blank lines: the
section's fixed guidance (from the SRU requirements page), then "Here it
<what's lacking>." written by the model. The guidance:

- `[Impact]` "should explain the real-world impact to users, in a way a
  technical reader unfamiliar with Ubuntu development can follow."
- `[Test Plan]` "should be precise enough that someone else following it
  would run exactly the same steps."
- `[Where problems could occur]` "should analyse what could break for
  existing users with this change, and how that risk is kept low."

The model says which case applies (`problem: missing|thin`); anything else
reads as `missing`. When deciding whether the bot already posted the same
comment, the model's part of each point is ignored (#185), so a re-triage
that words the same points differently doesn't post again.

With several linked bugs, the lead-in covers both cases, and each failing
bug gets its own sub-list labelled with its case (#186); the bullet is
blocking if any bug is missing a section, advice otherwise (#197):

> This looks like an SRU with multiple linked bugs, but some of their
> descriptions don't follow the SRU bug template ({SRU_TEMPLATE_LINK}) or need
> more detail:

```
Bug #N, doesn't follow the template:
  - [Test Plan] is missing.
Bug #M, uses the template but needs more detail:
  - [Impact] should explain ... Here it ...
```

### Changelog stanza quality

Whether the changelog entry actually tells a reader what changed. The
wording is written by the model rather than templated here, and is posted as
a "nice to have" bullet in the aggregated comment.

### Changelog vs. diff

Where the changelog claims something the diff doesn't do, or the diff does
something the changelog doesn't mention. Also model-written, posted as a
"please verify" bullet, since a mismatch is a judgement call.

### Feature Freeze

> This change appears to introduce a new feature, and Feature Freeze is in
> effect -- please confirm there's an approved Feature Freeze Exception

### Sync requests

Already in Ubuntu, so the request is closed as Fix Released:

> Thanks for your contribution! It looks like {pkg} {req_version} (or
> newer) is already published in Ubuntu. Closing this sync request as Fix
> Released.

Needs more from the requester:

> This sync request needs a bit more work before it can be sponsored:

## Why the bot sometimes says nothing

Silence is usually deliberate. In rough order of when it happens:

- **`~ubuntu-sponsors` isn't subscribed.** The team subscription is what
  puts an item in the queue; without it the bot doesn't act.
- **The item is private.** Skipped without a comment.
- **Kernel-team packages** (#136). An item whose source package is `linux`
  or anything derived from it (`linux-signed*`, `linux-hwe-*`, `linux-oem-*`,
  per-cloud and flavour kernels, `linux-firmware`, ...) is skipped before any
  check runs: the kernel team has its own SRU and upload workflow. DKMS
  drivers such as `backport-iwlwifi-dkms` are *not* kernel packages here and
  are triaged normally. A bug's `linux` task only counts while it's still
  open (#165): one marked Invalid/Won't Fix/etc. is a ruled-out guess, not
  a live ask, and doesn't put the bug's other, still-open tasks out of
  scope.
- **Uploads to a `-backports` pocket** (#172). When the new changelog entry
  targets e.g. `noble-backports`, the item follows the
  [backports process](https://ubuntu.com/project/docs/how-ubuntu-is-made/processes/backports/),
  which these checks don't model -- a `~24.04.1` version isn't a botched
  no-change rebuild, and a backport isn't an SRU. Skipped before any check,
  for now, nothing persisted. On a bug, the newest debdiff per series
  counts, and one backport among them is enough.
- **The merge proposal isn't about an Ubuntu source package** (#139). The
  sponsoring report lists a proposal whenever any uploading team is a
  requested reviewer, which in September 2026 briefly pulled in two decades
  of abandoned proposals on upstream *projects*. Those have no `debian/`
  directory, so the bot has nothing true to say about them and skips before
  any check runs.
- **A brand-new bug** gets a ten-minute grace period, so a contributor can
  finish attaching things before being reviewed.
- **Nothing changed** since the last pass. The bot remembers the facts it
  acted on and won't repeat itself.
- **A lookup was inconclusive.** If Launchpad or the archive couldn't be
  read, the pass persists nothing and the item is retried later, rather than
  acting on a guess. Since #138 the audit trail names the check whose lookup
  failed, so `stats.py` can tell a real failure from a healthy skip -- each
  of these reasons records its own outcome rather than one catch-all. The
  same goes for an LLM review whose call failed (an outage, a timeout, the
  run's call budget running out) since #169: nothing is posted and the item
  is retried, recorded as `llm-failed:<review>`.
- **A human reviewer is already engaged** (#94/#106). Non-blocking
  question-tier findings are dropped entirely -- a reviewer in the
  conversation doesn't need the bot's suggestions -- while real blockers are
  still posted.
- **A reviewer already said it.** Findings a human has already covered in
  their comments are dropped rather than restated.
- **Dry-run mode.** Nothing is ever written; intended writes are logged.

## Operator notifications

Separate from anything a contributor sees, the bot pings its operator over a
Mattermost webhook (`notify.py`, disabled in dry-run) when a situation needs
a human's attention rather than a contributor's: a preview diff Launchpad
never generated, the five archive-importer and rich-history cases in Check 6
(including an upload to `debian/*` that won't autoclose its MP), and
exhaustion of a run's LLM call budget.
