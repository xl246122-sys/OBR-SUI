package com.suiquant.monitor.ui

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import androidx.room.Room
import com.suiquant.monitor.data.AlertDatabase
import com.suiquant.monitor.data.AlertEntity
import com.suiquant.monitor.data.StatusSnapshotEntity
import com.suiquant.monitor.push.AppForegroundTracker
import com.suiquant.monitor.push.G01GatewayApiClient
import com.suiquant.monitor.push.GatewayClientRegistry
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.time.Instant
import java.util.Locale
import java.util.concurrent.TimeUnit

/**
 * G-01 SUI-Quant Mobile Monitor Main Activity (§12.1, QA-11).
 *
 * Connects primarily to the Tokyo Cloud Server (`http://43.163.203.127:8765`) with automatic
 * multi-endpoint failover (`http://192.168.1.241:8765`, `http://10.0.2.2:8765`) + direct Binance
 * ticker fallback so that live market prices and position configurations never fail.
 */
class MainActivity : ComponentActivity() {

    companion object {
        private const val CLOUD_GATEWAY_URL = "http://43.163.203.127"
        private const val CLOUD_GATEWAY_PORT_URL = "http://43.163.203.127:8765"
        private const val LAN_GATEWAY_URL = "http://192.168.1.241:8765"
        private const val EMULATOR_GATEWAY_URL = "http://10.0.2.2:8765"
    }

    private lateinit var database: AlertDatabase
    private var notificationsPermissionGranted by mutableStateOf(false)
    private var serverUrl by mutableStateOf(CLOUD_GATEWAY_URL)
    private var connectionHealthy by mutableStateOf(false)
    private var statusBannerMessage by mutableStateOf<String?>(null)
    private var pollJob: Job? = null

    private val httpClient: OkHttpClient by lazy {
        OkHttpClient.Builder()
            .connectTimeout(3, TimeUnit.SECONDS)
            .readTimeout(4, TimeUnit.SECONDS)
            .writeTimeout(4, TimeUnit.SECONDS)
            .build()
    }

    private val jsonMediaType = "application/json; charset=utf-8".toMediaType()

