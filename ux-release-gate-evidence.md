# UX release blocker evidence for Hermes

Snapshot taken November 18, 2026. OpenClaw starts at the July 4 ClawSweeper rubric merge. Hermes starts at midnight October 1 in Bangkok (September 30 at 17:00 UTC).

## OpenClaw results

| Measure | Count |
| --- | ---: |
| Published version tags after rollout | 69: 42 stable, 27 preview |
| Reports filed after rollout currently labeled both P0 and UX release blocker | 1,839: 254 open, 1,585 closed |
| Closed reports with a GitHub-linked PR that merged in the window | 340 reports, 334 distinct PRs |
| Linked PRs whose exact merge commits are present in a published stable tag | 308 |
| Linked PRs whose exact merge commits first appeared in preview and later in stable | 185 PRs, linked to 183 reports |
| Reports in that preview-first cohort with both ClawSweeper labels applied before an associated fix merged | 182 |
| Hand-verified preview blocker cases labeled before repair, with the fix or backport present in a later stable tag | **4 distinct problems** |

The rubric merged at 04:07:44 UTC on July 4. The first reference case has public bot events ten minutes later. Its fix later shipped in stable, but the report came from an already released build. That makes it a good small-fix example and a separate case from the four preview catches. [Rubric PR](https://github.com/openclaw/clawsweeper/pull/387) · [First bot-labeled report](https://github.com/openclaw/openclaw/issues/98674) · [Fix](https://github.com/openclaw/openclaw/pull/112507) · [Published releases](https://github.com/openclaw/openclaw/releases)

| Preview problem | Verified repair in stable |
| --- | --- |
| [#109867](https://github.com/openclaw/openclaw/issues/109867). A state migration prevents the gateway from starting on beta.2. | [Migration repair](https://github.com/openclaw/openclaw/pull/109876) and [related SQLite fix](https://github.com/openclaw/openclaw/pull/111167) reached [v2026.8.1](https://github.com/openclaw/openclaw/compare/25a8b33ff50f34189473333ef24872380e718f95...ea806575e6450e4d1efdfc72c19f04be982a1b9b). One problem, two PRs. |
| [#119770](https://github.com/openclaw/openclaw/issues/119770). The beta.7 macOS app writes an obsolete key and blocks onboarding. | [ClawSweeper called for a release-owner decision](https://github.com/openclaw/openclaw/issues/119770#issuecomment-5199056708). [The fix](https://github.com/openclaw/openclaw/pull/119831) reached [v2026.8.1](https://github.com/openclaw/openclaw/compare/355c107c093767cb14c1463b8e3a7dd9764a7359...ea806575e6450e4d1efdfc72c19f04be982a1b9b). |
| [#165846](https://github.com/openclaw/openclaw/issues/165846). The core Control UI returns HTTP 503 on beta.1 under an arbitrary UID. | [Main fix](https://github.com/openclaw/openclaw/pull/165880) and [release backport](https://github.com/openclaw/openclaw/pull/165937) reached [v2026.10.1](https://github.com/openclaw/openclaw/compare/de60aefc3f7cc196e5285cd6b3424a7b1520016f...e178c39e9aae3fa440735217f9aef9574f286677). |
| [#165850](https://github.com/openclaw/openclaw/issues/165850). Doctor repeatedly rejects legacy session state on beta.1. | [The fix PR](https://github.com/openclaw/openclaw/pull/165866) records that ClawSweeper requested proof with the published updater. [The release cherry-pick](https://github.com/openclaw/openclaw/compare/c4b0d65fbd2ebcac56a73e051671c61c2467b801...e178c39e9aae3fa440735217f9aef9574f286677) reached v2026.10.1. |

**Why the release gate matters.** In [#120633](https://github.com/openclaw/openclaw/issues/120633#issuecomment-5487074985), the reporter confirmed that a preview message-dispatch failure still reproduced in v2026.8.1 stable. In [#183056](https://github.com/openclaw/openclaw/issues/183056), a Control UI reload failed on a preview and [the fix merged to main](https://github.com/openclaw/openclaw/pull/183148). The [v2026.10.10 stable source](https://github.com/openclaw/openclaw/blob/v2026.10.10/src/gateway/control-ui-session-entry-path.ts) still lacked the fix's navigation hints. A release can ship different code from main.

**Method.** Enumerated current dual-label issues through all 19 REST pages. Verified merged closing links through GitHub GraphQL. Reconciled each GitHub release to its peeled Git tag and checked exact merge-commit ancestry and publication order. Took both labels’ bot-event timestamps from the timeline of all 183 reports in the preview-first cohort. The tag CSV also records the moving `linux-stable` release and seven Git-only refs observed under July–November version prefixes. Those entries are outside the 69 published version releases. Exact ancestry misses fixes that were cherry-picked or implemented independently. The four case checks include explicit backports where appropriate. The public record supports timed triage and release inclusion. It does not yield a count of formal release holds attributable to the rubric.

## Hermes first wave since October 1

| Measure | Conservative count |
| --- | ---: |
| Newly filed, still-open problems with report evidence of a blocked supported journey and no normal recovery | **9** |
| Earlier open report with a fresh stable repro in the window | **1** |
| Qualifying problem clusters with fixes merged in the window | **7**, across **8 PRs** |
| Additional merged cases with ambiguous P0 recovery evidence | **2** |

The first-wave queue therefore has **10 open problems to check**, including the earlier macOS case. The team reproduced that carry-in on stable v0.23.2. The new reports have qualifying evidence in GitHub and still need exact release-candidate confirmation before they hold a release. [Hermes proposal](https://github.com/NousResearch/hermes-agent/issues/157333) · [Draft gate PR](https://github.com/NousResearch/hermes-agent/pull/157340) · [Local stable repro](https://github.com/NousResearch/hermes-agent/issues/123737#issuecomment-6974336662)

| Open problem | Evidence and fix status |
| --- | --- |
| macOS CLI update strands installed Desktop | September carry-in [#123737](https://github.com/NousResearch/hermes-agent/issues/123737). About says current and offers only an installer. The normal-looking UI has no finish action. [Fix PR #128465](https://github.com/NousResearch/hermes-agent/pull/128465) remains open. |
| Windows fixture copy blocks install or leaves update incomplete | [#150277](https://github.com/NousResearch/hermes-agent/issues/150277) and [#156983](https://github.com/NousResearch/hermes-agent/issues/156983). The installer needed an OS setting change. The updater’s normal retry failed. Grouped conservatively while the exact updater mechanism is unresolved. |
| Windows Desktop update never relaunches | [#156217](https://github.com/NousResearch/hermes-agent/issues/156217). Recovery required killing processes and handing off through the CLI. [Fix PR #156237](https://github.com/NousResearch/hermes-agent/pull/156237) is open. |
| Interrupted Windows update leaves an empty environment | [#149520](https://github.com/NousResearch/hermes-agent/issues/149520). Desktop boot loops until the environment and pending markers are repaired by hand. |
| Telegram goes silent after Windows reboot | [#157025](https://github.com/NousResearch/hermes-agent/issues/157025). The gateway parks Telegram until the user deletes a stale lock and restarts it. |
| First Windows install fails under a non-ASCII profile path | [#151581](https://github.com/NousResearch/hermes-agent/issues/151581) and [#151600](https://github.com/NousResearch/hermes-agent/issues/151600). Recovery required forcing UTF-8 or using an ASCII-only location. [Fix PR #148568](https://github.com/NousResearch/hermes-agent/pull/148568) is open. |
| Slow version probe sends an existing Desktop install into setup | The P0 basis is [#155862](https://github.com/NousResearch/hermes-agent/issues/155862), where recovery needs a Git fetch or a configuration change. Related [#148498](https://github.com/NousResearch/hermes-agent/issues/148498) can sometimes recover on relaunch. [Fix PR #150540](https://github.com/NousResearch/hermes-agent/pull/150540) is open. |
| Windows update leaves its gateway on a blocked interpreter | [#148950](https://github.com/NousResearch/hermes-agent/issues/148950). The reporter manually repaired launcher and interpreter selection. |
| Subagent routing strands a returning Desktop Bot Chat | [#154994](https://github.com/NousResearch/hermes-agent/issues/154994). Restart did not recover the chat. The reporter repaired routing state manually. [Fix PR #155020](https://github.com/NousResearch/hermes-agent/pull/155020) is open. |
| Windows logon task claims success without starting the gateway | [#136390](https://github.com/NousResearch/hermes-agent/issues/136390). After logon, messaging stays down until the user installs a Startup-folder script. [Fix PR #136397](https://github.com/NousResearch/hermes-agent/pull/136397) is open. |

| Qualifying merged problem | Merge evidence |
| --- | --- |
| Profile OAuth reports success while dropping the credential | [#134574](https://github.com/NousResearch/hermes-agent/pull/134574) |
| A valid Anthropic Console key is handled as OAuth, blocking first reply | [#137140](https://github.com/NousResearch/hermes-agent/pull/137140) |
| CLI re-login advice invokes a command the provider cannot handle | [#151957](https://github.com/NousResearch/hermes-agent/pull/151957) |
| The Windows installer reuses a broken cached Git helper forever | [#145592](https://github.com/NousResearch/hermes-agent/pull/145592) |
| Packaged macOS update downloads and repeatedly fails to install | [#148192](https://github.com/NousResearch/hermes-agent/pull/148192). Merge verified. A native ShipIt install remained unverified in the PR. |
| Remote Desktop cannot create a new chat against its gateway | [#148219](https://github.com/NousResearch/hermes-agent/pull/148219) |
| Restart loses a turn and silently pauses subsequent messages | [#141357](https://github.com/NousResearch/hermes-agent/pull/141357) and [#145078](https://github.com/NousResearch/hermes-agent/pull/145078). One problem, two fixes. This is why the still-open [#139532](https://github.com/NousResearch/hermes-agent/issues/139532) is not counted again as wholly unresolved. |

The two merged candidates excluded from the seven are [#131809](https://github.com/NousResearch/hermes-agent/pull/131809), where the installer’s recovery guidance is unclear, and [#149623](https://github.com/NousResearch/hermes-agent/pull/149623), where gated Desktop history failed but the chat WebSocket already worked on main.

**Method.** Queried new issues and in-window PR merges separately using 12 title terms covering installation, setup, authentication, login, providers, response, messaging, update, and sessions. The first 100 results per query yielded 1,470 distinct records. The high-signal title pass picked 408. Detailed verification read 33 issue bodies and 24 product PR bodies, then checked duplicates, linked PRs, merge dates, and recovery. The 7 count is based on date of merge and can include older reports. It establishes merged fixes, while release-tag inclusion and current candidate behavior require separate checks. The query caps and title matching make the Hermes figures lower bounds.

## Suggested wording for the upstream discussion

> After OpenClaw introduced its UX release-blocker rubric, 185 linked fixes reached a published preview before stable. In 182 associated reports, ClawSweeper had applied both blocker labels before a linked fix merged. We verified four individual preview blockers where the bot flagged the problem before repair and the fix reached a later stable tag. We also verified a preview failure that escaped into stable. A release gate should therefore check the actual candidate and require a usable recovery path.
>
> Since October 1, our first Hermes screen found nine newly filed open UX blocker reports after deduplication, one older problem reproduced again on current stable, and seven qualifying problem clusters with merged fixes. Start with the already-reproduced macOS Desktop update, then verify Windows install and update, and a Telegram reboot recovery, on the candidate.

Record the affected stable or preview build, detection date, fix PR, exact candidate proof, and promotion decision for each pilot case. That will make future claims about a blocker holding or clearing a release auditable.
