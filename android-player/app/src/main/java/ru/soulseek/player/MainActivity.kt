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
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.MediaMetadata
import androidx.media3.common.Player
import androidx.media3.session.MediaController
import androidx.media3.session.SessionToken
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import ru.soulseek.player.data.Api
import ru.soulseek.player.data.RecPlaylist
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

    var tab by remember { mutableIntStateOf(0) }
    var navUrl by remember { mutableStateOf(api.navUrl) }
    var user by remember { mutableStateOf(api.username) }
    var pass by remember { mutableStateOf(api.password) }
    var recUrl by remember { mutableStateOf(api.recommendationUrl) }
    var query by remember { mutableStateOf("") }
    var tracks by remember { mutableStateOf(emptyList<Track>()) }
    var recs by remember { mutableStateOf(emptyList<JSONObject>()) }
    var recMode by remember { mutableStateOf("recent_tracks") }
    var recQuery by remember { mutableStateOf("") }
    var recArtist by remember { mutableStateOf("") }
    var playlistName by remember { mutableStateOf("Рекомендации") }
    var recPeriod by remember { mutableStateOf("overall") }
    var enqueueRecommendations by remember { mutableStateOf(false) }
    var modeMenu by remember { mutableStateOf(false) }
    var playlists by remember { mutableStateOf(emptyList<RecPlaylist>()) }
    var activePlaylistId by remember { mutableStateOf<String?>(null) }
    var activePlaylistName by remember { mutableStateOf("") }
    var playlistDetail by remember { mutableStateOf<JSONObject?>(null) }
    var playlistTracks by remember { mutableStateOf(emptyList<Track>()) }
    var playbackQueue by remember { mutableStateOf(emptyList<Track>()) }
    var current by remember { mutableStateOf<Track?>(null) }
    var status by remember { mutableStateOf("Настройте подключения к серверам") }
    var busy by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()
    val latestQueue by rememberUpdatedState(playbackQueue)

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

    fun runTask(block: () -> Unit) {
        busy = true
        scope.launch {
            try { withContext(Dispatchers.IO) { block() } }
            catch (e: Exception) { status = e.message ?: "Ошибка запроса" }
            finally { busy = false }
        }
    }

    fun mediaItem(track: Track): MediaItem = MediaItem.Builder()
        .setMediaId(track.id)
        .setUri(api.streamUrl(track))
        .setMediaMetadata(MediaMetadata.Builder().setTitle(track.title).setArtist(track.artist).setAlbumTitle(track.album).build())
        .build()

    fun playTracks(list: List<Track>, index: Int) {
        if (list.isEmpty()) { status = "В плейлисте пока нет доступных в Navidrome треков"; return }
        val player = mediaController ?: run { status = "Медиасессия ещё подключается"; return }
        val safeIndex = index.coerceIn(0, list.lastIndex)
        playbackQueue = list
        current = list[safeIndex]
        player.setMediaItems(list.map(::mediaItem), safeIndex, C.TIME_UNSET)
        player.prepare()
        player.play()
        status = "Воспроизводится: ${list[safeIndex].title}"
    }

    fun openPlaylist(id: String, name: String) {
        activePlaylistId = id
        activePlaylistName = name
        tab = 0
        runTask {
            val detail = api.playlistDetail(id)
            val downloaded = api.playlistTracks(detail)
            playlistDetail = detail
            playlistTracks = downloaded
            status = "Открыт плейлист «$name»: доступно треков — ${downloaded.size}"
        }
    }

    // Сверка с soulseek-queue оставляет экран плейлиста актуальным во время загрузок.
    // Новые скачанные треки добавляются в конец уже играющей очереди без перезапуска текущего трека.
    LaunchedEffect(activePlaylistId, mediaController) {
        val id = activePlaylistId ?: return@LaunchedEffect
        while (true) {
            delay(7000)
            runCatching {
                val detail = withContext(Dispatchers.IO) { api.playlistDetail(id) }
                val downloadedCount = detail.optJSONArray("downloaded_items")?.length()
                    ?: detail.optJSONArray("downloadedItems")?.length() ?: 0
                if (downloadedCount != playlistTracks.size || playlistTracks.isEmpty()) {
                    val freshTracks = withContext(Dispatchers.IO) { api.playlistTracks(detail) }
                    val oldIds = playlistTracks.map { it.id }
                    val newIds = freshTracks.map { it.id }
                    if (newIds != oldIds) {
                        val queueIds = latestQueue.map { it.id }
                        if (newIds.size >= oldIds.size && newIds.take(oldIds.size) == oldIds && queueIds == oldIds) {
                            val added = freshTracks.drop(oldIds.size)
                            if (added.isNotEmpty()) mediaController?.addMediaItems(oldIds.size, added.map(::mediaItem))
                        } else if (queueIds == oldIds && freshTracks.isNotEmpty()) {
                            val wasPlaying = mediaController?.isPlaying == true
                            val currentId = mediaController?.currentMediaItem?.mediaId
                            val newIndex = freshTracks.indexOfFirst { it.id == currentId }.coerceAtLeast(0)
                            mediaController?.setMediaItems(freshTracks.map(::mediaItem), newIndex, C.TIME_UNSET)
                            mediaController?.prepare()
                            if (wasPlaying) mediaController?.play()
                        }
                        playlistTracks = freshTracks
                    }
                }
                playlistDetail = detail
                if (downloadedCount > 0 && playlistTracks.isEmpty()) status = "Файлы скачаны; ожидаю индексацию Navidrome"
            }
        }
    }

    DisposableEffect(mediaController) {
        val listener = object : Player.Listener {
            override fun onMediaItemTransition(mediaItem: MediaItem?, reason: Int) {
                current = latestQueue.firstOrNull { it.id == mediaItem?.mediaId }
            }
        }
        mediaController?.addListener(listener)
        onDispose { mediaController?.removeListener(listener) }
    }

    MaterialTheme {
        Scaffold(topBar = { TopAppBar(title = { Text("Soulseek Player") }) }) { pad ->
            Column(Modifier.padding(pad).padding(horizontal = 14.dp, vertical = 8.dp).fillMaxSize()) {
                TabRow(selectedTabIndex = tab) {
                    listOf("Музыка", "Рекомендации", "Настройки").forEachIndexed { i, label ->
                        Tab(selected = tab == i, onClick = { tab = i }, text = { Text(label) })
                    }
                }
                Spacer(Modifier.height(8.dp))
                when (tab) {
                    0 -> {
                        if (activePlaylistId != null) {
                            Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
                                TextButton(onClick = { activePlaylistId = null; playlistDetail = null; playlistTracks = emptyList() }) { Text("← Назад") }
                                Text(activePlaylistName, style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
                                TextButton(onClick = { runTask { val d = api.playlistDetail(activePlaylistId!!); playlistDetail = d; playlistTracks = api.playlistTracks(d); status = "Плейлист обновлён" } }, enabled = !busy) { Text("Обновить") }
                            }
                            val recommended = playlistDetail?.optJSONArray("items")?.asObjects().orEmpty()
                            Text("Скачано и доступно: ${playlistTracks.size} • Рекомендаций: ${recommended.size}", style = MaterialTheme.typography.bodySmall)
                            if (playlistTracks.isNotEmpty()) {
                                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                    Button(onClick = { playTracks(playlistTracks, 0) }) { Text("Играть всё") }
                                    OutlinedButton(onClick = { mediaController?.seekToPreviousMediaItem() }) { Text("Предыдущий") }
                                    OutlinedButton(onClick = { if (mediaController?.isPlaying == true) mediaController?.pause() else mediaController?.play() }) { Text(if (mediaController?.isPlaying == true) "Пауза" else "Продолжить") }
                                    OutlinedButton(onClick = { mediaController?.seekToNextMediaItem() }) { Text("Следующий") }
                                }
                            }
                            LazyColumn(Modifier.weight(1f)) {
                                if (playlistTracks.isNotEmpty()) {
                                    items(playlistTracks, key = { it.id }) { track ->
                                        ListItem(
                                            headlineContent = { Text(track.title) },
                                            supportingContent = { Text(listOf(track.artist, track.album).filter { it.isNotBlank() }.joinToString(" • ")) },
                                            trailingContent = { Text(if (current?.id == track.id) "▶" else "") },
                                            modifier = Modifier.clickable { playTracks(playlistTracks, playlistTracks.indexOf(track)) }
                                        )
                                        HorizontalDivider()
                                    }
                                } else {
                                    item { Text(if (recommended.isNotEmpty()) "Пока нет скачанных файлов. Список обновится автоматически по мере загрузки." else "В этом плейлисте пока нет треков.", modifier = Modifier.padding(16.dp)) }
                                }
                                if (recommended.isNotEmpty()) {
                                    item { Text("Рекомендации и состояние загрузки", style = MaterialTheme.typography.titleSmall, modifier = Modifier.padding(top = 12.dp, bottom = 4.dp)) }
                                    items(recommended) { item ->
                                        val title = item.optString("title").ifBlank { item.optString("name", "Без названия") }
                                        ListItem(
                                            headlineContent = { Text(title) },
                                            supportingContent = { Text(listOf(item.optString("artist"), item.optString("status")).filter { it.isNotBlank() }.joinToString(" • ")) }
                                        )
                                    }
                                }
                            }
                        } else {
                            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                                OutlinedTextField(query, { query = it }, Modifier.weight(1f), label = { Text("Поиск в Navidrome") }, singleLine = true)
                                TextButton(onClick = { runTask { tracks = api.search(query); status = "Найдено: ${tracks.size}" } }, enabled = !busy && query.isNotBlank()) { Text("Найти") }
                            }
                            Row {
                                TextButton(onClick = { runTask { tracks = api.randomSongs(); status = "Случайные треки" } }, enabled = !busy) { Text("Случайное") }
                                TextButton(onClick = { runTask { tracks = api.starred(); status = "Избранное" } }, enabled = !busy) { Text("Избранное") }
                                TextButton(onClick = { runTask { playlists = api.playlists(); status = "Плейлистов: ${playlists.size}" } }, enabled = !busy) { Text("Мои плейлисты") }
                            }
                            LazyColumn(Modifier.weight(1f)) {
                                items(tracks, key = { it.id }) { track ->
                                    ListItem(
                                        headlineContent = { Text(track.title) },
                                        supportingContent = { Text(listOf(track.artist, track.album).filter { it.isNotBlank() }.joinToString(" • ")) },
                                        modifier = Modifier.clickable { playTracks(tracks, tracks.indexOf(track)) }
                                    )
                                    HorizontalDivider()
                                }
                                if (playlists.isNotEmpty()) {
                                    item { Text("Плейлисты soulseek-queue", style = MaterialTheme.typography.titleSmall, modifier = Modifier.padding(vertical = 8.dp)) }
                                    items(playlists, key = { "pl_${it.id}" }) { playlist ->
                                        ListItem(
                                            headlineContent = { Text(playlist.name) },
                                            supportingContent = { Text("Рекомендаций: ${playlist.count} • Скачано: ${playlist.downloadedCount}") },
                                            modifier = Modifier.clickable { openPlaylist(playlist.id, playlist.name) }
                                        )
                                    }
                                }
                            }
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
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Text("Период:")
                                listOf("overall" to "Всё время", "7day" to "7 дней", "1month" to "Месяц", "3month" to "3 месяца", "6month" to "6 месяцев", "12month" to "Год").forEach { (key, label) ->
                                    TextButton(onClick = { recPeriod = key }) { Text(if (recPeriod == key) "[$label]" else label) }
                                }
                            }
                        }
                        OutlinedTextField(playlistName, { playlistName = it }, Modifier.fillMaxWidth(), label = { Text("Название плейлиста") }, singleLine = true)
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Checkbox(checked = enqueueRecommendations, onCheckedChange = { enqueueRecommendations = it })
                            Text("Сразу искать и скачивать")
                        }
                        val needsQuery = recMode == "genre_tracks" || recMode == "genre_artists" || recMode == "similar_artist" || recMode == "similar_track"
                        val validInput = (!needsQuery || recQuery.isNotBlank()) && (recMode != "similar_track" || recArtist.isNotBlank())
                        Row {
                            Button(onClick = {
                                runTask {
                                    recs = api.recommendations(recMode, 25, recQuery, recArtist, recPeriod)
                                    status = "Рекомендаций получено: ${recs.size}"
                                }
                            }, enabled = !busy && validInput) { Text("Получить") }
                            Spacer(Modifier.width(8.dp))
                            Button(onClick = {
                                runTask {
                                    val result = api.createPlaylist(recMode, playlistName, enqueueRecommendations, recQuery, recArtist, recPeriod)
                                    val id = result.optString("playlist_id")
                                    val name = result.optString("name").ifBlank { playlistName }
                                    playlists = api.playlists()
                                    val detail = api.playlistDetail(id)
                                    playlistDetail = detail
                                    playlistTracks = api.playlistTracks(detail)
                                    activePlaylistId = id
                                    activePlaylistName = name
                                    tab = 0
                                    status = "Плейлист создан и открыт: $name"
                                }
                            }, enabled = !busy && validInput) { Text("Создать и открыть") }
                        }
                        Text("Результаты: ${recs.size}")
                        LazyColumn(Modifier.weight(1f)) {
                            items(recs) { item ->
                                ListItem(
                                    headlineContent = { Text(item.optString("title").ifBlank { item.optString("name", "Без названия") }) },
                                    supportingContent = { Text(listOf(item.optString("artist"), item.optString("source")).filter { it.isNotBlank() }.joinToString(" • ")) }
                                )
                            }
                        }
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Text("Плейлисты сервера")
                            TextButton(onClick = { runTask { playlists = api.playlists(); status = "Плейлистов: ${playlists.size}" } }, enabled = !busy) { Text("Обновить") }
                        }
                        LazyColumn(Modifier.heightIn(max = 180.dp)) {
                            items(playlists, key = { it.id }) { item ->
                                ListItem(
                                    headlineContent = { Text(item.name) },
                                    supportingContent = { Text("Рекомендаций: ${item.count} • Скачано: ${item.downloadedCount}") },
                                    modifier = Modifier.clickable { openPlaylist(item.id, item.name) }
                                )
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
                        Button(onClick = {
                            api.recommendationUrl = recUrl.trim()
                            runTask { playlists = api.playlists(); status = "Soulseek Queue: ${playlists.size} плейлистов" }
                        }, enabled = !busy) { Text("Проверить рекомендации") }
                    }
                }
                Spacer(Modifier.height(6.dp))
                if (busy) LinearProgressIndicator(Modifier.fillMaxWidth())
                Text(status, style = MaterialTheme.typography.bodySmall)
                current?.let { playing ->
                    ListItem(
                        headlineContent = { Text("Сейчас играет: ${playing.title}") },
                        supportingContent = { Text(playing.artist) },
                        trailingContent = {
                            Row {
                                TextButton(onClick = { mediaController?.seekToPreviousMediaItem() }) { Text("Назад") }
                                TextButton(onClick = { if (mediaController?.isPlaying == true) mediaController?.pause() else mediaController?.play() }) { Text(if (mediaController?.isPlaying == true) "Пауза" else "Играть") }
                                TextButton(onClick = { mediaController?.seekToNextMediaItem() }) { Text("Далее") }
                            }
                        }
                    )
                }
            }
        }
    }
}

private fun JSONArray.asObjects(): List<JSONObject> = (0 until length()).mapNotNull { optJSONObject(it) }