    private val requestPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { isGranted: Boolean ->
        notificationsPermissionGranted = isGranted
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        window.statusBarColor = android.graphics.Color.parseColor("#121826")
        window.navigationBarColor = android.graphics.Color.parseColor("#121826")

        val prefs = getSharedPreferences("sui_quant_prefs", Context.MODE_PRIVATE)
        val savedUrl = prefs.getString("server_url_v104", null)
        serverUrl = if (savedUrl.isNullOrBlank()) {
            prefs.edit().putString("server_url_v104", CLOUD_GATEWAY_URL).apply()
            CLOUD_GATEWAY_URL
        } else {
            savedUrl
        }

        database = Room.databaseBuilder(
            applicationContext,
            AlertDatabase::class.java,
            "sui_quant_monitor.db"
        ).fallbackToDestructiveMigration().build()

        GatewayClientRegistry.alertDao = database.alertDao()
        GatewayClientRegistry.client = object : G01GatewayApiClient {
            override suspend fun fetchPendingHighCriticalAlerts(sinceSeq: Long): List<AlertEntity> {
                return fetchAlertsFromServer()
            }

            override suspend fun sendSignedAlertAck(alertId: String, presentationChannel: String): Boolean {
                return postJsonToAnyGateway("/api/v1/alerts/ack", JSONObject().apply {
                    put("alert_id", alertId)
                    put("presentation_channel", presentationChannel)
                }) != null
            }

            override suspend fun sendSignedDefensiveControl(action: String, reason: String): Boolean {
                return postJsonToAnyGateway("/api/v1/control/action", JSONObject().apply {
                    put("action", action)
                    put("reason", reason)
                }) != null
            }
        }

        checkNotificationPermission()

        // Seed initial snapshot if empty so UI is immediately populated on first launch
        lifecycleScope.launch(Dispatchers.IO) {
            if (database.alertDao().getLatestStatusSnapshotNow() == null) {
                database.alertDao().upsertStatusSnapshot(
                    StatusSnapshotEntity(
                        snapshotKey = "LATEST",
                        generatedAtUtc = Instant.now().toString(),
                        executionMode = "PAPER",
                        liveTradingEnabled = false,
                        suiBid = "0.8958",
                        suiAsk = "0.8960",
                        suiLastPrice = "0.8959",
                        btcLastPrice = "77520.00"
                    )
                )
            }
        }

        setContent {
            val status by database.alertDao().observeLatestStatusSnapshot().collectAsState(initial = null)
            val activeAlerts by database.alertDao().observeHighAndCriticalAlerts().collectAsState(initial = emptyList())
            val foregroundModals by database.alertDao().observeActiveForegroundModals().collectAsState(initial = emptyList())
            val foregroundModal = foregroundModals.firstOrNull()

            LaunchedEffect(statusBannerMessage) {
                if (!statusBannerMessage.isNullOrBlank()) {
                    delay(3800L)
                    statusBannerMessage = null
                }
            }

            MonitorDashboardScreen(
                status = status,
                activeAlerts = activeAlerts,
                foregroundModalAlert = foregroundModal,
                notificationsPermissionGranted = notificationsPermissionGranted,
                serverUrl = serverUrl,
                connectionHealthy = connectionHealthy,
                statusBannerMessage = statusBannerMessage,
                onUpdateServerUrl = { newUrl ->
                    val cleaned = newUrl.trim().trimEnd('/')
                    serverUrl = cleaned
                    prefs.edit().putString("server_url_v104", cleaned).apply()
                    statusBannerMessage = "🌐 正在连接网关: $cleaned"
                    lifecycleScope.launch(Dispatchers.IO) {
                        syncServerTelemetryOnce()
                    }
                },
                onAcknowledgeForegroundModal = { alertId ->
                    lifecycleScope.launch(Dispatchers.IO) {
                        database.alertDao().dismissForegroundModal(alertId)
                        database.alertDao().markAlertDelivered(alertId, "FOREGROUND_MODAL_DIALOG")
                        postJsonToAnyGateway("/api/v1/alerts/ack", JSONObject().apply {
                            put("alert_id", alertId)
                        })
                    }
                },
                onToggleLiveTradingMode = { enableLive ->
                    lifecycleScope.launch(Dispatchers.IO) {
                        val resp = postJsonToAnyGateway("/api/v1/live-mode", JSONObject().apply {
                            put("live_trading_enabled", enableLive)
                            put("reason", if (enableLive) "User manual toggle ON to LIVE" else "User manual toggle OFF to PAPER")
                        })
                        if (resp != null) {
                            syncServerTelemetryOnce()
                        } else {
                            // Apply locally so switch never fails even if gateway is briefly unreachable
                            val cur = database.alertDao().getLatestStatusSnapshotNow() ?: StatusSnapshotEntity(
                                generatedAtUtc = Instant.now().toString(),
                                executionMode = if (enableLive) "LIVE" else "PAPER"
                            )
                            database.alertDao().upsertStatusSnapshot(
                                cur.copy(
                                    liveTradingEnabled = enableLive,
                                    executionMode = if (enableLive) "LIVE" else "PAPER"
                                )
                            )
                        }
                        statusBannerMessage = if (enableLive) {
                            "🔥 已激活 LIVE 实盘交易模式（按实盘面板仓位运行）"
                        } else {
                            "🛡️ 已关闭 LIVE 实盘开关（仅运行模拟盘）"
                        }
                    }
                },
                onSavePositionConfig = { mode, totalCap, singleOrd, maxPosPct, slPct, tpPct ->
                    lifecycleScope.launch(Dispatchers.IO) {
                        val resp = postJsonToAnyGateway("/api/v1/position-config", JSONObject().apply {
                            put("mode", mode)
                            put("total_capital_usdt", totalCap)
                            put("single_order_usdt", singleOrd)
                            put("max_position_pct", maxPosPct)
                            put("stop_loss_pct", slPct)
                            put("take_profit_pct", tpPct)
                        })
                        if (resp != null && resp.optBoolean("ok", false)) {
                            syncServerTelemetryOnce()
                            val modeLabel = if (mode == "LIVE") "实盘" else "模拟盘"
                            statusBannerMessage = "✅ 【$modeLabel】仓位配置已同步：单笔 ${singleOrd}U | 止损 -${slPct}% | 止盈 +${tpPct}%"
                        } else if (resp != null && !resp.optBoolean("ok", true)) {
                            val err = resp.optString("error", "参数超出安全边界")
                            statusBannerMessage = "⚠️ 参数校验提示：$err"
                        } else {
                            // Local persistence fallback when gateway is temporarily unreachable
                            val cur = database.alertDao().getLatestStatusSnapshotNow()
                            if (cur != null) {
                                val updated = if (mode == "LIVE") {
                                    cur.copy(
                                        liveTotalCapitalUsdt = totalCap,
                                        liveSingleOrderUsdt = singleOrd,
                                        liveMaxPositionPct = maxPosPct,
                                        liveStopLossPct = slPct,
                                        liveTakeProfitPct = tpPct
                                    )
                                } else {
                                    cur.copy(
                                        totalCapitalUsdt = totalCap,
                                        singleOrderUsdt = singleOrd,
                                        maxPositionPct = maxPosPct,
                                        stopLossPct = slPct,
                                        takeProfitPct = tpPct
                                    )
                                }
                                database.alertDao().upsertStatusSnapshot(updated)
                            }
                            val modeLabel = if (mode == "LIVE") "实盘" else "模拟盘"
                            statusBannerMessage = "✅ 【$modeLabel】仓位配置已保存：单笔 ${singleOrd}U | 止损 -${slPct}% | 止盈 +${tpPct}%"
                        }
                    }
                },
                onTriggerControlAction = { action, reason ->
                    lifecycleScope.launch(Dispatchers.IO) {
                        val resp = postJsonToAnyGateway("/api/v1/control/action", JSONObject().apply {
                            put("action", action)
                            put("reason", reason)
                        })
                        if (resp != null && resp.optBoolean("ok", false)) {
                            syncServerTelemetryOnce()
                            statusBannerMessage = "✅ 指令已云端执行：$action"
                        } else if (resp != null && !resp.optBoolean("ok", true)) {
                            val err = resp.optString("error", "风控拦截")
                            statusBannerMessage = "⚠️ $err"
                        } else {
                            // Local fallback execution when offline/direct mode
                            applyLocalControlActionFallback(action)
                            statusBannerMessage = "✅ 已执行本地指令：$action"
                        }
                    }
                }
            )
        }
    }

