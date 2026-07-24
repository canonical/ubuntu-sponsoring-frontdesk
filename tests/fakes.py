"""Lightweight stand-ins for launchpadlib objects used across the test suite."""

import datetime
import types

BOT = "https://api.launchpad.net/devel/~ubuntu-sponsoring-bot"
HUMAN = "https://api.launchpad.net/devel/~marco"
DEVEL_SERIES = "Noble"

# Well past any grace period (#91) -- the default for fakes whose tests
# don't care about bug age, so existing tests aren't affected by it.
OLD_ENOUGH = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)


class FakePerson:
    def __init__(self, link):
        self.self_link = link


class FakeHostedFile:
    """Stand-in for launchpadlib's HostedFile (what `preview_diff.diff_text`
    or `attachment.data` returns): `.open().read()` gives the raw bytes,
    or raises when fail=True (a fetch failure)."""

    def __init__(self, content, fail=False):
        self._content = content.encode() if isinstance(content, str) else content
        self.fail = fail
        self.opens = 0  # fetch counter, for the diff-memoization tests (#39)

    def open(self):
        if self.fail:
            raise TimeoutError("simulated Launchpad timeout")
        self.opens += 1
        return self

    def read(self):
        return self._content


# A readable diff touching no debian/changelog: checks 5/6 conclusively find
# nothing to check (False), rather than "diff unreadable" (None/inconclusive).
# End-to-end tests exercising a bounce need this since design #31: an
# inconclusive pass posts nothing at all, so a default FakeDiff (no
# diff_text) would suppress the very aggregate those tests assert on.
CLEAN_DIFF_TEXT = "diff --git a/src/foo.c b/src/foo.c\n@@ -1 +1 @@\n-old\n+new\n"


class FakeDiff:
    def __init__(
        self,
        link,
        lines,
        conflicts="",
        diff_text=None,
        date_created=None,
        source_revision_id=None,
    ):
        self.self_link = link
        self.diff_lines_count = lines
        self.conflicts = conflicts  # string of conflicting files; empty = none
        # Anchor for "since the current push" logic (#35 human-engagement).
        self.date_created = date_created
        # The MP's proposed tip commit sha (#49 case B ancestry check).
        self.source_revision_id = source_revision_id
        if diff_text is not None:
            self.diff_text = FakeHostedFile(diff_text)


class FakeBugRef:
    def __init__(self, title):
        self.title = title


class FakeMP:
    resource_type_link = "https://api.launchpad.net/devel/#branch_merge_proposal"

    def __init__(
        self,
        target="git+ssh://.../debian/sid",
        source="refs/heads/merge-1.2-3-stonking",
        diff=None,
        conflicts="",
        queue_status="Needs review",
        bugs=None,
        package="testpkg",
        date_created=None,
        no_diff=False,
        votes=None,
    ):
        self.votes = votes or []
        self.target_git_path = target
        self.source_git_path = source
        # Linked bugs, used as a merge-detection fallback when the source
        # branch name doesn't look like a merge (see checks._is_merge_proposal).
        self.bugs = bugs or []
        # Conflict state lives on the preview diff, mirroring real Launchpad.
        # no_diff=True forces preview_diff to a genuine None (Launchpad
        # hasn't generated a diff yet/at all), distinct from `diff` being
        # unset (which just means "use the default FakeDiff").
        self.preview_diff = (
            None
            if no_diff
            else (diff if diff is not None else FakeDiff("/diff/1", 42, conflicts=conflicts))
        )
        # Used by checks._diff_missing_is_still_generating's grace-period
        # check when preview_diff is None.
        self.date_created = date_created
        # The MP's submitter, excluded (with the bot) by the #35
        # human-engagement check.
        self.registrant_link = HUMAN
        self.queue_status = queue_status
        self.self_link = f"https://api.launchpad.net/devel/~human/ubuntu/+source/{package}/+git/{package}/+merge/1"
        self.web_link = (
            f"https://code.launchpad.net/~human/ubuntu/+source/{package}/+git/{package}/+merge/1"
        )
        self.all_comments = []
        self.created_comments = []
        self.created_votes = []

    def createComment(self, content, vote=None):
        self.created_comments.append(content)
        self.created_votes.append(vote)


class FakeTask:
    def __init__(self, name, status, date_incomplete=None):
        self.bug_target_name = name
        self.status = status
        # Launchpad maintains this on transition to Incomplete; the sweep
        # (#66) uses it as the bounce reference time.
        self.date_incomplete = date_incomplete
        self.saved = 0

    def lp_save(self):
        # Real writes are `task.status = X; task.lp_save()` (#92 --
        # transitionToStatus was never a real bug_task operation; found
        # live when the first real Incomplete write hit an AttributeError).
        # status is already set as a plain attribute by the caller before
        # this runs; just record that a save happened, for tests that
        # want to confirm the real write shape was used.
        self.saved += 1


