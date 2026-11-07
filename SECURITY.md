# Security policy

## The threat model, up front

couchdeck gives a shell to anyone who can load its page. It has **no
authentication by design**: it is meant to run on a private network (such as a
Tailscale tailnet) where every device that can reach the port belongs to the
person running it. "Someone on my private network can get a shell" is the
documented behaviour, not a vulnerability.

What *is* in scope is anything that lets someone **outside** that boundary act
through it, for example:

- a web page the user visits driving the terminal or the API (cross-site
  requests, WebSocket origin bypass, DNS rebinding, clickjacking);
- script injection through session names, pane output, launcher labels or
  notes;
- command injection through API parameters (session names, models, project
  paths) or path traversal out of `PROJECTS_ROOT`;
- the public-address refusal (`COUCHDECK_ALLOW_PUBLIC`) or the host allowlist
  being bypassable;
- a request that can wedge or crash the tmux server for every session.

Known and accepted: no rate limiting on session create or attach.

## Supported versions

Only the latest commit on the default branch is supported.

## Reporting a vulnerability

Please report privately through GitHub: open the repository's **Security**
tab and choose **Report a vulnerability**. Do not open a public issue.

Include what you did, what happened, and the commit you tested. You should get
a first reply within a week. Once a fix is released we will credit you in the
release notes unless you would rather stay anonymous.
