# CI requirements — what the automatic tests need, and what they do NOT

This repository is one of three that make up the ekey module; the daemon's own repository is
not public, so everything needed to work on this one is written out here rather than
cross-referenced. First question:

> *Do the GitHub Actions tests need a working connection to the ekey daemon or scanner?*

**No.** Neither the pytest suite nor the panel tests open a socket. Every HTTP call goes
through `tests/ha_component/fake_http.py`, a small stand-in for the piece of aiohttp the
client actually uses; the panel tests stub the four DOM APIs the panel touches. The
`127.0.0.1:8080` you will find in `test_api.py` and `test_coordinator.py` is a string being
asserted on — `coord.base_url == "http://127.0.0.1:8080"` — not a connection.

Verified by running both suites on a workstation with no daemon anywhere on the network:
**119 pytest tests passed, 60 panel checks passed.**

The `integration: requires running daemon` marker declared in `pytest.ini` is currently
unused — no test carries it. If one ever does, CI must select against it
(`pytest -m "not integration"`), because the hosted runner cannot reach a daemon. See
part 3.

---

## Part 1 — What CI requires

- **R1 — Runner.** `ubuntu-latest`, GitHub-hosted. No self-hosted runner, no hardware, no
  route to a daemon.
- **R2 — Python 3.14.2 or newer, and this is not a preference.** `homeassistant` 2026.3.0
  raised its floor to `requires_python >=3.14.2`; 2026.2.3 was the last release installable
  on 3.13. `requirements_test.txt` asks for `homeassistant>=2026.6.0`, so on Python 3.13 pip
  has **no candidate at all** and the job dies at the install step. The matrix in
  `ha-component.yml` and the floor in `requirements_test.txt` are one decision in two files;
  move one and you must move the other. Ground truth:
  <https://pypi.org/pypi/homeassistant/json>.
- **R3 — Real Home Assistant, installed for real.** The suite imports the actual helpers —
  `Store`, `DataUpdateCoordinator`, `websocket_api`, `panel_custom` — so that an upstream
  rename fails in CI rather than in someone's installation. The install step must therefore
  carry **no `|| true`**: swallowing a failed install turns every one of those failures into
  a silent skip, which is how this job spent its time before.
- **R4 — Node 20 for the panel, and no package.json.** The panel is plain ES modules served
  straight to the browser and the test stubs the DOM surface it uses. Staying
  dependency-free is what makes the job worth having; a bundler here would cost more than
  the code it checks.
- **R5 — No `with:` on hassfest.** `home-assistant/actions/hassfest` is a composite action
  that declares **no inputs**; it runs the hassfest container over `$GITHUB_WORKSPACE` and
  scans `custom_components/*`. A `path:` input is answered with an "Unexpected input(s)"
  warning and otherwise ignored, so it reads as configuration while doing nothing.
- **R6 — Determinism.** Nothing here asserts on wall-clock time, and nothing new should. A
  shared two-vCPU runner will eventually lose any race a test bets on. Where a timing
  assertion is genuinely the property under test, the only sound form is a *ratio* — a bound
  expressed as a fraction of the delay it must not wait for, with both ends scalable
  together from the environment. Needing none of that here is the better outcome.

---

## Part 2 — HACS validation is a separate question

`hacs.yml` does not ask "is this integration correct?" — `hassfest`, pytest and the panel
tests ask that. It asks "can HACS actually install it?", and it fails for reasons that have
nothing to do with the code. It runs weekly on a schedule because HACS tightens its
requirements over time and a repository people install from should find that out first.

Eight checks, all waivable through `ignore`:

| Check | Status here | What satisfies it |
| --- | --- | --- |
| `hacsjson` | passes | `hacs.json` exists at the repo root |
| `information` | passes | `README.md` exists |
| `archived` | passes | the repository is not archived |
| `description` | **repo setting** | a description on the GitHub repository |
| `topics` | **repo setting** | at least one topic on the GitHub repository |
| `issues` | **repo setting** | the issue tracker enabled |
| `images` | **ignored** | an image embedded in `README.md` — there is none yet |
| `brands` | **ignored** | brand assets merged into `home-assistant/brands` |

- **R7 — Only waive what this repository cannot fix.** `brands` needs a merged PR in another
  repository and is a requirement for *default*-listing, not for the custom-repository
  install this is distributed as. `images` needs a screenshot that does not exist yet. Those
  two are ignored. The other six stay fatal.
