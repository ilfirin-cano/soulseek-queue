package ru.soulseek.player

import android.content.ComponentName
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.media3.session.MediaController
import androidx.media3.session.SessionToken
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
        val prefs = getSharedPreferences("settings", MODE_PRIVATE)
        api.navUrl = prefs.getString("navUrl", "") ?: ""
        api.username = prefs.getString("username", "") ?: ""
        api.password = prefs.getString("password", "") ?: ""
        api.recommendationUrl = prefs.getString("recommendationUrl", "") ?: ""
        setContent { PlayerApp(api, prefs) }
    }
}

@Composable
private fun PlayerApp(api: Api, prefs: android.content.SharedPreferences) {
    var tab by remember { mutableStateOf(0) }
    var navUrl by remember { mutableStateOf(api.navUrl) }
    var user by remember { mutableStateOf(api.username) }
    var pass by remember { mutableStateOf(api.password) }
    var recUrl by remember { mutableStateOf(api.recommendationUrl) }
    var query by remember { mutableStateOf("") }
    var tracks by remember { mutableStateOf(emptyList<Track>()) }
    var recs by remember { mutableStateOf(emptyList<org.json.JSONObject>()) }
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
                        TrackList(tracks, current) { current = it; status = "Выбран: ${it.title}. Подключение плеера будет выполнено через MediaSession." }
                    }
                    1 -> {
                        Text("Персональные рекомендации soulseek-queue")
                        Row {
                            TextButton(onClick = { runTask { recs = api.recommendations("recent_tracks"); status = "Рекомендации обновлены: ${recs.size}" } }, enabled = !busy) { Text("Недавние") }
                            TextButton(onClick = { runTask { recs = api.recommendations("top_tracks"); status = "Топ-треки: ${recs.size}" } }, enabled = !busy) { Text("Топ-треки") }
                            TextButton(onClick = { runTask { recs = api.recommendations("loved_tracks"); status = "Избранное: ${recs.size}" } }, enabled = !busy) { Text("Любимые") }
                        }
                        OutlinedTextField("Мои рекомендации", {}, Modifier.fillMaxWidth(), label = { Text("Название плейлиста") }, readOnly = true)
                        TextButton(onClick = { runTask { val result = api.createPlaylist("recent_tracks", "Рекомендации", false); playlists = api.playlists(); status = "Плейлист создан: ${result.optString("playlist_id")}" } }, enabled = !busy) { Text("Создать плейлист") }
                        LazyColumn { items(recs) { item -> ListItem(headlineContent = { Text(item.optString("name", item.optString("title", "Трек"))) }, supportingContent = { Text(item.optString("artist")) }) } }
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
                current?.let { ListItem(headlineContent = { Text("Выбранный трек: ${it.title}") }, supportingContent = { Text(it.artist) }) }
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