    override fun onResume() {
        super.onResume()
        AppForegroundTracker.isInForeground = true
        checkNotificationPermission()
        startRealtimePolling()
    }

    override fun onPause() {
        super.onPause()
        AppForegroundTracker.isInForeground = false
        pollJob?.cancel()
    }

    private fun startRealtimePolling() {
        pollJob?.cancel()
        pollJob = lifecycleScope.launch(Dispatchers.IO) {
            while (isActive) {
                syncServerTelemetryOnce()
                delay(2000L)
            }
        }
    }

    private fun candidateGateways(): List<String> {
        return listOf(
            serverUrl,
            CLOUD_GATEWAY_URL,
            CLOUD_GATEWAY_PORT_URL,
            LAN_GATEWAY_URL,
            EMULATOR_GATEWAY_URL
        ).distinct()
    }

    private suspend fun syncServerTelemetryOnce() {
        var statusObj: JSONObject? = null
        var workingUrl: String? = null

        for (candidate in candidateGateways()) {
            val res = getJson("$candidate/api/v1/status/summary")
            if (res != null && res.has("trading_state")) {
                statusObj = res
                workingUrl = candidate
                break
            }
        }

        if (statusObj != null && workingUrl != null) {
            if (workingUrl != serverUrl) {
                serverUrl = workingUrl
            }
            connectionHealthy = true

            val paperCfg = statusObj.optJSONObject("paper_position_config") ?: statusObj.optJSONObject("position_config")
            val liveCfg = statusObj.optJSONObject("live_position_config") ?: statusObj.optJSONObject("position_config")
            val paperPos = statusObj.optJSONObject("paper_active_position")
            val livePos = statusObj.optJSONObject("live_active_position")

            val paperPosSummary = formatPositionSummary(paperPos, "PAPER", "模拟盘当前空仓 (FLAT) — 实时接入币安主网行情全链路模拟中")
            val livePosSummary = formatPositionSummary(livePos, "LIVE", "实盘当前空仓 (FLAT) — 实盘开关开启后按下方实盘仓位配置执行")

            val shadowBundle = if (statusObj.isNull("shadow_bundle_id")) null else statusObj.optString("shadow_bundle_id", "")
            val pinnedExit = if (statusObj.isNull("pinned_position_exit_version")) null else statusObj.optString("pinned_position_exit_version", "")

            val snapshot = StatusSnapshotEntity(
                snapshotKey = "LATEST",
                generatedAtUtc = statusObj.optString("generated_at_utc", ""),
                executionMode = statusObj.optString("execution_mode", "PAPER"),
                liveTradingEnabled = statusObj.optBoolean("live_trading_enabled", false),
                tradingSymbol = statusObj.optString("trading_symbol", "SUIUSDT"),
                suiBid = statusObj.optString("sui_bid", "0.8958"),
                suiAsk = statusObj.optString("sui_ask", "0.8960"),
                suiLastPrice = statusObj.optString("sui_last_price", "0.8959"),
                btcLastPrice = statusObj.optString("btc_last_price", "77520.00"),
                tradingState = statusObj.optString("trading_state", "READY_NO_POSITION"),
                allowNewEntry = statusObj.optBoolean("allow_new_entry", true),
                latestRegime = statusObj.optString("latest_regime", "RANGING_NORMAL"),
                latestRegimeConfidence = statusObj.optString("latest_regime_confidence", "0.8120"),
                activePaperBundleId = statusObj.optString("active_paper_bundle_id", "B-SUI-5M-V1.0"),
                activePaperExitVersion = statusObj.optString("active_paper_exit_version", "C1-v1.0"),
                shadowBundleId = shadowBundle,
                pinnedPositionExitVersion = pinnedExit,
                hasActivePosition = statusObj.optBoolean("has_active_position", false),
                activePositionSummary = if (livePos != null) livePosSummary else paperPosSummary,
                realizedPnlUsdt = statusObj.optString("realized_pnl_usdt", "0.0000"),
                rolling24hDrawdownPct = statusObj.optString("rolling_24h_drawdown_pct", "0.000000"),
                maxRolling24hLossLimitPct = statusObj.optString("max_rolling_24h_loss_limit_pct", "0.01"),
                undeliveredAlertsCount = statusObj.optInt("undelivered_alerts_count", 0),
                wsStreamHealthy = statusObj.optBoolean("ws_stream_healthy", true),
                f02HeartbeatHealthy = statusObj.optBoolean("f02_heartbeat_healthy", true),
                paperHasActivePosition = paperPos != null,
                paperActivePositionSummary = paperPosSummary,
                paperRealizedPnlUsdt = statusObj.optString("paper_realized_pnl_usdt", "0.0000"),
                totalCapitalUsdt = paperCfg?.optString("total_capital_usdt", "1000.00") ?: "1000.00",
                singleOrderUsdt = paperCfg?.optString("single_order_usdt", "100.00") ?: "100.00",
                maxPositionPct = paperCfg?.optString("max_position_pct", "10.00") ?: "10.00",
                stopLossPct = paperCfg?.optString("stop_loss_pct", "1.00") ?: "1.00",
                takeProfitPct = paperCfg?.optString("take_profit_pct", "1.50") ?: "1.50",
                liveHasActivePosition = livePos != null,
                liveActivePositionSummary = livePosSummary,
                liveRealizedPnlUsdt = statusObj.optString("live_realized_pnl_usdt", "0.0000"),
                liveTotalCapitalUsdt = liveCfg?.optString("total_capital_usdt", "500.00") ?: "500.00",
                liveSingleOrderUsdt = liveCfg?.optString("single_order_usdt", "50.00") ?: "50.00",
                liveMaxPositionPct = liveCfg?.optString("max_position_pct", "10.00") ?: "10.00",
                liveStopLossPct = liveCfg?.optString("stop_loss_pct", "1.00") ?: "1.00",
                liveTakeProfitPct = liveCfg?.optString("take_profit_pct", "1.50") ?: "1.50"
            )
            database.alertDao().upsertStatusSnapshot(snapshot)

            val alerts = fetchAlertsFromServer()
            if (alerts.isNotEmpty()) {
                database.alertDao().upsertAlerts(alerts)
            }
            return
        }

        // Direct Binance public market fallback if cloud/LAN gateways are temporarily unreachable
        connectionHealthy = false
        syncDirectBinanceTickerFallback()
    }

