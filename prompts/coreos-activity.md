<!--
CoreOS weekly activity prompt -- the successor to the coreos-activity skill.

Scope is deliberately narrow: the coreos org and openshift/os. That makes it a
useful validity check on the store, because the expected answer is one the
CoreOS team already knows.

Two traps this scope is prone to, both called out in the prompt:

  - fedora/bootc is on GitLab and is NOT collected. The skill this replaces
    covered it. A report that omits the blind spot reads as "bootc was quiet".
  - FCOS stream releases are *issues* in fedora-coreos-streams, not releases.
    An agent checking only releases will report "no releases this week".
-->

You are producing the weekly CoreOS activity report. Scope is **the CoreOS org
and RHCOS only** — the `coreos` and `rhcos` projects. Everything else the tools
can see is out of scope for this report.

Reader: the CoreOS team lead. Technical, time-poor. Lead with what changed and
what needs a decision; keep evidence one click away.

    /workspace/.venv/bin/al0g

Read commands. All accept `--json`; `--distro` and `--since` (24h, 7d, 30d):

    al0g stats    --distro coreos --distro rhcos --since 7d
    al0g top      --distro coreos --distro rhcos --since 7d --limit 25
    al0g list     --distro coreos --distro rhcos --kind pr --state merged --since 7d
    al0g list     --distro coreos --distro rhcos --kind issue --since 7d
    al0g releases --distro coreos --since 30d
    al0g search   "<terms>" --distro coreos
    al0g show     <id>
    al0g fetch    <id> --diff                      live PR diff
    al0g fetch    <id> --comments --since 7d       the discussion behind a count
    al0g sources show coreos                       what is collected, and when

Work entirely through these commands. Do not read `data/` and do not query the
database directly — the storage layout is an implementation detail.

## What this scope does and does not cover

In scope: coreos-assembler, fedora-coreos-config, fedora-coreos-streams,
ignition, butane, afterburn, zincati, chunkah, bootupd, and openshift/os.

**Not covered — say so plainly rather than letting it read as a quiet week:**

  - **fedora/bootc is invisible.** Those repositories are on GitLab and there
    is no GitLab collector. The report this replaces included bootc; this one
    cannot. State the blind spot near the top.
  - **RHCOS has no GitHub releases.** RHCOS ships as OCP/RHEL errata, so
    release data for `rhcos` is expected to be empty. That is not a quiet week.
  - **FCOS stream releases are issues, not releases.** They are filed in
    `fedora-coreos-streams` with titles like `next: new release on <date>
    (<version>)`. Find them explicitly; do not report "no releases" without
    checking.
  - Comments exist for both projects, so discussion analysis is meaningful here.

## Task

Report the last 7 days.

1. **Overview.** New and active items, comments and releases, per repository.
2. **What landed.** Merged PRs, each with what it changes, plus notable closed
   issues. For anything touching bootupd, bootc, sysext, composefs, Secure Boot
   or the stream pipeline, say what it implies.
3. **Where the discussion is.** Items with recent comment activity, ranked by
   comments *in the window*, and what each argument is actually about. Say when
   a thread is CI or repo plumbing rather than design — it changes how much
   attention it deserves.
4. **Releases and streams.** Stream release issues for next, testing and
   stable, with versions and dates, plus any coreos releases.
5. **Key themes.** Three to five threads of work visible across the week.
6. **Needs attention.** Anything blocked, failing or awaiting a decision, with
   the reason and a link.

## Output

Markdown. Every factual claim carries an item id or URL. Mark your reading of
intent as interpretation. State the date range covered and the exact scope.

## Rules

  - Read only. Never run `al0g sync`.
  - An empty result is a finding about collection before it is a finding about
    activity. Confirm a repository is collected before calling it quiet.
  - No filler, no hedging boilerplate, no restating the task.
