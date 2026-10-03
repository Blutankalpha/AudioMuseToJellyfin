An attempt at vibecoding (first time for everything) a script to move Audiomuse metadata into Jellyfin

Generated using Claude.ai's "Sonnet 5" model, with Medium effort and thinking enabled.

Pulls analysis data (BPM/tempo and mood) from an AudioMuse-AI instance
(https://github.com/NeptuneHub/AudioMuse-AI) and writes it back into Jellyfin:

  - AudioMuse "tempo"       -> Jellyfin Tag, "BPM:128"
  - AudioMuse "mood_vector" -> Jellyfin Genres

How matching works
-------------------
It resolves each track in two steps:

  1. GET /api/search_tracks?artist=<artist name>&start=&end=  -> list of
     {author, item_id, title}, paginated (server default page size is only 20;
     this script pages through with start/end, capped at 500 rows/page, to
     fetch an artist's full catalog). One cached lookup per distinct artist.
  2. Match the Jellyfin track's title against that candidate list (normalized
     exact match first, then fuzzy match above --match-threshold) to find the
     AudioMuse-AI item_id for that specific track.
  2b. If the artist search comes back with zero candidates at all (AudioMuse-AI
      has nothing filed under that exact artist string - common with
      soundtracks/compilations tagged inconsistently), fall back to a
      title-only search across the whole catalog, accepted only when exactly
      one track anywhere has that exact title.
  3. GET /external/get_score?id=<resolved AudioMuse item_id> -> tempo, mood_vector

If your AudioMuse-AI setup has multiple music servers configured, pass
--audiomuse-server with the exact server name you gave it in the Setup Wizard.

If /api/search_tracks lives at a different path on your version (check
http://<audiomuse-host>:8000/apidocs), override it with --audiomuse-search-path.

Requirements
------------
    python3
    pip install requests
Usage
-----
    python sync_audiomuse_to_jellyfin.py \
        --jellyfin-url http://jellyfin.local:8096 \
        --jellyfin-api-key XXXXXXXX \
        --jellyfin-user-id XXXXXXXX \
        --audiomuse-url http://audiomuse.local:8000 \
        --audiomuse-token YYYYYYYY \
        --dry-run -v

Remove --dry-run once you're happy with the planned changes. -v (verbose) is
recommended on first run so you can see match/no-match decisions per track.

Notes / assumptions (please double check against your setup)
--------------------------------------------------------------
* AudioMuse-AI auth: if AUTH_ENABLED is on, you need an API_TOKEN configured in
  AudioMuse-AI and passed here via --audiomuse-token; it's sent as
  "Authorization: Bearer <token>". If your instance has auth disabled, omit it.
* Jellyfin auth: an API key created under Dashboard > API Keys. Jellyfin sends it using the "Authorization: MediaBrowser Token=..."
* mood_vector format: a comma-separated "mood:weight" string, e.g.
  "energetic:0.812,happy:0.431,sad:0.112". Adjust parse_mood_vector() if a
  future AudioMuse-AI version changes this.
* Jellyfin item updates are "whole object" updates: GET the full item, modify
  fields, POST the whole thing back to /Items/{itemId}. This script merges
  (doesn't replace) existing Genres/Tags so you don't lose curated ones.
* Title/artist matching is inherently fuzzy. Ambiguous or unmatched tracks are
  skipped and logged rather than guessed at - rerun with -v to review them.
