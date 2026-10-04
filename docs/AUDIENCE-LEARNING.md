# Learning from audience results

The console's **Audience** page connects a creative brief and its drafts to the
results of published posts. Cowork can read those results when choosing its next
strategy. The page records experiments, publication, delayed observations and
preference examples. It does not publish posts, run GPU training or replace the
production model.

## A first experiment

1. Open **Audience**, choose the project, and create an experiment. Record the
   brief, the change you are testing, platform, account ID, format and posting
   context. For automatic collection, the account must match the connected
   analytics account: TikTok's account ID (`open_id`) or Instagram's user ID.
2. Select **Learning** for examples you may later train on, or **Evaluation only**
   for an independent test. Keep evaluation experiments out of training and avoid
   choosing their label after seeing the outcome.
3. Attach each draft's actual text or script with a label such as A and B. You can
   link the saved post draft ID and declare its model and strategy. Model names
   are declarations, not proof of the model that produced the draft. Save the
   actual version used in the post; later edits should be recorded as a new draft.
4. Publish through the existing approval flow or directly through the platform.
   On the Audience page, record the published post ID, HTTPS link, publication
   time, account and organic, paid, mixed or unknown exposure. Saving this record does not send
   anything to the platform.
5. Return to the collection schedule for 24-hour, 72-hour and seven-day
   observations, attempts and errors. The
   page refreshes when no form is open; **Refresh** checks immediately. You can
   enter counts manually with the time you observed them.
6. Review reach score, sample support and supporting signals. Export eligible
   preference examples when the drafts have sufficient comparable observations.

An experiment compares drafts with the same platform, account, format and
posting context. Use a specific context: audience, topic, offer, organic
distribution and a similar posting window. A paid boost, changed thumbnail,
different distribution or a much larger follower base can explain a result
without the script being better. Those comparisons need a separate experiment
or should be treated as inconclusive. No experiment here randomizes viewers, so
a higher score does not establish cause and effect or guarantee factual quality.

If an organic post is boosted after its publication is recorded, use **Exclude
from organic comparisons** on that draft and select paid, mixed or unknown.
The owner publication tool can make the same update using the existing post's
identity. The system keeps an audit and the original observations. Exclusion is
one-way: a draft classified as paid, mixed or unknown cannot be relabeled organic.
The published post ID, link, publication time and account remain fixed. Excluded
drafts stay visible but do not enter organic baselines or preference comparisons.

## Counts and collection

Observations are timestamped snapshots of the cumulative counts the platform
reports. They are not the number of new views earned during a checkpoint.
Observation age is measured from the publication time, allowing comparisons at
similar post ages instead of comparing one-hour and three-day totals.

Leave unavailable metrics **blank**. Blank is saved as missing, while zero is a
reported zero. Views, likes, comments, shares, saves, reach, average watch seconds,
completion rate, followers and conversions may be recorded manually. Completion
rate is entered as a percentage in the console and stored as a fraction between
zero and one. Followers and conversions need a consistent definition across
drafts; they are supporting records, not automatically attributed outcomes.

Automatic collection currently uses first-party APIs for TikTok and Instagram.
It depends on usable credentials, the correct account, ownership of the post and
analytics permissions. A connected posting account is not a guarantee of insight
permissions. The collector shows missing credentials, expired tokens, permission
failures, unavailable metrics and rate limits as errors or warnings. It preserves
available counts when a different metric is unsupported.