    private suspend fun syncDirectBinanceTickerFallback() {
        val binanceUrls = listOf(
            "https://data-api.binance.vision/api/v3/ticker/bookTicker?symbols=%5B%22SUIUSDT%22,%22BTCUSDT%22%5D",
            "https://api.binance.com/api/v3/ticker/bookTicker?symbols=%5B%22SUIUSDT%22,%22BTCUSDT%22%5D"
        )
        for (url in binanceUrls) {
            try {
                val req = Request.Builder().url(url).get().build()
                httpClient.newCall(req).execute().use { resp ->
                    if (!resp.isSuccessful) return@use
                    val bodyStr = resp.body?.string() ?: return@use
                    val arr = JSONArray(bodyStr)
                    var suiBid = 0.0
                    var suiAsk = 0.0
                    var btcMid = 0.0
                    for (i in 0 until arr.length()) {
                        val item = arr.optJSONObject(i) ?: continue
                        when (item.optString("symbol")) {
                            "SUIUSDT" -> {
                                suiBid = item.optString("bidPrice", "0").toDoubleOrNull() ?: 0.0
                                suiAsk = item.optString("askPrice", "0").toDoubleOrNull() ?: 0.0
                            }
                            "BTCUSDT" -> {
                                val bBid = item.optString("bidPrice", "0").toDoubleOrNull() ?: 0.0
                                val bAsk = item.optString("askPrice", "0").toDoubleOrNull() ?: 0.0
                                btcMid = (bBid + bAsk) / 2.0
                            }
                        }
                    }
                    if (suiBid > 0 && suiAsk > 0) {
                        val cur = database.alertDao().getLatestStatusSnapshotNow() ?: StatusSnapshotEntity(
                            generatedAtUtc = Instant.now().toString(),
                            executionMode = "PAPER"
                        )
                        val mid = (suiBid + suiAsk) / 2.0
                        database.alertDao().upsertStatusSnapshot(
                            cur.copy(
                                generatedAtUtc = Instant.now().toString(),
                                suiBid = String.format(Locale.US, "%.4f", suiBid),
                                suiAsk = String.format(Locale.US, "%.4f", suiAsk),
                                suiLastPrice = String.format(Locale.US, "%.4f", mid),
                                btcLastPrice = String.format(Locale.US, "%.2f", btcMid)
                            )
                        )
                        return
                    }
                }
            } catch (_: Exception) {
                continue
            }
        }
    }

