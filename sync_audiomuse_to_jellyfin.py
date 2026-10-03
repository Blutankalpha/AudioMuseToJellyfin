#!/usr/bin/env python3
"""
sync_audiomuse_to_jellyfin.py

Pulls analysis data (BPM/tempo and mood) from an AudioMuse-AI instance
(https://github.com/NeptuneHub/AudioMuse-AI) and writes it back into Jellyfin:

  - AudioMuse "tempo"       -> Jellyfin Tag, e.g. "BPM:128"   (Jellyfin has no native BPM field)
  - AudioMuse "mood_vector" -> Jellyfin Genres (top N moods, e.g. "energetic", "happy")

How matching works
-------------------
IMPORTANT: AudioMuse-AI's own `item_id` is NOT the same as Jellyfin's item GUID
on most setups (confirmed: e.g. AudioMuse item_id "6ef2ff49c14b462bdd224006af567008"
vs the same track's Jellyfin Id "96fb6f6911e617e2113140c0f41e7d8a" -- unrelated
values, not just a formatting difference). So this script does NOT assume the
ids line up. Instead it resolves each track in two steps:

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
* Jellyfin auth: an API key created under Dashboard > API Keys. Recent Jellyfin
  (10.11+/12) requires the standard "Authorization: MediaBrowser Token=..."
  header (the legacy X-Emby-Token header was removed); this script uses that.
* mood_vector format: a comma-separated "mood:weight" string, e.g.
  "energetic:0.812,happy:0.431,sad:0.112". Adjust parse_mood_vector() if a
  future AudioMuse-AI version changes this.
* Jellyfin item updates are "whole object" updates: GET the full item, modify
  fields, POST the whole thing back to /Items/{itemId}. This script merges
  (doesn't replace) existing Genres/Tags so you don't lose curated ones.
* Title/artist matching is inherently fuzzy. Ambiguous or unmatched tracks are
  skipped and logged rather than guessed at - rerun with -v to review them.
"""

import argparse
import difflib
import logging
import re
import sys
import time
from typing import Dict, List, Optional, Tuple

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("audiomuse2jellyfin")


# --------------------------------------------------------------------------- #
# AudioMuse-AI client
# --------------------------------------------------------------------------- #

