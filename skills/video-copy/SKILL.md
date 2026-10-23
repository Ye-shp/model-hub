---
name: Reference video recreation
description: Recreate an Instagram/TikTok reference in Higgsfield with the owner's saved character sheet and background.
---
The source and the owner's references have already been analyzed and the storyboard is complete. Produce that
storyboard. Discover the connected Higgsfield tools and their actual schemas.

The owner's character and background are fixed: their description, written sheet (read it with read_file when a
path is given) and reference images define them. Use the storyboard's Continuity text in every generation prompt.
Condition each shot on the reference image(s) the storyboard names, using the provider's character/reference
features when it has them (for example a saved character or image reference), so the character looks the same in
every shot. If local images need uploading, use the provider's supported upload flow; never give a local path to an
API that expects a public URL. If an essential capability or reference is unavailable, report that exact blocker
with the saved storyboard.

Keep the source's pacing and camera ideas with new wording and staging. Do not copy the source person's identity,
voice, music, branding or exact dialogue. Do not promise pixel-identical reproduction.

Generate each required shot once. Save and reuse provider job IDs and results. Poll existing jobs;
never resubmit a generation whose result is uncertain. The provider-call journal prevents identical
requests from being repeated. Do not evade it by changing irrelevant arguments.
Assemble returned clips when needed using assemble_video. Return the real playable provider URL
or share_file for a locally available output. Never report a queued job as a completed video.
Telegram delivery files must be at most 49 MB; compress a delivery copy or return the actual
provider video URL for a larger result.

Use inspect_output at most once after generation. It returns a review of the output against the storyboard and the
owner's references. When it says fix, make that one correction and finish; otherwise finish. Do not analyze the
reference again, conduct new research, rewrite the storyboard repeatedly, seek another agent's critique, or continue
into another task. Report output, the review's findings, remaining limitations and provider job IDs concisely.
Never publish.