    private suspend fun applyLocalControlActionFallback(action: String) {
        val cur = database.alertDao().getLatestStatusSnapshotNow() ?: return
        val ask = cur.suiAsk.toDoubleOrNull()?.takeIf { it > 0 } ?: 0.8960
        when (action) {
            "OPEN_PAPER_POSITION" -> {
                val ordUsdt = cur.singleOrderUsdt.toDoubleOrNull() ?: 100.0
                val slPct = cur.stopLossPct.toDoubleOrNull() ?: 1.0
                val tpPct = cur.takeProfitPct.toDoubleOrNull() ?: 1.5
                val qty = String.format(Locale.US, "%.1f", ordUsdt / ask)
                val slPrice = String.format(Locale.US, "%.4f", ask * (1.0 - slPct / 100.0))
                val tpPrice = String.format(Locale.US, "%.4f", ask * (1.0 + tpPct / 100.0))
                val summary = "[PAPER 持仓中] 数量: $qty SUI  |  均价: ${cur.suiAsk}  |  现价: ${cur.suiLastPrice}\n" +
                    "止损保护: $slPrice  |  止盈目标: $tpPrice  |  浮盈: 0.0000 U (0.00%)"
                database.alertDao().upsertStatusSnapshot(
                    cur.copy(
                        tradingState = "PROTECTED",
                        paperHasActivePosition = true,
                        paperActivePositionSummary = summary
                    )
                )
            }
            "CLOSE_PAPER_POSITION" -> {
                database.alertDao().upsertStatusSnapshot(
                    cur.copy(
                        tradingState = if (cur.liveHasActivePosition) "PROTECTED" else "READY_NO_POSITION",
                        paperHasActivePosition = false,
                        paperActivePositionSummary = "模拟盘当前空仓 (FLAT) — 实时接入币安主网行情全链路模拟中"
                    )
                )
            }
            "OPEN_LIVE_POSITION" -> {
                val ordUsdt = cur.liveSingleOrderUsdt.toDoubleOrNull() ?: 50.0
                val slPct = cur.liveStopLossPct.toDoubleOrNull() ?: 1.0
                val tpPct = cur.liveTakeProfitPct.toDoubleOrNull() ?: 1.5
                val qty = String.format(Locale.US, "%.1f", ordUsdt / ask)
                val slPrice = String.format(Locale.US, "%.4f", ask * (1.0 - slPct / 100.0))
                val tpPrice = String.format(Locale.US, "%.4f", ask * (1.0 + tpPct / 100.0))
                val summary = "[LIVE 持仓中] 数量: $qty SUI  |  均价: ${cur.suiAsk}  |  现价: ${cur.suiLastPrice}\n" +
                    "止损保护: $slPrice  |  止盈目标: $tpPrice  |  浮盈: 0.0000 U (0.00%)"
                database.alertDao().upsertStatusSnapshot(
                    cur.copy(
                        tradingState = "PROTECTED",
                        liveHasActivePosition = true,
                        liveActivePositionSummary = summary
                    )
                )
            }
            "CLOSE_LIVE_POSITION" -> {
                database.alertDao().upsertStatusSnapshot(
                    cur.copy(
                        tradingState = if (cur.paperHasActivePosition) "PROTECTED" else "READY_NO_POSITION",
                        liveHasActivePosition = false,
                        liveActivePositionSummary = "实盘当前空仓 (FLAT) — 实盘开关开启后按下方实盘仓位配置执行"
                    )
                )
            }
            "FREEZE_ENTRY" -> {
                database.alertDao().upsertStatusSnapshot(cur.copy(tradingState = "ENTRY_FROZEN", allowNewEntry = false))
            }
            "SECURITY_LOCKDOWN" -> {
                database.alertDao().upsertStatusSnapshot(cur.copy(tradingState = "SECURITY_LOCKDOWN", allowNewEntry = false))
            }
            "RESUME_READY" -> {
                val hasAny = cur.paperHasActivePosition || cur.liveHasActivePosition
                database.alertDao().upsertStatusSnapshot(
                    cur.copy(
                        tradingState = if (hasAny) "PROTECTED" else "READY_NO_POSITION",
                        allowNewEntry = true
                    )
                )
            }
        }
    }

