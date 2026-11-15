package ru.soulseek.player

import android.content.ComponentName
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import java.util.concurrent.Executor
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.platform.LocalContext
import androidx.media3.common.MediaItem
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.media3.session.MediaController
import androidx.media3.session.SessionToken
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import ru.soulseek.player.data.Api
import ru.soulseek.player.data.Track

class MainActivity : ComponentActivity() {
    private val api = Api()
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val masterKey = MasterKey.Builder(this).setKeyScheme(MasterKey.KeyScheme.AES256_GCM).build()
        val prefs = EncryptedSharedPreferences.create(
            this, "settings", masterKey,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM
        )
        api.navUrl = prefs.getString("navUrl", "") ?: ""
        api.username = prefs.getString("username", "") ?: ""
        api.password = prefs.getString("password", "") ?: ""
        api.recommendationUrl = prefs.getString("recommendationUrl", "") ?: ""
        setContent { PlayerApp(api, prefs) }
    }
}

@Composable
@OptIn(ExperimentalMaterial3Api::class)
private fun PlayerApp(api: Api, prefs: android.content.SharedPreferences) {
    val context = LocalContext.current
    var mediaController by remember { mutableStateOf<MediaController?>(null) }
    DisposableEffect(context) {
        val token = SessionToken(context, ComponentName(context, PlaybackService::class.java))
        val future = MediaController.Builder(context, token).buildAsync()
        future.addListener({ runCatching { mediaController = future.get() } }, Executor { Handler(Looper.getMainLooper()).post(it) })
        onDispose { mediaController?.release(); mediaController = null }
    }
    var tab by remember { mutableStateOf(0) }
    var navUrl by remember { mutableStateOf(api.navUrl) }
    var user by remember { mutableStateOf(api.username) }
    var pass by remember { mutableStateOf(api.password) }
    var recUrl by remember { mutableStateOf(api.recommendationUrl) }
    var query by remember { mutableStateOf("") }
    var tracks by remember { mutableStateOf(emptyList<Track>()) }
    var recs by remember { mutableStateOf(emptyList<org.json.JSONObject>()) }
    var recMode by remember { mutableStateOf("recent_tracks") }
    var recQuery by remember { mutableStateOf("") }
    var recArtist by remember { mutableStateOf("") }
    var playlistName by remember { mutableStateOf("Рекомендации") }
    var recPeriod by remember { mutableStateOf("overall") }
    var enqueueRecommendations by remember { mutableStateOf(false) }
    var modeMenu by remember { mutableStateOf(false) }
    val recommendationModes = listOf(
        "recent_tracks" to "Похожие на недавние прослушивания",
        "top_tracks" to "Похожие на топ-треки",
        "loved_tracks" to "Похожие на любимые треки",
        "top_artists" to "Треки похожих исполнителей",
        "genre_tracks" to "Топ треков по жанру",
        "genre_artists" to "Исполнители по жанру",
        "similar_artist" to "Похожие исполнители",
        "similar_track" to "Похожие треки"
    )
    var playlists by remember { mutableStateOf(emptyList<ru.soulseek.player.data.RecPlaylist>()) }
    var status by remember { mutableStateOf("Настройте подключения к серверам") }
    var busy by remember { mutableStateOf(false) }
    var current by remember { mutableStateOf<Track?>(null) }
    val scope = rememberCoroutineScope()

    fun runTask(block: () -> Unit) {
        busy = true
        scope.launch {
            try { withContext(Dispatchers.IO) { block() } }
            catch (e: Exception) { status = e.message ?: "Ошибка запроса" }
            finally { busy = false }
        }
    }

    MaterialTheme {
        Scaffold(topBar = { TopAppBar(title = { Text("Soulseek Player") }) }) { pad ->
            Column(Modifier.padding(pad).padding(16.dp).fillMaxSize()) {
                TabRow(selectedTabIndex = tab) {
                    listOf("Музыка", "Рекомендации", "Настройки").forEachIndexed { i, label ->
                        Tab(selected = tab == i, onClick = { tab = i }, text = { Text(label) })
                    }
                }
                Spacer(Modifier.height(12.dp))
                when (tab) {
                    0 -> {
                        Row(Modifier.fillMaxWidth()) {
                            OutlinedTextField(query, { query = it }, Modifier.weight(1f), label = { Text("Поиск музыки") }, singleLine = true)
                            TextButton(onClick = { runTask { tracks = api.search(query); status = "Найдено: ${tracks.size}" } }, enabled = !busy && query.isNotBlank()) { Text("Найти") }
                        }
                        Row {
                            TextButton(onClick = { runTask { tracks = api.randomSongs(); status = "Случайные треки" } }, enabled = !busy) { Text("Случайное") }
                            TextButton(onClick = { runTask { tracks = api.starred(); status = "Избранное" } }, enabled = !busy) { Text("Избранное") }
                        }
                        TrackList(tracks, current) {
                            current = it
                            runCatching {
                                val player = mediaController ?: error("Медиасессия ещё подключается")
                                player.setMediaItem(MediaItem.fromUri(api.streamUrl(it)))
                                player.prepare()
                                player.play()
                                status = "Воспроизводится: ${it.title}"
                            }.onFailure { status = it.message ?: "Не удалось начать воспроизведение" }
                        }
                    }
                    1 -> {
                        Text("Рекомендации Last.fm / soulseek-queue")
                        Box {
                            OutlinedButton(onClick = { modeMenu = true }, modifier = Modifier.fillMaxWidth()) {
                                Text(recommendationModes.first { it.first == recMode }.second)
                            }
                            DropdownMenu(expanded = modeMenu, onDismissRequest = { modeMenu = false }) {
                                recommendationModes.forEach { (key, label) ->
                                    DropdownMenuItem(text = { Text(label) }, onClick = { recMode = key; modeMenu = false; recs = emptyList() })
                                }
                            }
                        }
                        if (recMode == "genre_tracks" || recMode == "genre_artists" || recMode == "similar_artist" || recMode == "similar_track") {
                            OutlinedTextField(recQuery, { recQuery = it }, Modifier.fillMaxWidth(), label = { Text(if (recMode.startsWith("genre")) "Жанр / тег" else if (recMode == "similar_track") "Название трека" else "Исполнитель") }, singleLine = true)
                        }
                        if (recMode == "similar_track") {
                            OutlinedTextField(recArtist, { recArtist = it }, Modifier.fillMaxWidth(), label = { Text("Исполнитель исходного трека") }, singleLine = true)
                        }
                        if (recMode == "top_tracks") {
                            Row(verticalAlignment = androidx.compose.ui.Alignment.CenterVertically) {
                                Text("Период:")
                                listOf("overall" to "Всё время", "7day" to "7 дней", "1month" to "Месяц", "3month" to "3 месяца", "6month" to "6 месяцев", "12month" to "Год").forEach { (key, label) ->
                                    TextButton(onClick = { recPeriod = key }) { Text(if (recPeriod == key) "[$label]" else label) }
                                }
                            }
                        }
                        OutlinedTextField(playlistName, { playlistName = it }, Modifier.fillMaxWidth(), label = { Text("Название плейлиста") }, singleLine = true)
                        Row(verticalAlignment = androidx.compose.ui.Alignment.CenterVertically) {
                            Checkbox(checked = enqueueRecommendations, onCheckedChange = { enqueueRecommendations = it })
                            Text("Сразу искать и скачивать")
                        }
                        val needsQuery = recMode == "genre_tracks" || recMode == "genre_artists" || recMode == "similar_artist" || recMode == "similar_track"
                        val validInput = (!needsQuery || recQuery.isNotBlank()) && (recMode != "similar_track" || recArtist.isNotBlank())
                        Row {
                            Button(onClick = {
                                runTask {
                                    recs = api.recommendations(recMode, 25, recQuery, recArtist, recPeriod)
                                    status = "Рекомендаций получено: " + recs.size
                                }
                            }, enabled = !busy && validInput) { Text("Получить") }
                            Spacer(Modifier.width(8.dp))
                            Button(onClick = {
                                runTask {
                                    val result = api.createPlaylist(recMode, playlistName, enqueueRecommendations, recQuery, recArtist, recPeriod)
                                    playlists = api.playlists()
                                    status = "Плейлист создан: " + result.optString("name") + "; треков: " + (result.optJSONArray("items")?.length() ?: 0)
                                }
                            }, enabled = !busy && validInput) { Text("Создать плейлист") }
                        }
                        Text("Результаты: " + recs.size)
                        LazyColumn(Modifier.weight(1f)) {
                            items(recs) { item ->
                                ListItem(
                                    headlineContent = { Text(item.optString("title").ifBlank { item.optString("name", "Без названия") }) },
                                    supportingContent = { Text(listOf(item.optString("artist"), item.optString("source")).filter { it.isNotBlank() }.joinToString(" • ")) }
                                )
                            }
                        }
                        Row(verticalAlignment = androidx.compose.ui.Alignment.CenterVertically) {
                            Text("Плейлисты сервера")
                            TextButton(onClick = { runTask { playlists = api.playlists(); status = "Плейлистов: " + playlists.size } }, enabled = !busy) { Text("Обновить") }
                        }
                        LazyColumn(Modifier.heightIn(max = 180.dp)) {
                            items(playlists) { item ->
                                ListItem(headlineContent = { Text(item.name) }, supportingContent = { Text("Рекомендаций: " + item.count + " • Скачано: " + item.downloadedCount) })
                            }
                        }
                    }
                    else -> {
                        Text("Navidrome / OpenSubsonic")
                        OutlinedTextField(navUrl, { navUrl = it }, Modifier.fillMaxWidth(), label = { Text("URL Navidrome") }, singleLine = true)
                        OutlinedTextField(user, { user = it }, Modifier.fillMaxWidth(), label = { Text("Пользователь") }, singleLine = true)
                        OutlinedTextField(pass, { pass = it }, Modifier.fillMaxWidth(), label = { Text("Пароль") }, singleLine = true)
                        Spacer(Modifier.height(12.dp))
                        Text("Рекомендательная система")
                        OutlinedTextField(recUrl, { recUrl = it }, Modifier.fillMaxWidth(), label = { Text("URL soulseek-queue (например http://host:8090)") }, singleLine = true)
                        Button(onClick = {
                            api.navUrl = navUrl.trim(); api.username = user; api.password = pass; api.recommendationUrl = recUrl.trim()
                            prefs.edit().putString("navUrl", api.navUrl).putString("username", user).putString("password", pass).putString("recommendationUrl", api.recommendationUrl).apply()
                            runTask { api.ping(); status = "Navidrome: подключение успешно" }
                        }, enabled = !busy) { Text("Сохранить и проверить Navidrome") }
                        Button(onClick = { api.recommendationUrl = recUrl.trim(); runTask { playlists = api.playlists(); status = "Soulseek Queue: ${playlists.size} плейлистов" } }, enabled = !busy) { Text("Проверить рекомендации") }
                        LazyColumn { items(playlists) { ListItem(headlineContent = { Text(it.name) }, supportingContent = { Text("Треков: ${it.count}") }) } }
                    }
                }
                Spacer(Modifier.height(8.dp))
                if (busy) LinearProgressIndicator(Modifier.fillMaxWidth())
                Text(status, style = MaterialTheme.typography.bodySmall)
                current?.let {
                    ListItem(
                        headlineContent = { Text("Сейчас играет: ${it.title}") },
                        supportingContent = { Text(it.artist) },
                        trailingContent = {
                            Row {
                                TextButton(onClick = { mediaController?.pause() }) { Text("Пауза") }
                                TextButton(onClick = { mediaController?.play() }) { Text("Играть") }
                            }
                        }
                    )
                }
            }
        }
    }
}

@Composable
private fun TrackList(tracks: List<Track>, current: Track?, onSelect: (Track) -> Unit) {
    LazyColumn(Modifier.fillMaxSize()) {
        items(tracks) { track ->
            ListItem(
                headlineContent = { Text(track.title) },
                supportingContent = { Text(listOf(track.artist, track.album).filter { it.isNotBlank() }.joinToString(" • ")) },
                modifier = Modifier.clickable { onSelect(track) }
            )
            HorizontalDivider()
        }
    }
}
