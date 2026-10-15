---
name: Frame extraction
description: Turn a video into a reusable character sheet, frame by frame (Telegram /frame, or the frame_sheet tool in Cowork).
---
Frame extraction runs as a fixed pipeline (agents/frames.py), not an agent. It grabs frames evenly across the video plus the
start of every shot, measures each frame's colour palette, has GLM's vision model read the frames in parallel (the resident
Qwen-VL when GLM isn't connected), and has the large GLM model merge everything into one character sheet: the canonical look,
every outfit with timestamps, accessories, expressions, voice, setting, lighting and camera, the measured palette, continuity,
extrapolated details marked "(inferred)", and a prompt kit for image and video models.

It saves sheet.md, a contact sheet, the frames and their notes under character-sheets/ in the chat's folder, shares them, and
adds the sheet to the knowledge base, so later tasks find it with search_knowledge. It describes what is visible; it does not
identify people or guess names, ethnicity or other sensitive traits.

In Cowork, use frame_sheet(source, name) when the user wants a video's look, character, outfits or shots captured for reuse;
analyze_video is for why a video performs (hook, beats, pacing).