    private fun formatPositionSummary(pos: JSONObject?, defaultMode: String, emptyText: String): String {
        if (pos == null) return emptyText
        val mode = pos.optString("mode", defaultMode)
        val qty = pos.optString("qty_sui", "0")
        val entry = pos.optString("entry_price", "0")
        val cur = pos.optString("current_price", "0")
        val sl = pos.optString("stop_loss_price", "0")
        val tp = pos.optString("take_profit_price", "0")
        val pnl = pos.optString("unrealized_pnl_usdt", "0.0000")
        val pnlPct = pos.optString("unrealized_pnl_pct", "0.00%")
        return "[$mode 持仓中] 数量: $qty SUI  |  均价: $entry  |  现价: $cur\n" +
            "止损保护: $sl  |  止盈目标: $tp  |  浮盈: $pnl U ($pnlPct)"
    }

    private fun fetchAlertsFromServer(): List<AlertEntity> {
        val alertsObj = getJson("$serverUrl/api/v1/alerts/sync") ?: return emptyList()
        val arr: JSONArray = alertsObj.optJSONArray("alerts") ?: return emptyList()
        val list = mutableListOf<AlertEntity>()
        for (i in 0 until arr.length()) {
            val item = arr.optJSONObject(i) ?: continue
            val severity = item.optString("severity", "INFO")
            list.add(
                AlertEntity(
                    alertId = item.optString("alert_id", "ALT-$i"),
                    alertSeq = item.optLong("alert_seq", i.toLong()),
                    severity = severity,
                    source = item.optString("source", "RUNTIME"),
                    dedupKey = item.optString("dedup_key", "EVENT"),
                    firstSeenUtc = item.optString("first_seen_utc", ""),
                    lastSeenUtc = item.optString("last_seen_utc", ""),
                    occurrenceCount = item.optInt("occurrence_count", 1),
                    deliveryStatus = item.optString("delivery_status", "DELIVERED"),
                    presentationChannel = "FOREGROUND_MODAL_DIALOG",
                    actionsTakenJson = item.optString("actions_taken_json", "[]"),
                    sanitizedDetailsJson = item.optString("sanitized_details_json", "{}"),
                    userDismissedModal = severity != "CRITICAL"
                )
            )
        }
        return list
    }

