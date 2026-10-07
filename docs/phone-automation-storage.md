# Phone automation: install and persistent storage

Phone posting uses the controller's direct ADB connection through your existing Tailscale or SSH tunnel. Configure the owned account and its device through Cowork before enabling scheduled posting. An unconfigured installation does not contact a phone or publish anything. The Hub does not install a VPN daemon or run the scaffold's systemd/setup scripts.

## Keep the current instance and memories

Use the usual `/update-code <commit>` and restart the **same** instance. Keep its existing persistent volume and environment variables. Phone automation does not require replacing the instance, recycling its storage, moving `HUB_DATA_DIR`, or deleting a database.

The first explicit Cowork setup/configure action prepares missing controller dependencies in place. Python packages are installed into `store.DATA/automation/python`, leaving the existing agents environment intact. If `adb` is missing, setup installs only the controller ADB package through noninteractive, bounded package-manager calls. This preparation never executes a downloaded phone setup script. Later controller restarts reactivate the saved Python package directory.

Older code updaters omit `tools/automation` from their extraction whitelist. Explicit setup can repair that missing source folder using **the exact commit currently running**. It extracts only `tools/automation`; it does not fetch another branch, replace other running code, or change saved user data. A developer checkout without a commit directory must restore the matching source locally instead of fetching an assumed version.

Fresh images include ADB and the pinned Python dependencies already. A new image remains an optional deployment route; it is not required to preserve and extend an existing installation.

## Connect and use the phone in Cowork

The phone must already expose authorized ADB over the private connection. Supply its Tailscale IP and ADB port, or `localhost:<forwarded-port>` for an SSH tunnel reachable from the Hub controller. Creating and maintaining that private connection is separate from this integration. The Hub does not expose a public ADB listener or store SSH credentials.

Select **Phone posting**, or ask Cowork to configure phone automation. Provide the exact device address, Instagram or TikTok username, account timezone and country. Cowork checks runtime readiness, prepares missing dependencies explicitly, and retains this configuration across restarts. The native adapter verifies that the app shows the configured signed-in profile before transferring an approved video.

Cowork saves a numbered draft first. Review its video, caption, account and time, then approve that specific draft in a new message, for example `approve post 7 for 2026-10-08T12:00:00-04:00`. The queue supports one video per TikTok post or Instagram Reel. Its initial account policy is 08:00–22:00 local time, at least 20 minutes between posts, and at most five Instagram or eight TikTok posts per day. Conflicting or late times are reported for review, not silently changed.

App layouts vary. A verified composer gets one publication tap; ambiguous layouts remain ready for manual completion. The adapter does not invent a platform link or assume success from the home screen. Confirm actual publication with the draft number, direct platform URL and an ISO time with timezone in the owner's message. An uncertain or manually prepared result holds the phone until that confirmation, or explicit owner verification that nothing was posted and the native composer was discarded. This initial integration therefore requires owner reconciliation after submission.

## Storage boundaries

`store.DATA` keeps its existing value. The supervisor normally sets `HUB_DATA_DIR=/workspace/data/agent-workspace`; local development retains its existing `agents/data` default.

| Data | Location |
| --- | --- |
| Existing notes, embeddings, documents, drafts and chat jobs | Existing `store.DATA/hub.db` |
| New publication queue and device leases | Additive automation tables in that same database |
| Phone/account configuration | `store.DATA/automation/config.json` |
| In-place Python dependencies | `store.DATA/automation/python` |
| Phone evidence/screenshots | `store.DATA/automation/screenshots` |
| Retained source videos | Existing `store.DATA/posts/<post-id>/` |
| Cowork memory files and other chat files | Existing `COWORK_ROOT`, normally `/workspace/cowork` |
| Staged app code | Existing `DATA_DIR/hub-code/<commit>` |

Adding automation tables does not rewrite notes, change their IDs or thread scopes, recreate embeddings, or migrate old memory files. Config creation is separate from database memory. Code snapshot cleanup applies only to staged app code, not these persistent data directories.

The standalone scaffold's YAML examples and JSON state under `tools/automation` remain a separate CLI interface. The Cowork integration stores its live configuration and queue in the persistent locations above; it does not automatically import those examples or migrate prior CLI state.

## Dependency and offline checks

Controller pins are [PyYAML 6.0.3](https://pypi.org/project/PyYAML/6.0.3/), [tzdata 2026.4](https://pypi.org/project/tzdata/2026.4/), and [uiautomator2 3.7.0](https://pypi.org/project/uiautomator2/3.7.0/). They are included in `agents/requirements.txt` and the standalone requirements. `automation_runtime.ready()` checks availability without contacting a device; only explicit `ensure()` performs setup.

CI runs the existing Hub unittest suite plus the scaffold's pytest suite. For the latter alone, from the repository root:

```sh
python -m pip install -r tools/automation/requirements-test.txt
python -m pytest tools/automation/tests -q
```

Deployment regressions use temporary databases and mocked downloads/install commands to check that code staging and explicit setup preserve notes, embeddings, configuration, retained assets and Cowork memory files. These checks do not execute a real installation, phone connection, or publication.
