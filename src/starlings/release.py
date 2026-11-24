"""One-command release for a mirror authority.

``starlings release`` chains the commands an operator would otherwise script:
``authority sync``, ``batch prepare``, ``plan --gate release``, ``run``,
``check``, a second read of the upstream heads, ``promote`` and ``authority
publish``. It adds no authority of its own: every step is the same function the
individual command runs, with the same leases, journal and rechecks.
"""

import getpass
import shlex
import time

from . import batches, gitops, promotion, runner, upstream, verifier
from .batches import EXIT, Failure
from .gitops import GitError
from .policy import PolicyError, parse

MEANING = {
    3: "CONFLICT (exit 3): Starlings already holds a different record under the same name (a batch, an approval or a trust record), or a file it needs is missing or unwritable. Nothing was landed or published. Read the message above; the same name is never reused for different inputs.",
    4: "PENDING (exit 4): a step was accepted but has not finished. Nothing was landed or published. Run the same release command again; if it then reports a held lease or an interrupted run, run `starlings recover <batch>` first.",
    5: "BLOCKED (exit 5): a precondition does not hold, and nothing was landed or published by this command. The code in brackets says which: untrusted-policy needs `starlings policy trust`; no-signer needs `starlings signer init`; approval-required needs `--approve`; release-conflict is fixed on the staging branch; staging-ahead and release-ahead are cleared with `starlings authority discard`; lease-held means another command is running.",
    6: "STALE (exit 6): a branch, the trusted policy, an approval or the receipt changed while the release was running. Read the message above for what was and was not written, then run the release again.",
    7: "FAILED (exit 7): a check failed, the run was interrupted, or Git, the upstream or the state database reported an error. The release branch was not advanced by a failed check. Read the check logs named above, fix the cause on the staging branch and release again.",
}


def batch_name(release_head, staging_head):
    return f"release-{release_head[:12]}-{staging_head[:12]}"


def staging_policy(authority, staging, staging_head):
    result = gitops.git(authority, "cat-file", "blob", f"{staging_head}:starlings.toml", ok=(0, 128), text=False)
    if result.returncode:
        raise Failure("blocked", "no-policy", f"{staging} head {staging_head} has no starlings.toml; add the policy to the staging branch first")
    try:
        return result.stdout, parse(result.stdout.decode("utf-8"))
    except (PolicyError, UnicodeError) as exc:
        raise Failure("invalid", "invalid", f"starlings.toml at {staging} head {staging_head} is invalid: {exc}") from exc


def trust_command(ctx, authority, commit, trusted):
    return f"starlings policy trust --state-dir {shlex.quote(str(ctx.root))} --directory {shlex.quote(str(ctx.directory))} --authority {shlex.quote(str(authority))} --commit {commit} --review <review-reference>" + (f" --supersedes {trusted['digest']}" if trusted is not None else "")


def check_lines(run):
    lines = []
    for item in run.get("checks", []):
        code = item.get("exit_code")
        ended = f"killed by signal {-code}" if type(code) is int and code < 0 else f"exit {code}"
        lines.append(f"  {item['check']}: {item['status']} ({ended}) in {item.get('metrics', {}).get('duration_seconds')} s")
    return lines


