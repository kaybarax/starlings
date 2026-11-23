"""Real GitHub adapter stub — wiring only, no network.

Implements the same adapter surface as ``fake_github.FakeHost`` and
raises ``hosted-unavailable`` from every operation until the separately
approved live rehearsal qualifies real event delivery, billing, branch
protections and runner parity. Nothing in this module opens a socket
or touches a real remote.
"""

from .host import hosted_unavailable


class GitHubHost:
    adapter = "github"

    def __init__(self, config):
        self.config = dict(config)
        self.host = self.config.get("host", "github.com")

    def probe(self):
        raise hosted_unavailable()

    def adapter_record(self):
        raise hosted_unavailable()

    def configure(self, workflow, routing):
        raise hosted_unavailable()

    def submit_job(self, *args, **kwargs):
        raise hosted_unavailable()

    def completion(self, submission):
        raise hosted_unavailable()

    def replay(self, kind, **fields):
        raise hosted_unavailable()

    def pull_request(self, repo, source):
        raise hosted_unavailable()

    def close_pull_request(self, repo, number, link=None):
        raise hosted_unavailable()

    def event_log(self):
        raise hosted_unavailable()
