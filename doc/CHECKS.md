# Checks reference

The complete, current list of what Frontdesk checks for, grouped by
category. This is the authoritative list — the Discourse post links here
rather than trying to keep its own copy in sync. Each check lives in
`checks.py` unless noted; see `doc/GLOSSARY.md` for the tier/kind
vocabulary (`closing` / `incomplete` / `question`) and `doc/design_journal.md`
for the full rationale behind each one (design entry numbers below).

Tiers, briefly: **closing** = the item is already resolved, this ends the
pass with its own comment. **incomplete** = a hard blocker; aggregates
into the "needs fixing" comment and drives a `Needs Fixing` vote.
**question** = non-blocking; aggregates into a "please verify" or "nice
to have" note, never changes status.

## Is there still something to sponsor?

These run first and can end the pass on their own — no point flagging
changelog style on a request that's already resolved.

| Check | Tier | Scope | What it does |
|---|---|---|---|
| `check_administrative_state` | closing | MP + bug | Every Ubuntu task already closed and something's landed / the MP is Merged — comment (if any Fix Committed tasks remain) and unsubscribe; a fully Fix Released item closes silently. |
| `check_nothing_to_sponsor` | closing | bug only | No patch and no linked MP (sync requests exempt), or every series the bug asks sponsoring for is already covered by a linked MP's review — unsubscribe, the real review lives elsewhere. Also handles needs-packaging bugs already published or queued. |
| `check_empty_diff` | closing | MP | The diff is empty — already landed, nothing left to review. |
| `check_stale_version` | closing / incomplete | MP + bug (debdiff attachment) | Compares the proposed version against every pocket published for the target series. Already uploaded with matching content → closing (or a short defer if <24h old / still in the upload queue); a version collision or stale version → incomplete, asks for a rebase. |

## Targeting and process

| Check | Tier | Scope | What it does |
|---|---|---|---|
| `check_target_branch` | incomplete | MP | Merge MPs must target `debian/sid` or `debian/experimental`, not `ubuntu/devel`. SRU-shaped MPs (changelog entry for a stable series) must target their own `ubuntu/<series>-devel`, not `ubuntu/devel` or the wrong series. |
| `check_mp_conflicts` | incomplete | MP | The MP has merge conflicts against its target. Suppressed for the pass if `check_target_branch` already fired — conflicts are usually just a symptom of comparing against the wrong branch. |
| `check_sru_newer_series` | question (`verify`) | bug | SRU policy requires the fix to land in the development release (and every newer supported series) first. Checked via task status / linked MP / patch attachment evidence, with an LLM fallback that reads the bug text for "already fixed there" before flagging. |
| `check_sru_version_suffix_convention` | question (`advisory`) | MP + bug (debdiff attachment) | The proposed version should follow the *recommended* SRU version-suffix convention (`ubuntu0.N` on top of a stable release, not the `ubuntuN` numbering devel/regular uploads use) — non-blocking, since it's a style recommendation, not a correctness guarantee (see `check_sru_version_newer_series_precedence` for the actual correctness check). Delegates to [`ubuntu-lint`](https://github.com/ubuntu/ubuntu-lint)'s own `check_sru_version_string_convention` rather than reimplementing it; requires `python3-ubuntu-lint` on the host, fails safe (silently skips) when it's absent. |
| `check_sru_version_newer_series_precedence` | incomplete | MP + bug (debdiff attachment) | Two correctness problems provable from real archive state: (1) every series newer than the target must currently publish a *higher* version than the one proposed, or a future upgrade past it would keep this SRU's version instead; (2) the proposed version must never have been published anywhere else in the archive's history (Ubuntu's pool is shared across every series) — catches a version reused from an unrelated series' upload, even one long since superseded there. No separate "is this an SRU" gate: leg (2) is a real problem for a devel upload too. |

## Changelog hygiene

| Check | Tier | Scope | What it does |
|---|---|---|---|
| `check_missing_changelog_stanza` | incomplete | MP | No `debian/changelog` entry at all in the diff. |
| `check_changelog_bug_reference` | incomplete | MP + bug (debdiff attachment) | The changelog cites an `LP: #NNNNNN` bug that isn't actually reported against this source package — usually a copy-paste/typo. |
| `check_ppa_version_suffix` | incomplete | MP + bug (debdiff attachment) | The proposed version carries a leftover `~ppaN` suffix from a PPA build — not valid for an archive upload. |
| `check_xsbc_original_maintainer` | question (`advisory`) | MP + bug (debdiff attachment) | A package's first Ubuntu delta should add `XSBC-Original-Maintainer` to `debian/control`, preserving the Debian maintainer. Non-blocking — a sponsor can add it at upload time. |

## Patch shape

| Check | Tier | Scope | What it does |
|---|---|---|---|
| `check_direct_source_edit` | incomplete | MP + bug (debdiff attachment) | Files outside `debian/` are edited directly instead of via a patch under `debian/patches`. Exempt: merges, new upstream versions, native packages. |
| `check_patch_not_debdiff` | incomplete | bug | A bug's attached patch touches no `debian/` file — it's a plain code patch, not sponsorable as-is; asks for a debdiff instead. |

## LLM-assisted reviews (`llm_reviewer.py`)

Narrow, tool-less, fixed-scope prompts — not deterministic checks, but
part of the same review pass. All are non-blocking (`question` tier)
except the SRU template check, which is a hard requirement.

| Review | Tier | Scope | What it does |
|---|---|---|---|
| SRU template completeness | incomplete | bug (and any MP linked to an SRU-shaped bug) | Every linked bug's description must pass the SRU bug-template check before an SRU can be sponsored. |
| Stanza quality | question (`advisory`) | MP | Judges the new changelog stanza's quality/completeness on its own terms. |
| Changelog/diff consistency | question (`verify`) | MP | Flags contradicted claims or whole unmentioned files/fixes between the stanza and the actual diff — scoped to *what changed*, not *how it works*. |
| Feature Freeze classification | question (`verify`) | MP (devel-targeted only, post-FF) | Flags a change that looks like a new feature proposed after Feature Freeze, which needs an FFe. |
| Findings already covered by an engaged reviewer | — (suppression) | MP + bug | When a human reviewer is already active, checks whether their own comment(s) already substantively raise the same problem as a pending blocking finding, to avoid repeating it. |

## Gates (not findings)

These control whether checks run at all; they don't post anything
themselves.

| Gate | What it does |
|---|---|
| `check_human_engaged` | Is a human reviewer already actively engaged since the current diff/attachment? Suppresses non-blocking findings, and skips the LLM phase entirely when nothing blocking is on the board yet. |
| `check_sponsoring_team_subscribed` | Is this bug actually in the sponsoring queue (directly subscribed to `~ubuntu-sponsors`/`~ubuntu-security-sponsors`), or a mistaken URL? |
