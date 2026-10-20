# Social missions and Telegram /copy

These features require the updated controller. Interactive phone work still requires the Phone
bridge to show connected; a Tailscale/ADB connection alone does not register the interactive tools.
Use a saved owner Cowork chat. The agent has Facebook, Instagram and TikTok app shortcuts.

## Set the account voice

In Telegram, send:

```
/persona We represent [your account/brand]. Our tone is [your tone]. Our niche is [niche].
Use Turkish when interacting with Turkish content. Our known facts are [facts].
Do not invent personal experiences or affiliations.
```

In Cowork, ask it to save those details with `social_persona`. The brief persists per project,
across chats and controller restarts. A persona defines voice and facts, not authorization.

## Give a bounded mission

For example:

> Use my [exact account] to find Turkish ceramics content. Search in Turkish, watch relevant
> posts, and use your discretion to follow relevant creators and join relevant Facebook
> ceramics groups after checking their rules. Reply only where you understand the actual
> post and have something useful to add. Use my saved persona. Complete one session and
> report what you actually did.

The agent can choose targets within an explicitly delegated mission. It samples the actual
post before replying; if understanding depends on speech, it needs the actual video link
to extract frames/transcript, otherwise it skips the reply. It reviews the exact response
against the mission, persona, observed post and current screen. Group joins require visible
group identity/rules and truthful membership answers. Missing owner facts need an answer.

The current session permits at most five externally visible reviewed actions. Reading and
navigation are separate. The same action is recorded before submission and cannot be
blindly retried. An uncertain result must be checked on the phone. Three screen reads without
an intervening action are allowed; a fourth stops the task. A third identical tap on an
unchanged screen also stops it. Phone actions run serially under the same phone lease as the
scheduled publisher, preventing concurrent chats/scheduled jobs from operating the phone.
Once a task takes the phone, Cowork does not start a second quality-review run or an
automatic continuation task; it returns its observed results. A new owner request starts
a new session.

This is interactive group/reply support. Facebook scheduled video publishing remains unsupported.
The system does not guarantee regional reach, a particular recommendation feed, or human
indistinguishability. Relevant browsing can influence recommendations; no fake biography or
fabricated expertise is part of the persona.

## Set up /copy

Connect Higgsfield in the owner's web Cowork chat with `/connect higgsfield`.
Then send the Telegram bot:

1. A reference character image with caption `/copy character <your character/cast description>`.
2. A background image with caption `/copy background <your setting description>`.
3. `/copy settings` to inspect the saved choices.

Text-only descriptions also work. A new character/background setting replaces that setting
for future requests. Each request snapshots its configuration and copies uploaded source
bytes to a unique path so later uploads cannot alter an in-flight job.

Use any of:

- `/copy https://www.instagram.com/reel/...`
- `/copy https://www.tiktok.com/@.../video/...`
- Upload a video with caption `/copy`.
- Reply `/copy` to a video.
- Send `/copy`, then send the link/video in the next message. `/copy cancel` cancels waiting.

Telegram bot downloads are limited to 20 MB; send a source link for larger videos. Reference
photos are decoded and normalized. Commands work only in the paired owner's private chat.
Output attachments must fit the bot's 49 MB delivery cap; larger results need a smaller
delivery copy or their actual provider URL.

One request runs a fixed source-analysis stage and saves a storyboard, then uses the connected
Higgsfield tools to produce an original adaptation. It carries over useful pacing and camera
structure, with new dialogue/staging and the owner's characters/backgrounds. It does not copy
the source person's identity, voice, music, branding or exact dialogue.

Generation uses the provider's current discovered schemas and may consume Higgsfield credits.
Provider-issued upload/download URLs can transfer saved reference images and generated clips.
There are at most 24 production-agent turns and one output-inspection pass, with no helpers,
automatic continuation or source re-analysis tools. Identical provider submissions are journaled
before sending and not retried. Status polling is bounded. A controller restart after generation
begins leaves receipts for reconciliation instead of risking duplicate paid work.

The bot returns the real generated file or provider URL, or the specific blocker with the saved
storyboard. It never automatically publishes the adaptation. Provider model capabilities,
source-download access and image/voice consistency still need live validation on your account.

## Validation scope

Offline tests cover persona persistence, rejection without content/audio evidence, unrelated
reply rejection, current-target checks, duplicate fences, session limits, Telegram pairing,
command routing, source retention, one-time source analysis and provider request journaling.
They mock model/provider/phone behavior and cannot prove semantic accuracy on real videos.
