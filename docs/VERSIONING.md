# Versioning and releases

KARAOKE APP uses a project-specific versioning convention during its
pre-alpha phase. It is intentionally not standard SemVer incrementing.

## Version components

- **Major**: Major project milestones and substantial overall evolution.
  `1.0.0` means the first stable release, not merely the introduction of a
  major feature.
- **Minor**: User-facing improvements, meaningful feature enhancements, and
  changes driven by feedback from friends, family, and other early users.
- **Patch**: Internal maintenance that does not materially change the
  application's appearance or user experience, including bug fixes,
  performance optimizations, technical debt reduction, dependency
  maintenance, and other behind-the-scenes improvements.

## Current status

The application is in the `0.x.y` pre-alpha series. Until the maintainer
explicitly decides the application is sufficiently mature:

- Keep the major version at `0`.
- Do not automatically promote the project to `1.0.0`.
- Treat all `0.x.y` releases as pre-alpha.
- Expect functionality, design, architecture, and implementation details to
  evolve.
- Do not promise backward compatibility or production-level stability.
- Clearly communicate pre-alpha status in GitHub Releases and release notes.

The transition to `1.0.0` requires the maintainer's explicit decision after
evaluating real-world feedback and the application's overall maturity.
Completing implementation tasks, passing tests, publishing an executable, or
successfully building through GitHub Actions does not independently justify a
`1.0.0` release.

## Examples

| Version | Meaning |
|---|---|
| `v0.0.1` | Initial public pre-alpha executable release |
| `v0.0.2` | Bug fixes, optimizations, and internal maintenance |
| `v0.0.3` | Additional bug fixes and technical improvements |
| `v0.1.0` | User-feedback-driven improvements or meaningful user-facing enhancements |
| `v0.1.1` | Maintenance and bug fixes following that release |
| `v0.2.0` | Another meaningful round of user-facing improvements |
| `v1.0.0` | First stable release after sufficient real-world testing and meaningful user feedback |

These examples are illustrative, not a mandatory release schedule.

## Source of truth

`karaoke/version.py` is the canonical application version used by the GUI,
the Flask application, and the Windows executable metadata. The Git tag is
the release authority. The release workflow extracts the version from the tag,
passes it to the build, and fails if it does not equal `karaoke/version.py`.
There is no independently maintained version in the GUI or CI workflow.

## Release rules

1. Use tags of the form `vMAJOR.MINOR.PATCH`.
2. Never overwrite or silently replace an existing published release.
3. Every release must have its own corresponding Git tag and release notes.
4. Keep the version consistent across `karaoke/version.py`, the executable
   metadata, the application window, the release title, and the release tag.
5. Do not automatically increment the version merely because a GitHub Actions
   build runs.
6. Do not create or publish a release without the appropriate version tag and
   the maintainer's authorization.
7. GitHub Releases remain pre-releases until the maintainer explicitly
   authorizes a stable release.

## Initial public executable release

- Tag: `v0.0.1`
- Release title: `Karaoke App v0.0.1`
- Mark as a GitHub pre-release.
- Label it as an early pre-alpha build.
- Include a disclaimer that bugs, incomplete functionality, and unexpected
  behavior are possible.
- Attach the Windows x64 ZIP and its SHA-256 checksum.
- Do not include copyrighted media, source AI models, caches, or personal
  data.
