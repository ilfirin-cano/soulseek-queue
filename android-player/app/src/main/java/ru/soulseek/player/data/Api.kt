package ru.soulseek.player.data

import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.OkHttpClient
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Request
import org.json.JSONArray
import org.json.JSONObject
import java.security.MessageDigest
import java.util.UUID
import java.util.concurrent.TimeUnit

data class Track(val id: String, val title: String, val artist: String = "", val album: String = "", val duration: Int = 0, val coverId: String? = null)
data class RecPlaylist(val id: String, val name: String, val count: Int = 0, val downloadedCount: Int = 0)

class Api {
    private val http = OkHttpClient.Builder().connectTimeout(15, TimeUnit.SECONDS).readTimeout(60, TimeUnit.SECONDS).build()
    var navUrl = ""
    var username = ""
    var password = ""
    var recommendationUrl = ""

    private fun md5(s: String): String = MessageDigest.getInstance("MD5").digest(s.toByteArray()).joinToString("") { "%02x".format(it) }

    private fun subsonic(method: String, params: Map<String, String> = emptyMap()): JSONObject {
        require(navUrl.isNotBlank()) { "Укажите адрес Navidrome" }
        val salt = UUID.randomUUID().toString().replace("-", "").take(12)
        val url = (navUrl.trimEnd('/') + "/rest/$method.view").toHttpUrl().newBuilder()
            .addQueryParameter("u", username).addQueryParameter("t", md5(password + salt))
            .addQueryParameter("s", salt).addQueryParameter("v", "1.16.1")
            .addQueryParameter("c", "SoulseekPlayer").addQueryParameter("f", "json")
        params.forEach { (k, v) -> url.addQueryParameter(k, v) }
        val request = Request.Builder().url(url.build()).get().build()
        http.newCall(request).execute().use { response ->
            if (!response.isSuccessful) error("Navidrome HTTP ${response.code}")
            val root = JSONObject(response.body?.string().orEmpty()).getJSONObject("subsonic-response")
            if (root.optString("status") != "ok") error(root.optJSONObject("error")?.optString("message") ?: "Ошибка Subsonic API")
            return root
        }
    }

    fun ping() { subsonic("ping") }

    fun search(query: String): List<Track> {
        val root = subsonic("search3", mapOf("query" to query, "songCount" to "80", "albumCount" to "0", "artistCount" to "0"))
        val songs = root.optJSONObject("searchResult3")?.optJSONArray("song") ?: JSONArray()
        return (0 until songs.length()).map { songs.getJSONObject(it).toTrack() }
    }

    fun randomSongs(size: Int = 40): List<Track> {
        val songs = subsonic("getRandomSongs", mapOf("size" to size.toString())).optJSONObject("randomSongs")?.optJSONArray("song") ?: JSONArray()
        return (0 until songs.length()).map { songs.getJSONObject(it).toTrack() }
    }

    fun starred(): List<Track> {
        val songs = subsonic("getStarred2").optJSONObject("starred2")?.optJSONArray("song") ?: JSONArray()
        return (0 until songs.length()).map { songs.getJSONObject(it).toTrack() }
    }

    fun setStar(track: Track, starred: Boolean) { subsonic(if (starred) "star" else "unstar", mapOf("id" to track.id)) }

    fun streamUrl(track: Track): String {
        val salt = UUID.randomUUID().toString().replace("-", "").take(12)
        return navUrl.trimEnd('/') + "/rest/stream.view?u=$username&t=${md5(password + salt)}&s=$salt&v=1.16.1&c=SoulseekPlayer&id=${track.id}"
    }

    fun recommendations(mode: String, limit: Int = 25, query: String = "", artist: String = "", period: String = "overall"): List<JSONObject> {
        val body = recommendationBody(mode, limit, query, artist, period)
        val root = postRecommendation("/api/lastfm/recommendations", body)
        return root.optJSONArray("items").objects()
    }

    fun playlists(): List<RecPlaylist> {
        val root = recommendationGet("/api/playlists")
        val array = root.optJSONArray("playlists") ?: root.optJSONArray("items") ?: JSONArray()
        return array.objects().map { RecPlaylist(it.optString("id", it.optString("playlist_id")), it.optString("name"), it.optInt("recommendation_count", it.optInt("count")), it.optInt("downloaded_count")) }
    }

    fun createPlaylist(mode: String, name: String, queue: Boolean, query: String = "", artist: String = "", period: String = "overall", limit: Int = 25): JSONObject {
        val body = recommendationBody(mode, limit, query, artist, period)
            .put("name", name.trim().ifBlank { "Рекомендации" })
            .put("queue", queue).put("start", queue).put("download", queue)
        val result = postRecommendation("/api/playlists", body)
        require(result.optString("playlist_id").isNotBlank()) {
            result.optString("error").ifBlank { "Сервер не вернул ID созданного плейлиста" }
        }
        return result
    }

    private fun recommendationBody(mode: String, limit: Int, query: String, artist: String, period: String): JSONObject {
        val body = JSONObject().put("mode", mode).put("limit", limit.coerceIn(1, 50)).put("period", period)
        if (query.isNotBlank()) body.put("query", query.trim())
        if (artist.isNotBlank()) body.put("artist", artist.trim())
        return body
    }

    private fun recommendationGet(path: String): JSONObject {
        require(recommendationUrl.isNotBlank()) { "Укажите адрес soulseek-queue" }
        val request = Request.Builder().url(recommendationUrl.trimEnd('/') + path).get().build()
        http.newCall(request).execute().use { r -> if (!r.isSuccessful) error("Soulseek Queue HTTP ${r.code}"); return JSONObject(r.body?.string().orEmpty()) }
    }

    private fun postRecommendation(path: String, json: JSONObject): JSONObject {
        require(recommendationUrl.isNotBlank()) { "Укажите адрес soulseek-queue" }
        val request = Request.Builder().url(recommendationUrl.trimEnd('/') + path)
            .post(json.toString().toRequestBody("application/json".toMediaType())).build()
        http.newCall(request).execute().use { r ->
            val payload = r.body?.string().orEmpty()
            val json = runCatching { JSONObject(payload) }.getOrElse { error("Некорректный ответ soulseek-queue, HTTP " + r.code) }
            if (!r.isSuccessful || json.has("error")) error(json.optString("error").ifBlank { "Soulseek Queue HTTP " + r.code })
            return json
        }
    }

    private fun JSONObject.toTrack() = Track(getString("id"), optString("title", "Без названия"), optString("artist"), optString("album"), optInt("duration"), optString("coverArt").takeIf { it.isNotBlank() })
    private fun JSONArray.objects() = (0 until length()).mapNotNull { optJSONObject(it) }
}