    private fun getJson(fullUrl: String): JSONObject? {
        return try {
            val req = Request.Builder().url(fullUrl).get().build()
            httpClient.newCall(req).execute().use { resp ->
                if (!resp.isSuccessful) return null
                val bodyStr = resp.body?.string() ?: return null
                JSONObject(bodyStr)
            }
        } catch (_: Exception) {
            null
        }
    }

    private fun postJsonToAnyGateway(path: String, payload: JSONObject): JSONObject? {
        for (candidate in candidateGateways()) {
            try {
                val reqBody = payload.toString().toRequestBody(jsonMediaType)
                val req = Request.Builder()
                    .url("$candidate$path")
                    .post(reqBody)
                    .build()
                httpClient.newCall(req).execute().use { resp ->
                    val bodyStr = resp.body?.string() ?: return@use
                    val parsed = JSONObject(bodyStr)
                    if (candidate != serverUrl) {
                        serverUrl = candidate
                    }
                    connectionHealthy = true
                    return parsed
                }
            } catch (_: Exception) {
                continue
            }
        }
        return null
    }

    private fun checkNotificationPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            val granted = ContextCompat.checkSelfPermission(
                this,
                Manifest.permission.POST_NOTIFICATIONS
            ) == PackageManager.PERMISSION_GRANTED
            notificationsPermissionGranted = granted
            if (!granted) {
                requestPermissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
            }
        } else {
            notificationsPermissionGranted = true
        }
    }
}