def release(ctx, authority=None, mode=None, approve=None, by=None, timeout=None, owner=None, seconds=600, allow_unprotected=False, interrupt=None):
    if mode not in (None, "dry-run", "publish"):
        raise batches.invalid("release: expected --dry-run, --publish or neither")
    result = {"mode": mode or "promote", "steps": [], "timings": {}, "summary": []}
    state = {"step": "start"}

    def step(name, function, *args, **kwargs):
        if interrupt is not None and interrupt.is_set():
            raise Failure("failed", "interrupted", f"the release was stopped by a signal before the {name} step; no further step ran")
        state["step"] = name
        begin = time.monotonic()
        try:
            outcome = function(*args, **kwargs)
        finally:
            result["timings"][name] = round(time.monotonic() - begin, 3)
        result["steps"].append(name)
        return outcome

    def say(line):
        result["summary"].append(line)

    try:
        return run_release(ctx, authority, mode, approve, by, timeout, owner, seconds, allow_unprotected, interrupt, result, step, say)
    except Failure as exc:
        code = EXIT[exc.status]
        if exc.code == "interrupted":
            hint = "INTERRUPTED: the release was stopped by a signal. No receipt was signed for an unfinished run and no later step ran. " + ("The release was already promoted in the authority and waits for `starlings release --publish`. " if "promote" in result["steps"] else "Nothing was promoted or published. ") + "Run the same command again; if it reports a held lease or a running run, run `starlings recover <batch>` first."
        else:
            hint = MEANING.get(code, "")
        data = {**result, "stopped_at": state["step"], "meaning": hint, "detail": exc.data}
        raise Failure(exc.status, exc.code, f"release stopped at {state['step']} [{exc.code}]: {exc.message}" + (f"\n{hint}" if hint else ""), data) from exc
    except GitError as exc:
        data = {**result, "stopped_at": state["step"], "meaning": MEANING[7]}
        raise Failure("failed", "git", f"release stopped at {state['step']} [git]: {upstream.redact(str(exc))}\n{MEANING[7]}", data) from exc


