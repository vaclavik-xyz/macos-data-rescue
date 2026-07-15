# Security policy

## Supported versions

Security fixes are applied to the latest commit on `main`. The project is still pre-1.0 and does not currently maintain older release branches.

## Reporting a vulnerability

Do not open a public issue for a vulnerability or include customer data in a report. Use GitHub private vulnerability reporting when available, or email `filip@vaclavik.xyz` with the subject `macos-data-rescue security`.

Useful reports include the affected version or commit, the expected safety boundary, a minimal synthetic reproduction, and the impact. Remove real names, paths, filenames, manifests, reports, volume identifiers, and recovered content before sharing anything.

Especially sensitive issues include source modification, path traversal, writes outside the configured destination, symlink following, arbitrary command execution, and disclosure of manifest or report data.