# A harmless plain patch (touches no debian/ file): attachment-content
# checks (#62) treat it as a normal contribution shape and stay silent,
# so tests that only care about attachment *metadata* keep passing.
_DEFAULT_PATCH = "--- a/src/x.c\n+++ b/src/x.c\n@@ -1 +1 @@\n-a\n+b\n"


class FakeAttachment:
    def __init__(
        self,
        title,
        type="Unspecified",
        content=None,
        fail_fetch=False,
        date_created=None,
    ):
        self.title = title
        self.type = type  # Launchpad's patch flag: "Patch" when ticked
        self.self_link = f"https://api.launchpad.net/devel/bug/1/+attachment/{title}"
        self.data = FakeHostedFile(_DEFAULT_PATCH if content is None else content, fail=fail_fetch)
        # Real attachments have no date of their own; their upload
        # message's date_created is the timestamp (#66).
        self.message = types.SimpleNamespace(date_created=date_created)


class FakeVote:
    """Stand-in for an MP's CodeReviewVoteReference."""

    def __init__(self, reviewer, comment_link=None):
        self.reviewer_link = f"https://api.launchpad.net/devel/{reviewer}"
        # Non-None means the reviewer actually reviewed (posted a vote comment).
        self.comment_link = comment_link


class FakeSubscription:
    def __init__(self, person):
        self.person_link = f"https://api.launchpad.net/devel/{person}"


class FakeBug:
    resource_type_link = "https://api.launchpad.net/devel/#bug"

    def __init__(
        self,
        tasks=None,
        description="",
        tags=None,
        title="",
        attachments=None,
        linked_merge_proposals=None,
        id=1,
        subscriptions=None,
        date_created=None,
    ):
        self.id = id
        # Direct subscriptions; queue items carry a sponsoring team (#76).
        # Default matches the common case so most tests need no change.
        self.subscriptions = (
            subscriptions if subscriptions is not None else [FakeSubscription("~ubuntu-sponsors")]
        )
        # New-bug grace period (#91). Default well past it so existing
        # tests are unaffected; pass a recent timestamp to exercise it.
        self.date_created = date_created if date_created is not None else OLD_ENOUGH
        # The bug's reporter -- the submitter for the #72 human-engaged
        # check, mirroring FakeMP.registrant_link.
        self.owner_link = HUMAN
        self.bug_tasks = tasks or []
        self.description = description
        self.tags = tags or []
        self.title = title
        self.attachments = attachments or []
        self.linked_merge_proposals = linked_merge_proposals or []
        self.self_link = "https://api.launchpad.net/devel/bug/1"
        self.messages = []
        self.new_messages = []
        self.subscribed_by = []

    @property
    def bug(self):
        return self

    def newMessage(self, content):
        self.new_messages.append(content)

    def unsubscribe(self, person=None):
        pass

    def subscribe(self, person=None):
        self.subscribed_by.append(person)


class FakeBugMessage:
    def __init__(self, owner_link, content, date_created=None):
        self.owner_link = owner_link
        self.content = content
        self.date_created = date_created


class FakeMPComment:
    def __init__(self, author_link, message_body, date_created=None):
        self.author_link = author_link
        self.message_body = message_body
        self.date_created = date_created


class FakeSeries:
    def __init__(self, name, version=None, status="Supported"):
        self.name = name
        self.version = version
        self.status = status


class FakePublication:
    def __init__(self, version, pocket="Release"):
        self.source_package_version = version
        self.pocket = pocket


class FakeArchive:
    """Stand-in for a distribution's main_archive: getPublishedSources
    returns the configured publications regardless of filters."""

    def __init__(self, pubs=None, fail=False):
        self.pubs = pubs or []
        self.fail = fail

    def getPublishedSources(self, **kwargs):
        if self.fail:
            raise TimeoutError("simulated Launchpad timeout")
        return self.pubs


class FakeDistribution:
    def __init__(self, devel_series_name="noble", archive=None, series=None):
        self.current_series = FakeSeries(devel_series_name)
        self.main_archive = archive if archive is not None else FakeArchive()
        # Full series table (archive_lookup.supported_series_ordered, #58);
        # empty by default -- only the SRU newer-series tests populate it.
        self.series = series or []

    def getSeries(self, name_or_version):
        return FakeSeries(name_or_version)


class FakeRoot:
    """Stand-in for the launchpadlib root object (self.lp)."""

    def __init__(self, me_link=BOT, devel_series_name="noble"):
        self.me = FakePerson(me_link)
        self.people = {
            "ubuntu-sponsors": FakePerson("https://api.launchpad.net/devel/~ubuntu-sponsors")
        }
        self.distributions = {"ubuntu": FakeDistribution(devel_series_name)}


