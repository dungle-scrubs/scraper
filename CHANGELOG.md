# Changelog

## [0.2.2](https://github.com/dungle-scrubs/scraper/compare/v0.2.1...v0.2.2) (2026-10-27)


### Added

* add an isolated public CLI scrape profile ([#40](https://github.com/dungle-scrubs/scraper/issues/40)) ([e30b14f](https://github.com/dungle-scrubs/scraper/commit/e30b14f2aecbbe771e6b71092019cc81b104c476))


### Fixed

* enforce patched runtime dependency floors ([655c7e4](https://github.com/dungle-scrubs/scraper/commit/655c7e40e01e8dadf944a30b072629586599c265))
* pin CI to the supported Ubuntu 24.04 runner ([#41](https://github.com/dungle-scrubs/scraper/issues/41)) ([ead154b](https://github.com/dungle-scrubs/scraper/commit/ead154b83db646db5ea8e6a0d792e13cf858c32a))


### Maintenance

* build and publish with uv, attest via attest-action ([#30](https://github.com/dungle-scrubs/scraper/issues/30)) ([4bc2aee](https://github.com/dungle-scrubs/scraper/commit/4bc2aeec638281e01021bdeefba4367075310ebc))

## [0.2.1](https://github.com/dungle-scrubs/scraper/compare/v0.2.0...v0.2.1) (2026-09-09)


### Added

* initial public release ([a05187b](https://github.com/dungle-scrubs/scraper/commit/a05187b38858ef75b2ddaf22fcc54ae02cedc9a8))


### Fixed

* harden SSRF DNS fan-out (P1) and disconnect replay (P2) ([#16](https://github.com/dungle-scrubs/scraper/issues/16)) ([f559cb1](https://github.com/dungle-scrubs/scraper/commit/f559cb17cbb428d1c8796f15a8fce10db6593203))
* resolve all verified audit findings ([#15](https://github.com/dungle-scrubs/scraper/issues/15)) ([41a3998](https://github.com/dungle-scrubs/scraper/commit/41a399815418325285e938970d61f0972f972bc4))


### Documentation

* add RFC-01 draft - scrape pipeline interface consolidation ([#27](https://github.com/dungle-scrubs/scraper/issues/27)) ([ebcc9b9](https://github.com/dungle-scrubs/scraper/commit/ebcc9b9b8e905cb2d2e677256ad16b59b744835a))


### Maintenance

* add brand assets - mark (svg, 512, 1024) and social preview ([#29](https://github.com/dungle-scrubs/scraper/issues/29)) ([4e21f82](https://github.com/dungle-scrubs/scraper/commit/4e21f821cd60a4ea1bc37a58ec0ad056eee0887c))
* **deps:** bump actions/checkout from 4.3.1 to 7.0.1 ([#21](https://github.com/dungle-scrubs/scraper/issues/21)) ([873ccdd](https://github.com/dungle-scrubs/scraper/commit/873ccddb7cb36d4abb7636646c9b39efbdbf08df))
* **deps:** bump actions/setup-python from 5.6.0 to 7.0.0 ([#22](https://github.com/dungle-scrubs/scraper/issues/22)) ([a22aa08](https://github.com/dungle-scrubs/scraper/commit/a22aa08621f5fc153243b79907b8356eb18152a3))
* **deps:** bump astral-sh/setup-uv from 6.8.0 to 10.0.1 ([#24](https://github.com/dungle-scrubs/scraper/issues/24)) ([a78e4fb](https://github.com/dungle-scrubs/scraper/commit/a78e4fb71840bbd9562e4c0e91c42d0250451291))
* **deps:** bump audited dependencies; ignore unfixed nltk advisory ([#26](https://github.com/dungle-scrubs/scraper/issues/26)) ([458596f](https://github.com/dungle-scrubs/scraper/commit/458596fd25bf41d633cbc08fdebd197c313a188c))
* **deps:** bump googleapis/release-please-action from 4.4.1 to 5.0.0 ([#18](https://github.com/dungle-scrubs/scraper/issues/18)) ([04b5a77](https://github.com/dungle-scrubs/scraper/commit/04b5a77a4143274523538af1a6d7c17d5651ad9d))
* **deps:** bump pypa/gh-action-pypi-publish from 1.14.0 to 1.14.2 ([#23](https://github.com/dungle-scrubs/scraper/issues/23)) ([d0bab90](https://github.com/dungle-scrubs/scraper/commit/d0bab90962e3409a6baa626410286557cb9a2b40))
* **deps:** bump python from 3.12-slim-bookworm to 3.14-slim-bookworm ([#11](https://github.com/dungle-scrubs/scraper/issues/11)) ([d3699d7](https://github.com/dungle-scrubs/scraper/commit/d3699d7f85327d5a24f44221dba5789fab702f06))

## [0.2.0](https://github.com/dungle-scrubs/scraper/compare/v0.1.0...v0.2.0) (2026-03-06)


### ⚠ BREAKING CHANGES

* rename to dendrite-scraper

### Added

* **cli:** expose stable CLI contract ([7acb360](https://github.com/dungle-scrubs/scraper/commit/7acb3607de6aca52b969064f754a298069f8639e))


### Changed

* rename to dendrite-scraper ([f51a2d3](https://github.com/dungle-scrubs/scraper/commit/f51a2d3a2ad0bb799e57c261a1196db26ab874da))


### Maintenance

* initial commit ([f7be644](https://github.com/dungle-scrubs/scraper/commit/f7be644f01b31649884c611584ee32b42a17ebab))
* prepare for public release ([c1083ab](https://github.com/dungle-scrubs/scraper/commit/c1083ab81d7060181ca7cbe6647bdd6f1aaabb81))

## Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
