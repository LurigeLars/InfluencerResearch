# Security policy

Do not report credentials, cookies, access tokens, browser profiles, or other secrets in a public issue.

For security-sensitive reports, use GitHub's private vulnerability reporting feature when it is enabled for this repository. If it is unavailable, contact the repository owner through GitHub without including secret material in a public post.

Before publishing changes, review staged files for:

- credentials, tokens and cookies;
- absolute local filesystem paths or usernames;
- browser/session state;
- generated research output and logs;
- unrelated private repository or infrastructure references.

If a real credential is ever committed, revoke or rotate it first. Removing the visible line from the latest commit does not remove it from Git history.
