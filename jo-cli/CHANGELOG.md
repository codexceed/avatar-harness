# Changelog

## [1.1.0](https://github.com/codexceed/avatar-harness/compare/jo-cli-v1.0.0...jo-cli-v1.1.0) (2026-06-17)


### Features

* **cockpit:** instant ctrl+c via run-task race + signal handlers (ADR-0024) ([#81](https://github.com/codexceed/avatar-harness/issues/81)) ([a4d5388](https://github.com/codexceed/avatar-harness/commit/a4d53887d15d06abaee3227be95c239a9ed0ba7a))
* **cockpit:** prompt history, selection-aware ctrl+c, quit-after-failure ([#76](https://github.com/codexceed/avatar-harness/issues/76)) ([ae7014e](https://github.com/codexceed/avatar-harness/commit/ae7014e7ab0d8038b2336f203cd3c2bb23732ec7))

## [1.0.0](https://github.com/codexceed/avatar-harness/compare/jo-cli-v0.1.0...jo-cli-v1.0.0) (2026-06-16)


### ⚠ BREAKING CHANGES

* the import package is renamed `avatar_harness` -> `avatar`, and the batch CLI command `avatar-harness` -> `avatar`. The PyPI distribution name `avatar-harness` is unchanged.

### Code Refactoring

* uv workspace + flat layout + rename import to `avatar` (1/2) ([#70](https://github.com/codexceed/avatar-harness/issues/70)) ([b7b06f9](https://github.com/codexceed/avatar-harness/commit/b7b06f93b07745b3f7752adfc20e56d3cf8ece03))

## Changelog

All notable changes to `jo-cli` are documented here (managed by release-please).
