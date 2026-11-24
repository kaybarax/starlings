# Changelog

## 0.2.0 (unreleased)

Starlings can now be plugged into a project with a `starlings.toml` and one check command, with no project-side driver code.

### Added

- `starlings release [--dry-run | --publish] [--approve <review>]`: sync the mirror authority, prepare a batch named after the release and staging heads, plan, run and check the release gate, re-read the upstream heads, promote and publish. A passing dry run's receipt is reused. Every stop names the step, the code and what the exit status means.
- `[authority] mode = "mirror"` with `upstream` and `publish_staging`, and the commands `authority sync`, `authority publish` (one atomic, never forced push) and `authority discard` (sets an unpublishable promotion aside under `refs/discarded/`). The local bare repository remains the only authority. All network Git lives in the new `upstream.py`; credentials in URLs are refused in the policy and redacted in output.
- Check placeholders `{base}`, `{head}` and `{context}`. The context file gives a check the plan, candidate, gate and the plan's live approvals, so checks never read the state database.
- `[environment]` with `home = "isolated" | "operator"` and `pass = [...]` for native checks, covered by the trusted policy digest and recorded in each check's evidence.
- `[identity]` with `name` and `email` for the commits Starlings creates.
- Approvals a run's checks were shown are bound into the receipt through the retained context file. A receipt whose consumed approval was revoked or expired is stale, so it is not promoted, reused or published.
- `requires_starlings = ">=x.y"` in the policy.
- Default state directory `~/.local/state/starlings/projects/<project>` and default mirror authority `authority.git` inside it.

### Changed

- The `STARLINGS_COMMIT_NAME` and `STARLINGS_COMMIT_EMAIL` environment variables, which existed only on an unreleased branch, are replaced by `[identity]`.
- `batch prepare` accepts the policy bytes from the caller, so `starlings release` uses the policy at the staging head rather than the working directory.

### Not included

- No tag and no published release: this entry describes the working tree.
