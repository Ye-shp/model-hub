# Direct phone control and Telegram /recreate

These features require the updated controller. Interactive phone work still requires the Phone
bridge to show connected; a Tailscale/ADB connection alone does not register the interactive tools.
Use a saved owner Cowork chat. The agent has Facebook, Instagram and TikTok app shortcuts.
It can also open other installed apps by Android package name. It observes the screen,
taps, long-presses, swipes, types, reads copied text, switches apps and transfers local
media to the gallery. It can publish through the app's own composer when your task
authorizes that post. These direct controls do not require scheduling or a numbered draft.

After updating the controller, download the new bridge.py from the console's Phone page
and restart that bridge process. On the computer running it, install Unicode/clipboard
support once with `python -m pip install uiautomator2==3.7.0`. Bridge 1.2 supports Turkish
characters/emoji through a verified clipboard paste, plus reading a copied post link.
It needs an authorized ADB connection, over USB or your existing private-network connection;
use `--serial <the exact serial shown by adb devices>` to select your phone.

## Set the account voice

In Telegram, send:

```
/persona We represent [your account/brand]. Our tone is [your tone]. Our niche is [niche].
Use Turkish when interacting with Turkish content. Our known facts are [facts].
Do not invent personal experiences or affiliations.
```

In Cowork, ask it to save those details with `social_persona`. The brief persists per project,
across chats and controller restarts. A persona defines voice and facts, not authorization.

## Give it a phone task

For example:

> Use my [exact account] to find Turkish ceramics content. Search in Turkish, watch relevant
> posts, and use your discretion to follow relevant creators and join relevant Facebook
> ceramics groups after checking their rules. Reply only where you understand the actual
> post and have something useful to add. Use my saved persona. Complete one session and
> report what you actually did.

The agent can choose targets within an explicitly delegated mission. It samples the actual
post before replying; if understanding depends on speech, it needs the actual video link
to extract frames/transcript, otherwise it skips the reply. It can obtain the actual link
itself using the app's Share/Copy link control and phone_clipboard. It reviews the exact response
against the mission, persona, observed post and current screen. Group joins require visible
group identity/rules and truthful membership answers. Missing owner facts need an answer.

The agent works through the task within its existing time/model-step budget; there is no
fixed five-action cutoff. Reviews are internal checks, not owner prompts for every tap.
The same action is recorded before submission and cannot be
blindly retried. An uncertain result must be checked on the phone. Three screen reads without
an intervening action are allowed; a fourth stops the task. A third identical tap on an
unchanged screen also stops it. Phone actions run serially under the same phone lease as the
scheduled publisher, preventing concurrent chats/scheduled jobs from operating the phone.
Once a task takes the phone, Cowork does not start a second quality-review run or an
automatic continuation task; it returns its observed results. A new owner request starts
a new session.

Facebook uses the same direct screen controls, including its post composer. This change
does not add a Facebook driver to the separate scheduled-publishing system. Real app screens
and the signed-in account still need live acceptance testing.
The system does not guarantee regional reach, a particular recommendation feed, or human
indistinguishability. Relevant browsing can influence recommendations; no fake biography or
fabricated expertise is part of the persona.

## Set up /recreate

`/recreate` recreates a reference video in Higgsfield with your own character and background. `/copy` is the same
command under its old name.

Connect Higgsfield in the owner's web Cowork chat with `/connect higgsfield`. Then save your references in Telegram:

- **Character images.** Send one photo or an album (turnaround sheet, angles, close-ups) with the caption
  `/recreate character <description>`. The other photos of the album join the same set as they arrive. A new
  captioned photo starts a new set; `/recreate character add` adds photos to the current one (up to 8).
  Photos sent as files work too.
- **Written character sheet.** `/recreate character sheet <name>` uses the newest sheet made with `/frame <video> <name>`
  in any of your chats. Or send a `.md`/`.txt` file captioned `/recreate character sheet`.
- **Background.** The same commands with `background`.
- `/recreate settings` shows what's saved; `/recreate clear character` (or `background`) removes one.

Each kind needs at least a description, a sheet or an image. Each request snapshots the references into the chat
folder, so later changes can't alter a job in progress.

Send the video any of these ways:

- `/recreate https://www.instagram.com/reel/...`
- `/recreate https://www.tiktok.com/@.../video/...`
- Upload a video with caption `/recreate`.
- Reply `/recreate` to a video.
- Send `/recreate`, then the link or video in the next message. `/recreate cancel` cancels waiting.

Telegram bot downloads are limited to 20 MB; send a source link for larger videos. Output attachments must fit the
bot's 49 MB delivery cap; larger results need a smaller delivery copy or their actual provider URL.

### Who does what

1. GLM's vision model reads the source frames and your reference images once.
2. The large GLM model writes the storyboard: a Continuity block (your fixed character and setting, pasted into every
   prompt), then per shot the duration, framing, action, new dialogue, which of your reference images to condition on,
   and the full Higgsfield prompt.
3. A Qwen agent runs Higgsfield with the provider's current discovered schemas: at most 24 turns, no helpers,
   continuation or re-analysis.
4. GLM checks the generated video once against the storyboard and your references and says deliver or fix; the agent
   makes at most that one fix.

Without GLM connected (or over its monthly budget), Qwen does steps 1, 2 and 4.

The recreation keeps the source's pacing and camera structure, with new dialogue and staging. It does not copy the
source person's identity, voice, music, branding or exact dialogue. Generation may consume Higgsfield credits.
Identical provider submissions are journaled before sending and not retried; status polling is bounded. A controller
restart after generation begins leaves receipts for reconciliation instead of risking duplicate paid work.

The bot returns the real generated file or provider URL, or the specific blocker with the saved storyboard. It never
publishes. Character consistency across shots depends on the provider's reference features and still needs live
validation on your account.

## Validation scope

Offline tests cover persona persistence, rejection without content/audio evidence, unrelated
reply rejection, current-target checks, duplicate fences, session limits, Telegram pairing,
command routing, source retention, one-time source analysis, provider request journaling, album and sheet handling, and the single output review.
They mock model/provider/phone behavior and cannot prove semantic accuracy on real videos.
