MoneyMantra 9 MF Research Universe v8.1.1 — GitHub Actions hotfix

Why the first Refresh Universe Data run failed:
The workflow enabled actions/setup-python pip caching but the repository did not contain requirements.txt or pyproject.toml. setup-python therefore stopped before the refresh script ran.

What this patch changes:
1. Adds requirements.txt at the repository root.
2. Adds cache-dependency-path: requirements.txt to cached Python workflows.
3. Updates actions/checkout from v4 to v5.
4. Updates actions/setup-python from v5 to v6.

How to apply:
Copy everything inside this hotfix folder into the cloned repository root, replace files when asked, then commit and push.