def normalize_text(value: str) -> str:
    """Lowercase, strip, collapse whitespace, drop punctuation for fuzzy-ish matching."""
    if not value:
        return ""
    value = value.lower().strip()
    value = re.sub(r"[\(\)\[\]\{\}]", " ", value)
    value = re.sub(r"[^a-z0-9 ]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


class AudioMuseClient:
    def __init__(self, base_url: str, token: Optional[str] = None, server: Optional[str] = None,
                 search_path: str = "/api/search_tracks", match_threshold: float = 0.90,
                 search_limit: int = 200, timeout: int = 15):
        self.base_url = base_url.rstrip("/")
        self.server = server
        self.search_path = search_path
        self.match_threshold = match_threshold
        self.search_limit = search_limit
        self.timeout = timeout
        self.session = requests.Session()
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        self._artist_cache: Dict[str, List[dict]] = {}
        self._title_cache: Dict[str, List[dict]] = {}

    def _search_tracks_page(self, start: int, end: int, artist: Optional[str] = None,
                             title: Optional[str] = None) -> Optional[List[dict]]:
        """One page of GET <search_path>, filtering by artist and/or title (legacy params).
        Returns None on a hard failure (caller stops paging)."""
        params = {
            "index": "musicnn",     # the index with every analyzed song for the server
            "start": start,
            "end": end,
        }
        if artist:
            params["artist"] = artist
        if title:
            params["title"] = title
        if self.server:
            params["server"] = self.server
        label = artist if artist else f"title={title!r}"
        url = f"{self.base_url}{self.search_path}"
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            log.warning("AudioMuse-AI search_tracks request failed for %s: %s", label, exc)
            return None

        if resp.status_code in (401, 403):
            log.warning(
                "AudioMuse-AI rejected search_tracks for %s with %s - check --audiomuse-token: %s",
                label, resp.status_code, resp.text[:300],
            )
            return None
        if resp.status_code == 404:
            log.warning(
                "search_tracks path '%s' returned 404. Check http://<audiomuse-host>:8000/apidocs "
                "for the correct path on your version and pass it via --audiomuse-search-path.",
                self.search_path,
            )
            return None
        if not resp.ok:
            log.warning("AudioMuse-AI search_tracks returned %s for %s: %s",
                        resp.status_code, label, resp.text[:300])
            return None

        try:
            results = resp.json()
        except ValueError:
            log.warning("AudioMuse-AI search_tracks returned non-JSON for %s", label)
            return None
        if not isinstance(results, list):
            # Some versions may wrap results in {"results": [...]}
            results = results.get("results", []) if isinstance(results, dict) else []
        return results

    def _search_tracks_paginated(self, cache_key: str, cache: Dict[str, List[dict]],
                                  artist: Optional[str] = None, title: Optional[str] = None) -> List[dict]:
        if cache_key in cache:
            return cache[cache_key]

        page_size = min(self.search_limit, 500)
        all_results: List[dict] = []
        start = 0
        while True:
            page = self._search_tracks_page(start, start + page_size, artist=artist, title=title)
            if page is None:
                break  # request failed; keep whatever we already gathered
            all_results.extend(page)
            if len(page) < page_size:
                break  # last page
            start += page_size
            if start > 20000:  # safety valve against a misbehaving/looping API
                log.warning("Stopped paginating search_tracks for %r after 20000 rows", cache_key)
                break

        cache[cache_key] = all_results
        return all_results

    def search_tracks_by_artist(self, artist: str) -> List[dict]:
        """
        Fetch an artist's full AudioMuse-AI catalog as [{author,item_id,title}, ...],
        paginating via start/end (server caps each page at 500 rows; default page
        size without these params is only 20). Cached per artist for this client's
        lifetime, since every track by that artist reuses the same lookup.
        """
        return self._search_tracks_paginated(artist.strip().lower(), self._artist_cache, artist=artist)

    def search_tracks_by_title(self, title: str) -> List[dict]:
        """
        Fetch every AudioMuse-AI track whose title matches, regardless of artist.
        Used as a fallback when an artist-scoped search comes back empty, which
        usually means Jellyfin and AudioMuse-AI disagree on how that artist is
        named (common with soundtracks/compilations) rather than the track being
        unanalyzed. Cached per title for this client's lifetime.
        """
        return self._search_tracks_paginated(title.strip().lower(), self._title_cache, title=title)

    def resolve_item_id(self, artist: str, title: str) -> Tuple[Optional[str], str]:
        """
        Resolve a Jellyfin (artist, title) pair to an AudioMuse-AI item_id.
        Returns (item_id_or_None, how): how is "exact", "fuzzy", or "none".
        """
        candidates = self.search_tracks_by_artist(artist)
        target = normalize_text(title)

        if not candidates:
            # AudioMuse-AI has nothing filed under this exact artist string at
            # all - common with soundtracks/compilations tagged inconsistently
            # between Jellyfin and AudioMuse-AI. Fall back to a title-only
            # search across the whole catalog, accepting it only if exactly
            # one track anywhere has this exact title (safe enough: exact
            # title collisions across an entire library are rare).
            title_candidates = self.search_tracks_by_title(title)
            exact = [c for c in title_candidates if normalize_text(c.get("title", "")) == target]
            if len(exact) == 1:
                log.debug(
                    "Resolved '%s' via title-only fallback (artist=%r had 0 candidates; "
                    "AudioMuse-AI has it filed under author=%r)",
                    title, artist, exact[0].get("author"),
                )
                return exact[0].get("item_id"), "title_fallback"
            if len(exact) > 1:
                log.debug(
                    "Title-only fallback for %r is ambiguous (%d exact matches across different "
                    "artists) - skipping rather than guessing", title, len(exact),
                )
            return None, "none"

        exact_matches = [c for c in candidates if normalize_text(c.get("title", "")) == target]
        if len(exact_matches) == 1:
            return exact_matches[0].get("item_id"), "exact"
        if len(exact_matches) > 1:
            # Ambiguous exact match (e.g. same title appears twice for this
            # artist, maybe a different album/remaster) - don't guess.
            log.debug("Ambiguous exact title match for artist=%r title=%r (%d candidates)",
                      artist, title, len(exact_matches))
            return None, "none"

        best_item = None
        best_ratio = 0.0
        for c in candidates:
            ratio = difflib.SequenceMatcher(None, target, normalize_text(c.get("title", ""))).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_item = c
        if best_item and best_ratio >= self.match_threshold:
            return best_item.get("item_id"), "fuzzy"

        # No match found. Log *why*, so "no candidates for this artist at all"
        # (probably not analyzed yet) is distinguishable from "candidates exist
        # but no title lines up" (probably an artist-name mismatch).
        if best_item:
            log.debug(
                "No match for artist=%r title=%r: closest of %d candidates was %r (ratio=%.2f, below threshold %.2f)",
                artist, title, len(candidates), best_item.get("title"), best_ratio, self.match_threshold,
            )
        else:
            log.debug(
                "No match for artist=%r title=%r: AudioMuse-AI returned 0 candidates for this artist "
                "(likely not analyzed yet, or stored under a different artist name)",
                artist, title,
            )
        return None, "none"

    def get_score(self, item_id: str) -> Optional[dict]:
        """GET /external/get_score?id=<item_id>[&server=<name>]"""
        params = {"id": item_id}
        if self.server:
            params["server"] = self.server
        url = f"{self.base_url}/external/get_score"
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            log.warning("AudioMuse-AI request failed for id=%s: %s", item_id, exc)
            return None

        if resp.status_code == 404:
            log.debug("AudioMuse-AI has no score for id=%s (404 - not analyzed yet, or server mismatch)", item_id)
            return None
        if resp.status_code in (401, 403):
            log.warning(
                "AudioMuse-AI rejected the request for id=%s with %s - check --audiomuse-token "
                "(AUTH_ENABLED instances need it): %s",
                item_id, resp.status_code, resp.text[:300],
            )
            return None
        if not resp.ok:
            log.warning(
                "AudioMuse-AI returned %s for id=%s: %s",
                resp.status_code, item_id, resp.text[:300],
            )
            return None
        try:
            return resp.json()
        except ValueError:
            log.warning("AudioMuse-AI returned non-JSON for id=%s", item_id)
            return None


def parse_mood_vector(mood_vector) -> List[Tuple[str, float]]:
    """
    Parse AudioMuse-AI's mood_vector field into [(mood, weight), ...] sorted
    by weight descending. Handles the "mood:weight,mood:weight" string format
    AudioMuse-AI's database.py writes, and tolerates a dict/list if a future
    version returns structured data instead.
    """
    if not mood_vector:
        return []

    if isinstance(mood_vector, dict):
        pairs = list(mood_vector.items())
    elif isinstance(mood_vector, list):
        pairs = []
        for entry in mood_vector:
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                pairs.append((entry[0], entry[1]))
    else:
        pairs = []
        for chunk in str(mood_vector).split(","):
            chunk = chunk.strip()
            if not chunk or ":" not in chunk:
                continue
            name, _, value = chunk.rpartition(":")
            try:
                pairs.append((name.strip(), float(value)))
            except ValueError:
                continue

    try:
        pairs = [(str(name), float(weight)) for name, weight in pairs]
    except (ValueError, TypeError):
        return []

    pairs.sort(key=lambda p: p[1], reverse=True)
    return pairs


# --------------------------------------------------------------------------- #
# Jellyfin client
# --------------------------------------------------------------------------- #

class JellyfinClient:
    def __init__(self, base_url: str, api_key: str, user_id: str, timeout: int = 15):
        self.base_url = base_url.rstrip("/")
        self.user_id = user_id
        self.timeout = timeout
        self.session = requests.Session()
        # Jellyfin 10.11+ / 12 dropped the legacy X-Emby-Token / X-MediaBrowser-Token
        # headers (see jellyfin/jellyfin#15559). The standard "Authorization:
        # MediaBrowser Token=..." header works on both old and new servers, so we
        # use that instead. A Client/Device/DeviceId/Version are required by the
        # scheme even for API-key auth; the values don't need to mean anything,
        # they just need to be present and unique-ish.
        self.session.headers["Authorization"] = (
            'MediaBrowser Token="{token}", Client="sync_audiomuse_to_jellyfin", '
            'Device="script", DeviceId="sync-audiomuse-to-jellyfin-script", Version="1.0.0"'
        ).format(token=api_key)
        self.session.headers["Accept"] = "application/json"

    def iter_audio_items(self, limit_page: int = 200):
        """Yield every Audio item in the library (handles pagination)."""
        start_index = 0
        while True:
            params = {
                "IncludeItemTypes": "Audio",
                "Recursive": "true",
                "Fields": "Genres,Tags,Path,AlbumArtist,Artists,ArtistItems",
                "StartIndex": start_index,
                "Limit": limit_page,
            }
            url = f"{self.base_url}/Users/{self.user_id}/Items"
            resp = self.session.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            items = data.get("Items", [])
            if not items:
                break
            for item in items:
                yield item
            start_index += len(items)
            total = data.get("TotalRecordCount", 0)
            if start_index >= total:
                break

    def get_item(self, item_id: str) -> dict:
        url = f"{self.base_url}/Users/{self.user_id}/Items/{item_id}"
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def update_item(self, item_id: str, item: dict) -> None:
        """Jellyfin updates are whole-object: POST the full item back."""
        url = f"{self.base_url}/Items/{item_id}"
        resp = self.session.post(url, json=item, timeout=self.timeout)
        resp.raise_for_status()


# --------------------------------------------------------------------------- #
# Sync logic
# --------------------------------------------------------------------------- #

def build_bpm_tag(tempo: float, prefix: str) -> str:
    return f"{prefix}{round(tempo)}"


def plan_update(jf_item: dict, score: dict, bpm_tag_prefix: str,
                 top_n_moods: int, mood_min_weight: float,
                 replace_genres: bool) -> Tuple[Optional[dict], List[str]]:
    """
    Returns (updated_item_or_None, change_descriptions).
    updated_item is None if nothing needs to change.
    """
    changes: List[str] = []
    existing_genres = list(jf_item.get("Genres") or [])
    existing_tags = list(jf_item.get("Tags") or [])

    new_genres = [] if replace_genres else list(existing_genres)
    new_tags = list(existing_tags)

    # --- Mood -> Genre ---
    moods = parse_mood_vector(score.get("mood_vector"))
    moods = [(name, w) for name, w in moods if w >= mood_min_weight][:top_n_moods]
    for mood_name, _weight in moods:
        genre_label = mood_name.strip().title()
        if genre_label and genre_label not in new_genres:
            new_genres.append(genre_label)
            changes.append(f"+genre '{genre_label}'")

    # --- Tempo (BPM) -> Tag ---
    tempo = score.get("tempo")
    if tempo:
        try:
            tempo = float(tempo)
        except (TypeError, ValueError):
            tempo = None
    if tempo:
        # Drop any previous BPM tag from this script before adding the fresh one.
        new_tags = [t for t in new_tags if not t.startswith(bpm_tag_prefix)]
        bpm_tag = build_bpm_tag(tempo, bpm_tag_prefix)
        new_tags.append(bpm_tag)
        changes.append(f"+tag '{bpm_tag}'")

    if not changes:
        return None, []

    if set(new_genres) == set(existing_genres) and set(new_tags) == set(existing_tags):
        return None, []

    updated = dict(jf_item)
    updated["Genres"] = new_genres
    updated["Tags"] = new_tags
    return updated, changes


def run(args: argparse.Namespace) -> int:
    audiomuse = AudioMuseClient(
        base_url=args.audiomuse_url,
        token=args.audiomuse_token,
        server=args.audiomuse_server,
        search_path=args.audiomuse_search_path,
        match_threshold=args.match_threshold,
        search_limit=args.audiomuse_search_limit,
    )
    jellyfin = JellyfinClient(
        base_url=args.jellyfin_url,
        api_key=args.jellyfin_api_key,
        user_id=args.jellyfin_user_id,
    )

    processed = 0
    updated = 0
    skipped_no_score = 0
    skipped_unresolved = 0
    resolved_exact = 0
    resolved_fuzzy = 0
    resolved_title_fallback = 0
    errors = 0

    for item in jellyfin.iter_audio_items():
        item_id = item.get("Id")
        name = item.get("Name", "<unknown>")
        if not item_id:
            continue

        processed += 1
        if args.limit and processed > args.limit:
            break

        artist = (item.get("AlbumArtist")
                  or (item.get("Artists") or [None])[0]
                  or (item.get("ArtistItems") or [{}])[0].get("Name", ""))
        if not artist or not name:
            skipped_unresolved += 1
            log.debug("Skipping '%s' (%s): missing artist or title needed to resolve", name, item_id)
            continue

        audiomuse_item_id, how = audiomuse.resolve_item_id(artist, name)
        if not audiomuse_item_id:
            skipped_unresolved += 1
            log.debug("No AudioMuse-AI match for '%s' by '%s' (%s)", name, artist, item_id)
            continue
        if how == "exact":
            resolved_exact += 1
        elif how == "title_fallback":
            resolved_title_fallback += 1
        else:
            resolved_fuzzy += 1
            log.debug("Fuzzy-matched '%s' by '%s' -> AudioMuse item_id %s", name, artist, audiomuse_item_id)

        score = audiomuse.get_score(audiomuse_item_id)
        if not score:
            skipped_no_score += 1
            log.debug("No AudioMuse-AI score for '%s' (jellyfin=%s, audiomuse=%s)",
                      name, item_id, audiomuse_item_id)
            continue

        try:
            new_item, changes = plan_update(
                item, score,
                bpm_tag_prefix=args.bpm_tag_prefix,
                top_n_moods=args.top_moods,
                mood_min_weight=args.mood_min_weight,
                replace_genres=args.replace_genres,
            )
        except Exception:
            errors += 1
            log.exception("Failed to plan update for '%s' (%s)", name, item_id)
            continue

        if not new_item:
            log.debug("Nothing to change for '%s' (%s)", name, item_id)
            continue

        log.info("'%s' (%s): %s", name, item_id, ", ".join(changes))

        if args.dry_run:
            continue

        try:
            jellyfin.update_item(item_id, new_item)
            updated += 1
        except requests.RequestException as exc:
            errors += 1
            log.error("Failed to update '%s' (%s): %s", name, item_id, exc)

        if args.sleep:
            time.sleep(args.sleep)

    log.info(
        "Done. processed=%d resolved_exact=%d resolved_fuzzy=%d resolved_title_fallback=%d "
        "unresolved=%d no_score_after_resolve=%d updated=%d errors=%d%s",
        processed, resolved_exact, resolved_fuzzy, resolved_title_fallback, skipped_unresolved,
        skipped_no_score, updated, errors,
        " (dry-run, nothing written)" if args.dry_run else "",
    )
    return 0 if errors == 0 else 1


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)

    jf = p.add_argument_group("Jellyfin")
    jf.add_argument("--jellyfin-url", required=True, help="e.g. http://jellyfin.local:8096")
    jf.add_argument("--jellyfin-api-key", required=True, help="Jellyfin API key (Dashboard > API Keys)")
    jf.add_argument("--jellyfin-user-id", required=True, help="A Jellyfin user id with library access")

    am = p.add_argument_group("AudioMuse-AI")
    am.add_argument("--audiomuse-url", required=True, help="e.g. http://audiomuse.local:8000")
    am.add_argument("--audiomuse-token", default=None, help="API_TOKEN, if AUTH_ENABLED on AudioMuse-AI")
    am.add_argument("--audiomuse-server", default=None,
                     help="AudioMuse-AI server name/id, only needed in multi-server setups")
    am.add_argument("--audiomuse-search-path", default="/api/search_tracks",
                     help="Path of AudioMuse-AI's search-by-artist endpoint "
                          "(check http://<host>:8000/apidocs if the default 404s)")
    am.add_argument("--match-threshold", type=float, default=0.90,
                     help="Minimum title similarity (0-1) to accept a fuzzy match "
                          "when an exact title match isn't found (default: 0.90)")
    am.add_argument("--audiomuse-search-limit", type=int, default=500,
                     help="Page size for paginating an artist's search_tracks results "
                          "(server caps this at 500 rows/page; default: 500)")

    mapping = p.add_argument_group("Mapping options")
    mapping.add_argument("--bpm-tag-prefix", default="BPM:",
                          help="Prefix for the BPM tag written to Jellyfin (default: 'BPM:')")
    mapping.add_argument("--top-moods", type=int, default=3,
                          help="Max number of top moods to write as genres (default: 3)")
    mapping.add_argument("--mood-min-weight", type=float, default=0.3,
                          help="Minimum AudioMuse-AI mood weight to include (default: 0.3)")
    mapping.add_argument("--replace-genres", action="store_true",
                          help="Replace existing Genres instead of merging moods into them")

    run_opts = p.add_argument_group("Run options")
    run_opts.add_argument("--dry-run", action="store_true", help="Log planned changes, write nothing")
    run_opts.add_argument("--limit", type=int, default=0, help="Stop after N items (0 = no limit)")
    run_opts.add_argument("--sleep", type=float, default=0.0,
                           help="Seconds to sleep between Jellyfin writes (be gentle on the server)")
    run_opts.add_argument("-v", "--verbose", action="store_true", help="Debug logging")

    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.verbose:
        log.setLevel(logging.DEBUG)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
