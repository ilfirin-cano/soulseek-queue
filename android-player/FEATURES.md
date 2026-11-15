# Android player: functional scope and protocol notes

## Product goal
A native Android client with two independent backends:
1. Navidrome through the Subsonic/OpenSubsonic REST API: the source of truth for indexed audio and playback.
2. soulseek-queue through its existing HTTP API: recommendation generation, recommendation playlists, download progress and availability metadata.

The Android app must not duplicate Last.fm credentials or recommendation algorithms. The recommendation service remains responsible for those operations.

## Core user journeys
- Configure Navidrome URL, username and password; validate with `ping.view`.
- Configure soulseek-queue base URL independently.
- Browse and search the Navidrome catalogue; open artist/album pages; view favorites and playlists.
- Play a track, pause/resume, seek, skip, maintain a local queue and restore it after process death.
- Continue playback in background with a Media3 foreground service, MediaSession, lock-screen controls, audio focus and noisy-audio handling.
- Mark tracks as favorites, rate tracks, report completed playback with `scrobble.view`, and persist/restore server play queue.
- Request recommendation modes: `recent_tracks`, `top_tracks`, `loved_tracks`, `top_artists`, `genre_tracks`, `genre_artists`, `similar_artist`, `similar_track`.
- Create recommendation playlists using `POST /api/playlists`; optionally pass `queue: true`, `start: true`, `download: true` to enqueue Soulseek searches/downloads.
- Show playlist detail from `GET /api/playlists/{playlist_id}`, including recommendation status, downloaded items and local availability.
- Refresh Navidrome catalogue after files are imported; never assume a recommendation is playable until it exists in Navidrome's indexed library.

## Subsonic/OpenSubsonic integration checklist
- Protocol negotiation/health: `ping`, `getOpenSubsonicExtensions` where supported.
- Authentication: Subsonic token auth (`md5(password + salt)`), HTTPS strongly recommended.
- Discovery: `getMusicFolders`, `getArtists`, `getArtist`, `getAlbum`, `getAlbumList2`, `getGenres`.
- Search: `search3`; keep server IDs as opaque strings.
- Media: `stream`, `getCoverArt`, `getLyrics`; support byte-range seeking and server transcoding parameters.
- Personal library: `getStarred2`, `star`, `unstar`, `setRating`.
- Playlists: `getPlaylists`, `getPlaylist`, `createPlaylist`, `updatePlaylist`, `deletePlaylist`.
- Queue: `getPlayQueue`, `savePlayQueue`.
- Listening history: `scrobble` with submission after a completed/qualifying play. Navidrome does not count a stream request itself as a play.
- Optional OpenSubsonic: `getLyricsBySongId`, `reportPlayback`, `getTranscodeDecision`, `songLyrics`/lyrics extensions as negotiated.

## Navidrome-specific compatibility constraints
- Navidrome documents Subsonic API compatibility at v1.16.1, with incremental OpenSubsonic extensions.
- IDs are opaque strings (often hashes/UUIDs); never parse them as integers.
- Navidrome does not implement folder browsing as a real filesystem API; `getIndexes`/`getMusicDirectory` are simulated.
- Navidrome only counts a play when `scrobble` is submitted, not merely when `stream` is requested.
- `search2`/`search3` support simple autocomplete rather than Lucene query syntax.
- Last.fm-backed endpoints such as similar songs/top songs depend on server integrations; recommendation calls should go to soulseek-queue instead.

## Security and reliability
- Store credentials in Android Keystore-backed encrypted preferences (implemented for local settings).
- Prefer HTTPS for both services. Cleartext HTTP exposes credentials/tokens to the local network.
- Never log passwords, auth tokens, API keys or full authenticated stream URLs.
- Use bounded connect/read timeouts, retry only idempotent requests, and surface server error codes.
- Handle expired credentials, offline servers, TLS errors, malformed JSON and partial recommendation responses.
- Do not send recommendation-system API secrets to the Android client.

## Current code status (0.1.0 foundation)
Implemented in this repository:
- Separate configurable service URLs and Navidrome account settings.
- Subsonic token authentication and `ping`, `search3`, `getRandomSongs`, `getStarred2`, `star`/`unstar`, and stream URL construction.
- Initial soulseek-queue integration for recent/top/loved recommendations, playlist creation and playlist listing.
- Media3 ExoPlayer service + MediaSession; selected Navidrome search results can be played and paused/resumed.
- Keystore-backed encrypted preferences for saved settings.

Not yet implemented / must not be considered release-ready:
- Full artist/album/genre navigation and native Navidrome playlist CRUD.
- Reliable queue editing, persistence and server `getPlayQueue`/`savePlayQueue` synchronization.
- Seek/next/previous UI, playback progress, scrobble threshold logic and `reportPlayback`.
- Recommendation modes beyond recent/top/loved, detail/status views and reconciliation of downloaded items with Navidrome IDs.
- Cover art, lyrics, offline downloads, Android Auto, Bluetooth/media-button QA and accessibility polish.
- Unit/instrumentation tests and a verified release build.

## Build validation
Open this directory in Android Studio with JDK 17 and Android SDK 35. The GitHub file-edit workflow used to add this module does not provide an Android SDK/Gradle execution environment; therefore compilation and device tests have not been run here.