def run_release(ctx, authority, mode, approve, by, timeout, owner, seconds, allow_unprotected, interrupt, result, step, say):
    _, synced = step("sync", upstream.sync, ctx, authority, owner, seconds)
    authority = upstream.location(ctx, authority)
    release_branch, staging_branch = synced["release"], synced["staging"]
    release_head, staging_head = synced["upstream_heads"][release_branch], synced["upstream_heads"][staging_branch]
    result.update({"authority": str(authority), "upstream": synced["upstream"], "release": release_branch, "staging": staging_branch, "release_head": release_head, "staging_head": staging_head})
    if synced.get("note"):
        say("note: " + synced["note"])
    say(f"upstream {release_branch} {release_head}")
    say(f"upstream {staging_branch} {staging_head}")
    if synced["state"] == "unpublished":
        promoted = synced["unpublished"]["commit"]
        result.update({"commit": promoted, "receipt_id": synced["unpublished"]["receipt_id"], "plan_id": synced["unpublished"]["plan_id"]})
        say(f"unpublished promotion {promoted} (receipt {synced['unpublished']['receipt_id']}) is already verified against these heads")
        return finish(ctx, authority, mode, owner, seconds, result, step, say, promoted)
    if gitops.is_ancestor(authority, staging_head, release_head):
        result["state"] = "nothing-to-release"
        say(f"Nothing to release: {release_branch} already contains {staging_branch}.")
        return "ok", result
    source, policy = staging_policy(authority, staging_branch, staging_head)
    if policy["authority"]["mode"] != "mirror":
        raise Failure("blocked", "not-mirror", f'starlings.toml at {staging_branch} head {staging_head} does not use [authority] mode = "mirror"; starlings release needs an upstream')
    batch = batch_name(release_head, staging_head)
    result["batch"] = batch
    step("prepare", batches.prepare, ctx, batch, ["release"], str(authority), source)
    try:
        _, plan = step("plan", verifier.plan, ctx, batch, "release")
    except Failure as exc:
        if exc.code != "untrusted-policy":
            raise
        command = trust_command(ctx, authority, staging_head, verifier.current_trust(ctx.store.connection, str(authority)))
        raise Failure(exc.status, exc.code, f"{exc.message}\nAfter independent review of starlings.toml and the files its checks run at {staging_head}, trust it with:\n  {command}", {"trust_command": command, "commit": staging_head}) from exc
    candidate = plan["candidate"]["commit"]
    if list(plan["candidate"]["parents"]) != [release_head, staging_head]:
        raise Failure("stale", "stale", f"the planned candidate does not merge the upstream {staging_branch} head into the upstream {release_branch} head; nothing was written. Start again.")
    result.update({"plan_id": plan["plan_id"], "commit": candidate, "checks": plan["checks"]})
    say(f"batch {batch}")
    say(f"plan {plan['plan_id']}")
    say(f"candidate {candidate}")
    if approve is not None:
        identity = by or (policy.get("identity") or {}).get("name") or getpass.getuser()
        _, approval = step("approve", promotion.approve, ctx, plan["plan_id"], identity, 86400, approve)
        result["approval"] = {"approval_id": approval["approval_id"], "by": identity, "review": approve}
        say(f"approved for this plan by {identity}: {approve}")
    status, run = step("run", runner.run, ctx, plan["plan_id"], False, timeout, owner, interrupt)
    result.update({"run_id": run.get("run_id") or (run.get("check") or {}).get("run_id"), "receipt_id": run.get("receipt_id"), "outcome": run.get("outcome"), "reused": run.get("reused", False), "logs": run.get("logs")})
    if run.get("reused"):
        say("reused the valid passing receipt already signed for this exact plan")
    for line in check_lines(run):
        say(line)
    if status != "ok":
        if status == "pending":
            raise Failure("pending", "run-pending", run.get("note") or "the run has not finished", run)
        failed = [item["check"] for item in run.get("checks", []) if item["status"] != "passed"]
        raise Failure("failed", "checks-failed", f"checks failed: {', '.join(failed) or 'unknown'}; receipt {run.get('receipt_id')} records the failure and can never promote. Logs: {run.get('logs')}\n" + "\n".join(check_lines(run)), {"receipt_id": run.get("receipt_id"), "checks": run.get("checks")})
    say(f"receipt {run['receipt_id']} ({run['outcome']})")
    status, checked = step("check", verifier.check, ctx, run["receipt_id"])
    if status != "ok":
        raise Failure(status, f"receipt-{checked['status']}", f"receipt {run['receipt_id']} is {checked['status']}: " + "; ".join(checked["reasons"]) + "; nothing was promoted", checked)
    say("receipt valid")
    if mode == "dry-run":
        result["state"] = "verified"
        say("Dry run: not promoted and not published. The same release reuses this receipt.")
        return "ok", result
    ctx.hook("release-before-promote", {"batch": batch, "plan": plan["plan_id"], "receipt": run["receipt_id"]})
    if step("reread", upstream.heads, authority, policy) != (release_head, staging_head):
        raise Failure("stale", "upstream-moved", f"the upstream {release_branch} or {staging_branch} moved during the checks. Nothing was promoted or published. Run the release again for the new heads.")
    target = f"refs/heads/{release_branch}"
    _, landed = step("promote", promotion.promote, ctx, plan["plan_id"], run["receipt_id"], owner, seconds, allow_unprotected)
    if landed.get("repeated") and gitops.resolve(authority, target) == release_head:
        step("restore", promotion.restore, ctx, plan["plan_id"], run["receipt_id"], owner, seconds, allow_unprotected)
        say("restored the recorded landing of this verified candidate, which had been set aside")
    promoted = gitops.resolve(authority, target)
    if promoted != candidate:
        raise Failure("stale", "stale", f"authority {release_branch} is {promoted}, not the verified candidate {candidate}. Nothing was published.")
    result["promotion_id"] = landed.get("promotion_id")
    say(f"promoted: authority {release_branch} is {promoted}")
    return finish(ctx, authority, mode, owner, seconds, result, step, say, promoted)


def finish(ctx, authority, mode, owner, seconds, result, step, say, promoted):
    if mode != "publish":
        result["state"] = "unpublished" if mode == "dry-run" or "promote" not in result["steps"] else "promoted"
        say(f"Not published: authority {result['release']} {promoted} waits for starlings release --publish.")
        return "ok", result
    _, published = step("publish", upstream.publish, ctx, authority, owner, seconds)
    result.update({"state": "published", "published": published["refspecs"] if published.get("published") else [], "upstream_heads": published["upstream_heads"]})
    say(f"published: upstream {' and '.join(sorted(name for name, sha in published['upstream_heads'].items() if sha == promoted))} at {promoted}")
    return "ok", result