class FakeTriageClient:
    """Minimal lp_client for exercising main.triage_url end-to-end."""

    def __init__(self, objects, write_outcome="performed", lp=None, mode="yes"):
        self.objects = objects  # url -> lp_obj
        # Write mode, consulted by main's queue-membership guard (#76).
        self.mode = mode
        # Optional FakeRoot: when set, main passes it to facts.build_facts so
        # the archive-version fingerprint (design #37) is exercised too.
        self.lp = lp
        self.comments = []
        self.votes = []
        # Mirrors LPClient's per-item write-outcome tracking: main persists an
        # item's facts only when every write took effect. Defaults to
        # "performed" so existing tests behave like a real --yes run;
        # pass "dry-run"/"declined"/... to exercise the non-persisting paths.
        self.write_outcome = write_outcome
        self.write_outcomes = []

    def start_item(self):
        self.write_outcomes = []

    def all_writes_effective(self):
        return all(o in ("performed", "skipped-duplicate") for o in self.write_outcomes)

    def load_url(self, url):
        return self.objects[url]

    def comment(self, obj, message, vote=None):
        self.comments.append(message)
        self.votes.append(vote)
        self.write_outcomes.append(self.write_outcome)

    def unsubscribe_sponsors(self, obj):
        self.unsubscribed = getattr(self, "unsubscribed", 0) + 1
        self.write_outcomes.append(self.write_outcome)

    def set_bug_tasks_incomplete(self, obj):
        bug = obj.bug if getattr(obj, "resource_type_link", "").endswith("bug_task") else obj
        resolved = (
            "Fix Released",
            "Fix Committed",
            "Won't Fix",
            "Invalid",
            "Incomplete",
        )
        changed = {}
        for task in bug.bug_tasks:
            if "(Ubuntu" in task.bug_target_name and task.status not in resolved:
                task.status = "Incomplete"
                task.lp_save()
                changed[task.bug_target_name] = "Incomplete"
                self.write_outcomes.append(self.write_outcome)
        return changed

    def set_bug_tasks_new(self, obj):
        bug = obj.bug if getattr(obj, "resource_type_link", "").endswith("bug_task") else obj
        changed = {}
        for task in bug.bug_tasks:
            if "(Ubuntu" in task.bug_target_name and task.status == "Incomplete":
                task.status = "New"
                task.lp_save()
                changed[task.bug_target_name] = "New"
                self.write_outcomes.append(self.write_outcome)
        return changed

    def set_bug_tasks_fix_released(self, obj):
        bug = obj.bug if getattr(obj, "resource_type_link", "").endswith("bug_task") else obj
        terminal = ("Fix Released", "Fix Committed", "Won't Fix", "Invalid")
        changed = {}
        for task in bug.bug_tasks:
            name = task.bug_target_name or ""
            if name.endswith("(Ubuntu)") or name.endswith(f"(Ubuntu {DEVEL_SERIES})"):
                if task.status not in terminal:
                    task.status = "Fix Released"
                    task.lp_save()
                    changed[name] = "Fix Released"
                    self.write_outcomes.append(self.write_outcome)
        return changed


class FakeAudit:
    """In-memory audit sink so tests don't touch audit.jsonl."""

    def __init__(self):
        self.records = []

    def record(self, **kwargs):
        self.records.append(kwargs)


class FakeLLM:
    def __init__(self, bug_result=("READY_FOR_HUMAN", ""), mp_result=("READY_FOR_HUMAN", "n/a")):
        self.bug_result = bug_result
        self.mp_result = mp_result

    def start_item(self, url=""):
        # #100: mirrors LLMReviewer.start_item (per-item budget/url reset).
        self.started_items = getattr(self, "started_items", [])
        self.started_items.append(url)

    def triage_bug(self, obj):
        return self.bug_result

    def triage_mp(self, obj, diff_text=None):
        return self.mp_result

    # Check 7's escape hatch (#58): default False = "the bug text doesn't
    # say it's fixed in newer series", the full-advisory path.
    fixed_in_newer = False
    # Rule B's comment judgment (#66): True/False/None.
    bounce_addressed = False

    def review_bounce_response(self, bounce_reason, comments):
        self.bounce_queries = getattr(self, "bounce_queries", [])
        self.bounce_queries.append((bounce_reason, list(comments)))
        return self.bounce_addressed

    def review_fixed_in_newer_series(self, bug_text, series_names):
        self.newer_series_queries = getattr(self, "newer_series_queries", [])
        self.newer_series_queries.append((bug_text, list(series_names)))
        return self.fixed_in_newer

    # #106: which 1-based finding numbers the engaged reviewer's own
    # comments already cover -- default empty, matching pre-#106 behavior.
    covered_findings = set()

    def review_findings_already_covered(self, findings, reviewer_comments):
        self.covered_findings_queries = getattr(self, "covered_findings_queries", [])
        self.covered_findings_queries.append((list(findings), list(reviewer_comments)))
        return self.covered_findings