- **R8 — Three things must be set on the GitHub repository itself**, and no commit here can
  do it: a **description**, at least one **topic**, and the **issue tracker** enabled.
  Until then `hacs.yml` is correctly red — HACS would reject the repository for the same
  reasons.
- **R9 — Remove the `images` ignore when a screenshot lands.** It is a debt with a specific
  discharge condition, not a permanent exemption. A sidebar-panel integration with no
  picture in its README is worse documentation than the ignore is bad CI.
- **R10 — HACS cannot install from a private repository.** The action itself passes either
  way, because it authenticates as the repository it runs in — so a green tick on a private
  repository does not mean a stranger can install anything. If this repository is ever taken
  private again, that is the consequence, and no check here will report it.

### Open inconsistency, not fixed here

`hacs.json` declares `"homeassistant": "2024.7.0"` — the minimum HA version HACS will offer
this integration to — while the test suite requires 2026.6.0. One of the two numbers is
wrong, and which one is a product decision rather than a CI fix: either the integration
genuinely works on 2024.7 (in which case the test floor is merely convenient and should say
so) or it does not, and `hacs.json` is inviting installs onto versions nobody has tested.
Nothing enforces the claim today, in either direction.

---

## Part 3 — IF a test ever needs a running daemon

Not required today, and the current suite covers everything reachable without one. What it
cannot cover is the wire: a real SSE stream, a real enrolment round-trip, the daemon's
actual JSON against this client's parser.

- **SH1** Mark it `@pytest.mark.integration` — the marker already exists in `pytest.ini` for
  exactly this — and make the hosted job select against it: `pytest -m "not integration"`.
  That has to happen in the same commit that adds the first such test, or the hosted runner
  starts trying to reach a daemon that is not there.
- **SH2** Run it from a **separate workflow** on a self-hosted runner that can reach a
  daemon, triggered by `workflow_dispatch` and pushes to `main` only — never from a fork
  PR, which would be arbitrary code execution on the machine wired to the door hardware.
- **SH3** Not a required status check. A daemon that is being reflashed must not block a
  merge.
- **SH4** `concurrency: group: hil, cancel-in-progress: false`. One daemon, one job.
- **SH5** The daemon's host and token come from repository secrets or the runner's
  filesystem, never the repository. A self-hosted runner's workspace persists between runs,
  so anything written there must be cleaned up in a step that runs on failure too.
- **SH6** The daemon under test drives a test relay or an LED, never a real lock, and its
  user database is expendable — enrolment tests create and delete templates.
- **SH7** Probe first and fail with `daemon not reachable at <host>` before any test runs, so
  an absent daemon can never be reported as a client regression.

---

## Appendix — what was actually wrong, and what was done

Diagnosed by reading the workflows against the tree and against upstream metadata; the run
logs were not reachable at the time.

1. **The pytest job could not install its dependencies.** Matrix pinned Python 3.13;
   `homeassistant>=2026.6.0` requires 3.14.2+. No candidate, no tests, red run — a
   guaranteed failure, not an intermittent one, and unrelated to any code in this repo.
   **Fixed:** matrix moved to 3.14, with the coupling written down in both files (R2).
   Confirmed by running the full suite on Python 3.14.5 with HA 2026.6.4: 119 passed.
2. **hassfest was passed an input it does not have.** `path:` on a composite action with no
   declared inputs — an "Unexpected input(s) 'path'" warning on every run, and no effect
   whatsoever. Harmless to the result, but it read as configuration. **Fixed:** removed,
   with the reason recorded so nobody adds it back (R5).
3. **The HACS job was gated on checks this repository cannot satisfy.** `brands` needs a
   merged PR in `home-assistant/brands`. **Fixed:** `brands` and `images` waived with
   explicit reasons and, for `images`, a removal condition (R7, R9). The three repository
   settings that remain outstanding are listed in R8 — those are the user's to set, and CI
   is right to stay red until they are.
4. **Coverage was uploaded only on success.** The failing run is the one whose coverage you
   want. **Fixed:** `if: always()` plus `if-no-files-found: ignore`.
5. **pip had no cache**, so every run reinstalled Home Assistant's whole dependency tree.
   **Fixed:** `cache: pip` keyed on `requirements_test.txt`.

The panel job was already correct: node 20, no dependencies, 60 checks passing locally.