TikTok access tokens expire after 24 hours according to [TikTok's token management
documentation](https://developers.tiktok.com/docs/en/oauth-user-access-token-management).
Connecting only an access token requires manual renewal; it is not enough for
unattended 72-hour and seven-day checkpoints. Automatic renewal also needs the
refresh token and the app's client key and client secret. Refresh credentials
must remain valid and retain the required permissions. A revoked or expired
refresh token still requires reconnecting the account, and missed checkpoints
cannot be reconstructed by renewing a token later.

Connect from an owner Cowork chat using credentials from the authorized TikTok
app with `video.list` and `user.info.basic` permissions:

```text
/connect tiktok <OAuth open_id> <access token> refresh_token=<refresh token> client_key=<client key> client_secret=<client secret>
```

Supply all three refresh fields together. The account ID is OAuth `open_id`, not
the account's username. The shorter `/connect tiktok <OAuth open_id> <access token>`
form uses manual renewal. Reconnecting with that shorter form also removes any
previously saved refresh configuration. Connection commands are handled outside
the model; do not copy their credentials into experiment descriptions or exports.
With valid refresh configuration, the collector renews access when needed and
saves the returned access token, refresh token and expiry. A failed renewal is
reported in the collection schedule rather than treated as an empty observation.

Use the numeric published video ID for TikTok or media ID for Instagram. An
Instagram permalink's shortcode is not its numeric media ID. The link must be a
direct TikTok video or Instagram post/reel permalink on that platform; the
publication record cannot later be reassigned to a different post. Exposure may
be updated to exclude a previously organic post as described above.

TikTok's Display API supplies views, likes, comments and shares for the connected
owner's videos. It does not supply saves, unique reach or watch-time metrics in
this integration. Instagram availability varies by media type and permissions.
Missing values remain missing. This version supports TikTok and Instagram
experiments only.

The worker runs independently of a Cowork chat. Checkpoints, retries and leases
are stored in the controller database, so a chat timeout does not stop the queue
and a worker restart does not erase pending checks. An interrupted worker's lease
expires and another attempt can claim the checkpoint. The container still has to
be running for collection to happen, and its data must be retained. A late restart
cannot reconstruct the exact counts a platform showed while the box was offline.
Checkpoints missed by more than the larger of two hours or 20% of their scheduled
age are marked missed. The worker does not label today's counts as an earlier
observation.

## What the first score means

`audience-v1` is **reach-focused**. Its score is
`log((views + 1) / (baseline views + 1))` at a comparable post age. Its historical baseline uses similar organic
learning posts from the same account, platform, format and posting context. Both
their publications and the observations used in the baseline must predate the
first publication in the current experiment. When
history is insufficient, the result can use the experiment median, including
the current draft, and
labels that fallback. Compare like with like; do not interpret scores from
different accounts or contexts as a global ranking.

Shares and completed viewing are diagnostics. Other recorded metrics are retained
for future objectives; this version does not claim a calibrated combination of
watch time, sales and engagement. A reach objective can prefer a widely seen post
even when another draft has better retention or conversions. Review those signals
against the experiment's brief before using a pair.

Sample support (the API's `confidence` field) is `views / (views + 500)`. It is a sample-size indicator, not a
statistical confidence interval or the probability that a draft is better. At
500 views it is 50%; at 2,000 views it is 80%. There is no automatic bandit or
traffic allocation. Cowork reads performance and proposes strategies; the owner
still decides what to publish.

## Preference exports and Soup

The export requires a shared observation horizon, minimum views on each draft and
a minimum score margin. Choose 24, 72 or 168 hours; defaults are 72 hours, at least
500 views and a 0.15 margin in log reach units. A selected observation must be
within the larger of two hours or 20% of the target age. The two compared
observations must also differ in age by no more than the larger of two hours or
10% of that horizon.

Select **Prepare learning export**, then use **Download preference examples**
and **Download comparison audit** to save the two files. Preparing an export
does not automatically save a file or start training.

Each eligible comparison produces a JSONL row:

```json
{"prompt":"The creative brief", "chosen":"The preferred draft", "rejected":"The other draft"}
```

The export uses only eligible **Learning** experiments and comparable organic
drafts. Within an experiment, the comparison uses a common baseline, so the
score margin is the log ratio of views plus one. The export chooses the highest
and lowest eligible view totals in each experiment. Ineligible comparisons are returned with
their reasons instead of becoming training examples. Review the pair against
the brief: audience reach is evidence, not an independent factual-quality judge.
The visible **Download comparison audit** link saves a separate portable JSON
record. It contains the experiment context, chosen and rejected evidence,
observed counts and their sources/times, publication links, declared models and
media fingerprints, skipped reasons, export time and a digest of the exported
dataset records. Download it alongside the JSONL so a later training run can
trace which measured outcomes produced each pair; exporting it does not start
training. The audit is a record of the evidence, not a guarantee of model lineage
or content quality.
Include the audience, topic and offer in the brief when they are needed to
understand the exported prompt; other experiment fields are stored separately.

Soup can consume this `prompt`/`chosen`/`rejected` shape as a preference dataset.
Exporting supplies data; it does not install Soup or trigger its learning loop.
A later training worker needs a compatible source model, a fixed dataset version,
GPU capacity, held-out evaluation and a separate deployment decision. The current
Qwen GGUF is an inference artifact; this page does not modify its weights.

For knowledge collected from TikToks, Reels or memory files, continue to use the
existing source-linked knowledge base. These audience records add evidence about
which of **your published drafts** performed well. They do not turn watched
videos or unverified model-written memories into trusted training data.

## Owner API

These console endpoints use the existing owner authentication and project scope:

| Endpoint | Purpose |
|---|---|
| `GET /api/audience/experiments?project=default` | List experiments |
| `POST /api/audience/experiments` | Create an experiment |
| `GET /api/audience/experiments/{id}?project=default` | Drafts, observations and checkpoint schedule |
| `POST /api/audience/variants` | Attach draft text and declared lineage |
| `POST /api/audience/variants/{id}/publication` | Record an already published post |
| `POST /api/audience/variants/{id}/metrics` | Record timestamped manual counts |
| `GET /api/audience/performance?project=default` | Read scored results |
| `GET /api/audience/preferences?project=default&horizon_hours=72&min_views=500&min_margin=0.15` | Preference records, comparisons and skipped reasons |

Write bodies include `project`. Times use ISO 8601 with a timezone; the console
converts local date inputs to UTC. Manual metrics use JSON numbers or `null`,
never empty strings. Do not put credentials in experiment text or API URLs.
