# Quick 261014-1by: deploy.sh pip-audit installed-closure gate Summary

Deploy now audits the installed dependency closure (17 pinned packages) with `pip_audit --no-deps --disable-pip`, installs pip-audit only when missing, and no longer rewrites root's crontab.

## Commit
- 0761a43: fix(deploy): pip-audit installed closure, conditional install, drop crontab rewrite (deploy.sh only; pushed)

## Verification (literal)
- `zsh -n deploy.sh` OK; non-comment `crontab` count 0; `pip_audit --no-deps --disable-pip -r "$pins"` count 1; `CVPINS` terminator count 1; `import pip_audit` count 2.
- cv-deploy wall time: 7.8s total (baseline ~65s). Audit section 00:59:30 to 00:59:33 (3s, baseline ~30s). Result: clean audit, systemd active, enabled, :8766 owned by MainPID 2606124.
- Closure size: 17 packages (resolver output line count on RPi).
- Block path: jinja2==2.10 lists PYSEC-2026-1471/1473/1474/1475 and others, rc=1.
- Missing path: `MISSING cv-nonexistent-pkg-xyz`, rc=1.
- Crontab: 11 lines before and after deploy. Journal `REPLACE (root)` count since 00:59:29: 0.
- `systemctl is-active courtview`: active.

## Deviations
- The "Auditing N installed packages" line is not visible in a clean deploy, because the unchanged local side prints only the OK message on rc 0. The count was confirmed separately (17).
- Direct `ssh pi-cmd` is blocked by a hook, so the missing-package test piped the reference script through the `rpi` alias.

## Known Stubs
None.
