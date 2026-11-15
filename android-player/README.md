# Soulseek Player (Android)

Standalone Android client scaffold for Navidrome/Subsonic plus the recommendation API exposed by `soulseek-queue`.

## Build
- Android Studio Ladybug or newer
- JDK 17
- Android SDK 35
- Open `android-player/` as a Gradle project and run the `app` configuration.

## Configuration
In the app's Settings tab:
- Navidrome base URL (for example `https://music.example.com`), username and password.
- soulseek-queue base URL (for example `http://server:8090`).

The Navidrome client uses Subsonic token authentication (`md5(password + salt)`) and API v1.16.1 JSON responses. Recommendation integration uses the documented `/api/lastfm/recommendations` and `/api/playlists` routes from the repository README.

## Current implementation
- Navidrome connectivity check, Subsonic search3, random songs, starred tracks, stream URL generation.
- Recommendation requests for recent/top/loved tracks; create recommendation playlists and list playlists.
- Media3 playback service with MediaSession foundation.
- Server URLs and credentials are configurable; no server addresses or credentials are hard-coded.

## Next engineering steps
Complete the playback controller binding (MediaController), artwork/lyrics, queue persistence and controls, playlist detail/download handling, richer recommendation modes, encrypted credential storage, and instrumented tests. This initial module is a working integration foundation, not yet a release-ready player.
