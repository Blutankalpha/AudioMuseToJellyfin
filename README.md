# AudioMuseToJellyfin
An attempt at vibe-coding (first time for everything) a script to convert Audiomuse data into Jellyfin data.

pulls analysis data (BPM/tempo and mood) from an AudioMuse-AI instance
(https://github.com/NeptuneHub/AudioMuse-AI) and writes it back into Jellyfin:

  - AudioMuse "tempo"       -> Jellyfin Tag, e.g. "BPM:128"   (Jellyfin has no native BPM field)
  - AudioMuse "mood_vector" -> Jellyfin Genres (top N moods, e.g. "energetic", "happy")

How matching works
-------------------
AudioMuse-AI's /external/get_score endpoint is keyed by "the calling server's own
track id" (see app_external.py / _resolve_external_id). Since this script is acting
as a Jellyfin client, we pass the Jellyfin ItemId as `id`. If your AudioMuse-AI
deployment has multiple music servers configured (v3.0.0+ multi-server support),
pass --audiomuse-server with the exact server name/id you set up in AudioMuse-AI's
Setup Wizard so the id resolves against the right server.

Requirements
------------
    pip install requests

Usage
-----
    python sync_audiomuse_to_jellyfin.py \
        --jellyfin-url http://jellyfin.local:8096 \
        --jellyfin-api-key XXXXXXXX \
        --jellyfin-user-id XXXXXXXX \
        --audiomuse-url http://audiomuse.local:8000 \
        --audiomuse-token YYYYYYYY \
        --dry-run

Remove --dry-run once you're happy with the planned changes.

Notes / assumptions (please double check against your setup)
--------------------------------------------------------------
* AudioMuse-AI auth: if AUTH_ENABLED is on (default since v0.9.6), you need an
  API_TOKEN configured in AudioMuse-AI and passed here via --audiomuse-token;
  it's sent as "Authorization: Bearer <token>". If your instance has auth
  disabled, just omit --audiomuse-token.
* Jellyfin auth: an API key created under Dashboard > API Keys, sent as the
  "X-Emby-Token" header.
* mood_vector format: AudioMuse-AI stores it server-side as a comma-separated
  "mood:weight" string (e.g. "energetic:0.812,happy:0.431,sad:0.112"). This
  script parses that format; if a future AudioMuse-AI version changes it,
  adjust parse_mood_vector().
* Jellyfin item updates are "whole object" updates: you GET the full item,
  modify fields, and POST the whole thing back to /Items/{itemId}. This script
  does that, merging (not replacing) existing Genres/Tags so you don't lose
  manually-curated ones.
