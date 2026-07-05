"""Lightweight stand-ins for launchpadlib objects used across the test suite."""

BOT = "https://api.launchpad.net/devel/~ubuntu-sponsoring-bot"
HUMAN = "https://api.launchpad.net/devel/~marco"
DEVEL_SERIES = "Noble"


class FakePerson:
    def __init__(self, link):
        self.self_link = link


class FakeHostedFile:
    """Stand-in for launchpadlib's HostedFile (what `preview_diff.diff_text`
    returns): `.open().read()` gives the raw bytes."""

    def __init__(self, content):
        self._content = content.encode() if isinstance(content, str) else content
        self.opens = 0  # fetch counter, for the diff-memoization tests (#39)

    def open(self):
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
    def __init__(self, link, lines, conflicts="", diff_text=None):
        self.self_link = link
        self.diff_lines_count = lines
        self.conflicts = conflicts  # string of conflicting files; empty = none
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
    ):
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
            else (
                diff
                if diff is not None
                else FakeDiff("/diff/1", 42, conflicts=conflicts)
            )
        )
        # Used by checks._diff_missing_is_still_generating's grace-period
        # check when preview_diff is None.
        self.date_created = date_created
        self.queue_status = queue_status
        self.self_link = f"https://api.launchpad.net/devel/~human/ubuntu/+source/{package}/+git/{package}/+merge/1"
        self.all_comments = []
        self.created_comments = []
        self.created_votes = []

    def createComment(self, content, vote=None):
        self.created_comments.append(content)
        self.created_votes.append(vote)


class FakeTask:
    def __init__(self, name, status):
        self.bug_target_name = name
        self.status = status

    def transitionToStatus(self, status):
        self.status = status


class FakeBug:
    resource_type_link = "https://api.launchpad.net/devel/#bug"

    def __init__(self, tasks=None, description="", tags=None, title=""):
        self.bug_tasks = tasks or []
        self.description = description
        self.tags = tags or []
        self.title = title
        self.self_link = "https://api.launchpad.net/devel/bug/1"
        self.messages = []
        self.new_messages = []

    @property
    def bug(self):
        return self

    def newMessage(self, content):
        self.new_messages.append(content)

    def unsubscribe(self, person=None):
        pass


class FakeBugMessage:
    def __init__(self, owner_link, content):
        self.owner_link = owner_link
        self.content = content


class FakeMPComment:
    def __init__(self, author_link, message_body):
        self.author_link = author_link
        self.message_body = message_body


class FakeSeries:
    def __init__(self, name):
        self.name = name


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
    def __init__(self, devel_series_name="noble", archive=None):
        self.current_series = FakeSeries(devel_series_name)
        self.main_archive = archive if archive is not None else FakeArchive()

    def getSeries(self, name_or_version):
        return FakeSeries(name_or_version)


class FakeRoot:
    """Stand-in for the launchpadlib root object (self.lp)."""

    def __init__(self, me_link=BOT, devel_series_name="noble"):
        self.me = FakePerson(me_link)
        self.people = {
            "ubuntu-sponsors": FakePerson(
                "https://api.launchpad.net/devel/~ubuntu-sponsors"
            )
        }
        self.distributions = {"ubuntu": FakeDistribution(devel_series_name)}


class FakeTriageClient:
    """Minimal lp_client for exercising main.triage_url end-to-end."""

    def __init__(self, objects, write_outcome="performed", lp=None):
        self.objects = objects  # url -> lp_obj
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
        bug = (
            obj.bug
            if getattr(obj, "resource_type_link", "").endswith("bug_task")
            else obj
        )
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
                task.transitionToStatus(status="Incomplete")
                changed[task.bug_target_name] = "Incomplete"
                self.write_outcomes.append(self.write_outcome)
        return changed

    def set_bug_tasks_fix_released(self, obj):
        bug = (
            obj.bug
            if getattr(obj, "resource_type_link", "").endswith("bug_task")
            else obj
        )
        terminal = ("Fix Released", "Fix Committed", "Won't Fix", "Invalid")
        changed = {}
        for task in bug.bug_tasks:
            name = task.bug_target_name or ""
            if name.endswith("(Ubuntu)") or name.endswith(f"(Ubuntu {DEVEL_SERIES})"):
                if task.status not in terminal:
                    task.transitionToStatus(status="Fix Released")
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
    def __init__(
        self, bug_result=("READY_FOR_HUMAN", ""), mp_result=("READY_FOR_HUMAN", "n/a")
    ):
        self.bug_result = bug_result
        self.mp_result = mp_result

    def triage_bug(self, obj):
        return self.bug_result

    def triage_mp(self, obj):
        return self.mp_result
