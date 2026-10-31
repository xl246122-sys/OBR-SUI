package com.suiquant.monitor.ui

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Slider
import androidx.compose.material3.SliderDefaults
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.SwitchDefaults
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.suiquant.monitor.data.AlertEntity
import com.suiquant.monitor.data.StatusSnapshotEntity
import java.util.Locale
import kotlin.math.max
import kotlin.math.min

// ============================================================================
// Institutional Quant Fintech Color System (Google Material 3 Dark Palette)
// ============================================================================
private val BgObsidian = Color(0xFF0A0E17)
private val SurfaceCard = Color(0xFF121826)
private val SurfaceElevated = Color(0xFF1A2234)
private val BorderSubtle = Color(0xFF26324B)
private val TextPrimary = Color(0xFFF8FAFC)
private val TextSecondary = Color(0xFF94A3B8)
private val TextMuted = Color(0xFF64748B)
private val AccentCyan = Color(0xFF38BDF8)
private val AccentEmerald = Color(0xFF10B981)
private val AccentEmeraldDark = Color(0xFF064E3B)
private val AccentCrimson = Color(0xFFEF4444)
private val AccentCrimsonDark = Color(0xFF7F1D1D)
private val AccentAmber = Color(0xFFF59E0B)
private val AccentAmberDark = Color(0xFF78350F)

/**
 * Institutional-Grade Quant Terminal UI (Refactored V1.04).
 *
 * 4 Dedicated Bottom Navigation Tabs:
 * 0 -> 🧪 模拟盘 (PAPER Simulation Workspace + Integrated Paper Position & Risk Config)
 * 1 -> 🔥 实盘交易 (LIVE Trading Workspace + Default-OFF Master Switch + Integrated Live Position Config)
 * 2 -> 🛡️ 风控演练 (D-01 State Machine, Emergency Defense Matrix & E-01 Model Registry)
 * 3 -> 🔔 审计网关 (Filtered G-02 Alert Stream & Cloud/LAN Gateway Switcher)
 */
@Composable
fun MonitorDashboardScreen(
    status: StatusSnapshotEntity?,
    activeAlerts: List<AlertEntity>,
    foregroundModalAlert: AlertEntity?,
    notificationsPermissionGranted: Boolean,
    serverUrl: String,
    connectionHealthy: Boolean,
    statusBannerMessage: String?,
    onUpdateServerUrl: (String) -> Unit,
    onAcknowledgeForegroundModal: (String) -> Unit,
    onToggleLiveTradingMode: (Boolean) -> Unit,
    onSavePositionConfig: (
        mode: String,
        totalCapitalUsdt: String,
        singleOrderUsdt: String,
        maxPositionPct: String,
        stopLossPct: String,
        takeProfitPct: String
    ) -> Unit,
    onTriggerControlAction: (action: String, reason: String) -> Unit
) {
    var selectedTab by remember { mutableIntStateOf(0) }
    var pendingDefensiveAction by remember { mutableStateOf<String?>(null) }
    var showLiveEnableConfirmDialog by remember { mutableStateOf(false) }

    // 1. PAPER Workspace Position Config State
    var paperTotalCapital by remember { mutableStateOf(status?.totalCapitalUsdt ?: "1000.00") }
    var paperSingleOrder by remember { mutableStateOf(status?.singleOrderUsdt ?: "100.00") }
    var paperMaxPosPct by remember { mutableStateOf(status?.maxPositionPct ?: "10.00") }
    var paperStopLossPct by remember { mutableStateOf(status?.stopLossPct ?: "1.00") }
    var paperTakeProfitPct by remember { mutableStateOf(status?.takeProfitPct ?: "1.50") }
    var paperConfigLoaded by remember { mutableStateOf(false) }

    // 2. LIVE Workspace Position Config State
    var liveTotalCapital by remember { mutableStateOf(status?.liveTotalCapitalUsdt ?: "500.00") }
    var liveSingleOrder by remember { mutableStateOf(status?.liveSingleOrderUsdt ?: "50.00") }
    var liveMaxPosPct by remember { mutableStateOf(status?.liveMaxPositionPct ?: "10.00") }
    var liveStopLossPct by remember { mutableStateOf(status?.liveStopLossPct ?: "1.00") }
    var liveTakeProfitPct by remember { mutableStateOf(status?.liveTakeProfitPct ?: "1.50") }
    var liveConfigLoaded by remember { mutableStateOf(false) }

    LaunchedEffect(status?.totalCapitalUsdt, status?.singleOrderUsdt) {
        if (status != null && !paperConfigLoaded) {
            paperTotalCapital = status.totalCapitalUsdt
            paperSingleOrder = status.singleOrderUsdt
            paperMaxPosPct = status.maxPositionPct
            paperStopLossPct = status.stopLossPct
            paperTakeProfitPct = status.takeProfitPct
            paperConfigLoaded = true
        }
    }

    LaunchedEffect(status?.liveTotalCapitalUsdt, status?.liveSingleOrderUsdt) {
        if (status != null && !liveConfigLoaded) {
            liveTotalCapital = status.liveTotalCapitalUsdt
            liveSingleOrder = status.liveSingleOrderUsdt
            liveMaxPosPct = status.liveMaxPositionPct
            liveStopLossPct = status.liveStopLossPct
            liveTakeProfitPct = status.liveTakeProfitPct
            liveConfigLoaded = true
        }
    }

    val isLiveMode = status?.liveTradingEnabled == true || status?.executionMode == "LIVE"
    val criticalCount = activeAlerts.count { it.severity == "CRITICAL" || it.severity == "HIGH" }

    // ------------------------------------------------------------------------
    // 1. Critical Alert Foreground Modal Dialog
    // ------------------------------------------------------------------------
    if (foregroundModalAlert != null) {
        AlertDialog(
            onDismissRequest = { /* Explicit operator ACK required */ },
            containerColor = SurfaceElevated,
            titleContentColor = TextPrimary,
            textContentColor = TextSecondary,
            title = {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    StatusPill(
                        text = foregroundModalAlert.severity,
                        bgColor = if (foregroundModalAlert.severity == "CRITICAL") AccentCrimson else AccentAmber,
                        textColor = Color.White
                    )
                    Spacer(modifier = Modifier.width(8.dp))
                    Text(
                        text = foregroundModalAlert.source,
                        fontWeight = FontWeight.Bold,
                        fontSize = 16.sp
                    )
                }
            },
            text = {
                Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    Text("告警编号: ${foregroundModalAlert.alertId}", fontFamily = FontFamily.Monospace, fontSize = 12.sp)
                    Text("事件标识: ${foregroundModalAlert.dedupKey}", color = TextPrimary, fontWeight = FontWeight.SemiBold)
                    Text("触发时间: ${foregroundModalAlert.firstSeenUtc}", fontSize = 12.sp)
                    Text("保护动作: ${foregroundModalAlert.actionsTakenJson}", color = AccentEmerald, fontSize = 12.sp)
                    Text("脱敏载荷: ${foregroundModalAlert.sanitizedDetailsJson}", fontFamily = FontFamily.Monospace, fontSize = 11.sp)
                }
            },
            confirmButton = {
                Button(
                    colors = ButtonDefaults.buttonColors(containerColor = AccentCyan, contentColor = BgObsidian),
                    onClick = { onAcknowledgeForegroundModal(foregroundModalAlert.alertId) }
                ) {
                    Text("确认知晓并签发 ACK", fontWeight = FontWeight.Bold)
                }
            }
        )
    }

    // ------------------------------------------------------------------------
    // 2. LIVE Real-Money Mode Activation Safety Dialog
    // ------------------------------------------------------------------------
    if (showLiveEnableConfirmDialog) {
        val totalCapVal = liveTotalCapital.toDoubleOrNull() ?: 500.0
        val singleOrdVal = liveSingleOrder.toDoubleOrNull() ?: 50.0
        val slVal = liveStopLossPct.toDoubleOrNull() ?: 1.0
        val tpVal = liveTakeProfitPct.toDoubleOrNull() ?: 1.5
        val maxLossUsdt = singleOrdVal * (slVal / 100.0)
        val targetProfitUsdt = singleOrdVal * (tpVal / 100.0)
        val rrRatio = if (slVal > 0) tpVal / slVal else 1.5

        AlertDialog(
            onDismissRequest = { showLiveEnableConfirmDialog = false },
            containerColor = SurfaceElevated,
            titleContentColor = TextPrimary,
            textContentColor = TextSecondary,
            title = {
                Text(
                    text = "🔥 确认开启 LIVE 实盘交易开关",
                    fontWeight = FontWeight.ExtraBold,
                    color = AccentCrimson,
                    fontSize = 18.sp
                )
            },
            text = {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text("开启后将激活 SUIUSDT 实盘交易通道，并应用您在【实盘面板】设定的专属仓位参数：")
                    Card(
                        colors = CardDefaults.cardColors(containerColor = BgObsidian),
                        border = BorderStroke(1.dp, BorderSubtle),
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Column(
                            modifier = Modifier.padding(12.dp),
                            verticalArrangement = Arrangement.spacedBy(4.dp)
                        ) {
                            MetricRow("实盘总授权资金上限", "$liveTotalCapital USDT")
                            MetricRow("实盘单笔开仓金额", "$liveSingleOrder USDT (${String.format(Locale.US, "%.1f", (singleOrdVal / max(totalCapVal, 1.0)) * 100)}% 总资金)")
                            MetricRow("实盘止损 / 预计最大亏损", "-$liveStopLossPct% (-${String.format(Locale.US, "%.2f", maxLossUsdt)} USDT)", valueColor = AccentCrimson)
                            MetricRow("实盘止盈 / 预计目标盈利", "+$liveTakeProfitPct% (+${String.format(Locale.US, "%.2f", targetProfitUsdt)} USDT)", valueColor = AccentEmerald)
                            MetricRow("实盘策略盈亏比 (R:R)", "1 : ${String.format(Locale.US, "%.2f", rrRatio)}", valueColor = AccentCyan)
                        }
                    }
                    Text(
                        text = "安全承诺：仅限 SUIUSDT 现货多头，0 杠杆、0 借贷，每笔持仓自动挂载硬止损单。您可随时拨回开关关闭实盘。",
                        fontSize = 12.sp,
                        color = TextMuted
                    )
                }
            },
            confirmButton = {
                Button(
                    colors = ButtonDefaults.buttonColors(containerColor = AccentCrimson, contentColor = Color.White),
                    onClick = {
                        onSavePositionConfig(
                            "LIVE",
                            liveTotalCapital,
                            liveSingleOrder,
                            liveMaxPosPct,
                            liveStopLossPct,
                            liveTakeProfitPct
                        )
                        onToggleLiveTradingMode(true)
                        showLiveEnableConfirmDialog = false
                    }
                ) {
                    Text("确认激活 LIVE 实盘", fontWeight = FontWeight.Bold)
                }
            },
            dismissButton = {
                TextButton(onClick = { showLiveEnableConfirmDialog = false }) {
                    Text("暂不开启", color = TextSecondary)
                }
            }
        )
    }

    // ------------------------------------------------------------------------
    // 3. Defensive Action Confirmation Dialog
    // ------------------------------------------------------------------------
    if (pendingDefensiveAction != null) {
        val actionName = pendingDefensiveAction!!
        AlertDialog(
            onDismissRequest = { pendingDefensiveAction = null },
            containerColor = SurfaceElevated,
            titleContentColor = TextPrimary,
            textContentColor = TextSecondary,
            title = {
                Text("🛡️ 确认执行风控防线指令：$actionName", fontWeight = FontWeight.Bold, color = AccentAmber)
            },
            text = {
                Text(
                    "该指令将立即向网关发送高优先级控制请求，将 D-01 引擎状态切换为 $actionName。\n\n" +
                        "• 所有未成交挂单买单将被立即撤销\n" +
                        "• 已在途持仓的止损保护卖单将 100% 强制保留"
                )
            },
            confirmButton = {
                Button(
                    colors = ButtonDefaults.buttonColors(containerColor = AccentCrimson, contentColor = Color.White),
                    onClick = {
                        onTriggerControlAction(actionName, "Operator manual $actionName")
                        pendingDefensiveAction = null
                    }
                ) {
                    Text("立即执行 $actionName", fontWeight = FontWeight.Bold)
                }
            },
            dismissButton = {
                TextButton(onClick = { pendingDefensiveAction = null }) {
                    Text("取消", color = TextSecondary)
                }
            }
        )
    }

    // ------------------------------------------------------------------------
    // Main Terminal Scaffold (Top Status Bar + 4 Dedicated Workspaces)
    // ------------------------------------------------------------------------
    Scaffold(
        containerColor = BgObsidian,
        topBar = {
            TopTerminalHeader(
                isLiveMode = isLiveMode,
                connectionHealthy = connectionHealthy,
                tradingState = status?.tradingState ?: "READY_NO_POSITION",
                suiPrice = status?.suiLastPrice ?: "0.8960",
                statusBannerMessage = statusBannerMessage,
                notificationsPermissionGranted = notificationsPermissionGranted,
                onSwitchToGatewayTab = { selectedTab = 3 }
            )
        },
        bottomBar = {
            BottomTerminalNavigationBar(
                selectedTab = selectedTab,
                criticalAlertCount = criticalCount,
                paperHasPos = status?.paperHasActivePosition == true,
                liveHasPos = status?.liveHasActivePosition == true,
                isLiveEnabled = isLiveMode,
                onSelectTab = { selectedTab = it }
            )
        }
    ) { innerPadding ->
        Box(
            modifier = Modifier
                .fillMaxSize()
                .padding(innerPadding)
                .background(BgObsidian)
        ) {
            when (selectedTab) {
                0 -> TradingWorkspacePanel(
                    workspaceMode = "PAPER",
                    status = status,
                    isLiveTradingEnabled = isLiveMode,
                    totalCapitalInput = paperTotalCapital,
                    singleOrderInput = paperSingleOrder,
                    maxPosPctInput = paperMaxPosPct,
                    stopLossPctInput = paperStopLossPct,
                    takeProfitPctInput = paperTakeProfitPct,
                    onTotalCapitalChange = { paperTotalCapital = it },
                    onSingleOrderChange = { paperSingleOrder = it },
                    onMaxPosPctChange = { paperMaxPosPct = it },
                    onStopLossPctChange = { paperStopLossPct = it },
                    onTakeProfitPctChange = { paperTakeProfitPct = it },
                    onRequestToggleLive = {},
                    onSaveConfig = {
                        onSavePositionConfig(
                            "PAPER",
                            paperTotalCapital,
                            paperSingleOrder,
                            paperMaxPosPct,
                            paperStopLossPct,
                            paperTakeProfitPct
                        )
                    },
                    onOpenPosition = {
                        onTriggerControlAction("OPEN_PAPER_POSITION", "Paper workspace 1-tap open")
                    },
                    onClosePosition = {
                        onTriggerControlAction("CLOSE_PAPER_POSITION", "Paper workspace 1-tap close")
                    }
                )

                1 -> TradingWorkspacePanel(
                    workspaceMode = "LIVE",
                    status = status,
                    isLiveTradingEnabled = isLiveMode,
                    totalCapitalInput = liveTotalCapital,
                    singleOrderInput = liveSingleOrder,
                    maxPosPctInput = liveMaxPosPct,
                    stopLossPctInput = liveStopLossPct,
                    takeProfitPctInput = liveTakeProfitPct,
                    onTotalCapitalChange = { liveTotalCapital = it },
                    onSingleOrderChange = { liveSingleOrder = it },
                    onMaxPosPctChange = { liveMaxPosPct = it },
                    onStopLossPctChange = { liveStopLossPct = it },
                    onTakeProfitPctChange = { liveTakeProfitPct = it },
                    onRequestToggleLive = { wantLive ->
                        if (wantLive) showLiveEnableConfirmDialog = true else onToggleLiveTradingMode(false)
                    },
                    onSaveConfig = {
                        onSavePositionConfig(
                            "LIVE",
                            liveTotalCapital,
                            liveSingleOrder,
                            liveMaxPosPct,
                            liveStopLossPct,
                            liveTakeProfitPct
                        )
                    },
                    onOpenPosition = {
                        onTriggerControlAction("OPEN_LIVE_POSITION", "Live workspace 1-tap open")
                    },
                    onClosePosition = {
                        onTriggerControlAction("CLOSE_LIVE_POSITION", "Live workspace 1-tap close")
                    }
                )

                2 -> ExecutionAndDefenseTab(
                    status = status,
                    onTriggerControlAction = onTriggerControlAction,
                    onRequestDefensiveConfirm = { action -> pendingDefensiveAction = action }
                )

                3 -> AlertsAndGatewayTab(
                    activeAlerts = activeAlerts,
                    serverUrl = serverUrl,
                    connectionHealthy = connectionHealthy,
                    onUpdateServerUrl = onUpdateServerUrl
                )
            }
        }
    }
}

// ============================================================================
// Persistent Institutional Top Header Bar
// ============================================================================
@Composable
private fun TopTerminalHeader(
    isLiveMode: Boolean,
    connectionHealthy: Boolean,
    tradingState: String,
    suiPrice: String,
    statusBannerMessage: String?,
    notificationsPermissionGranted: Boolean,
    onSwitchToGatewayTab: () -> Unit
) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .background(SurfaceCard)
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 16.dp, vertical = 12.dp),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Box(
                    modifier = Modifier
                        .size(10.dp)
                        .clip(CircleShape)
                        .background(if (connectionHealthy) AccentEmerald else AccentAmber)
                )
                Spacer(modifier = Modifier.width(8.dp))
                Column {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text(
                            text = "OBR-SUI QUANT",
                            color = TextPrimary,
                            fontWeight = FontWeight.ExtraBold,
                            fontSize = 16.sp,
                            letterSpacing = 0.8.sp
                        )
                        Spacer(modifier = Modifier.width(8.dp))
                        StatusPill(
                            text = if (isLiveMode) "LIVE 实盘已开" else "PAPER 模拟中",
                            bgColor = if (isLiveMode) AccentCrimson else AccentEmeraldDark,
                            textColor = if (isLiveMode) Color.White else AccentEmerald
                        )
                    }
                    Text(
                        text = "D-01: $tradingState",
                        color = TextSecondary,
                        fontSize = 11.sp,
                        fontFamily = FontFamily.Monospace
                    )
                }
            }

            Column(
                horizontalAlignment = Alignment.End,
                modifier = Modifier.clickable { onSwitchToGatewayTab() }
            ) {
                Text(
                    text = "$$suiPrice",
                    color = AccentCyan,
                    fontWeight = FontWeight.ExtraBold,
                    fontSize = 17.sp,
                    fontFamily = FontFamily.Monospace
                )
                Text(
                    text = if (connectionHealthy) "🟢 云网关已连通" else "🟡 币安直连模式 (点击设置)",
                    color = if (connectionHealthy) AccentEmerald else AccentAmber,
                    fontSize = 11.sp
                )
            }
        }

        if (!notificationsPermissionGranted) {
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .background(AccentCrimsonDark)
                    .padding(horizontal = 14.dp, vertical = 6.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text(
                    text = "⚠️ 系统通知权限未开启，已启用应用内 2s 高频同步与 WorkManager 补偿",
                    color = Color.White,
                    fontSize = 11.sp,
                    fontWeight = FontWeight.Medium
                )
            }
        }

        AnimatedVisibility(
            visible = !statusBannerMessage.isNullOrBlank(),
            enter = fadeIn(),
            exit = fadeOut()
        ) {
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .background(Color(0xFF065F46))
                    .padding(horizontal = 14.dp, vertical = 8.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text(
                    text = statusBannerMessage ?: "",
                    color = Color.White,
                    fontSize = 12.sp,
                    fontWeight = FontWeight.SemiBold
                )
            }
        }

        HorizontalDivider(color = BorderSubtle, thickness = 1.dp)
    }
}

// ============================================================================
// Bottom Navigation Bar (0: 模拟盘 | 1: 实盘交易 | 2: 风控演练 | 3: 审计网关)
// ============================================================================
@Composable
private fun BottomTerminalNavigationBar(
    selectedTab: Int,
    criticalAlertCount: Int,
    paperHasPos: Boolean,
    liveHasPos: Boolean,
    isLiveEnabled: Boolean,
    onSelectTab: (Int) -> Unit
) {
    val items = listOf(
        Triple(0, "🧪", "模拟盘"),
        Triple(1, "🔥", if (isLiveEnabled) "实盘(开启)" else "实盘交易"),
        Triple(2, "🛡️", "风控演练"),
        Triple(3, "🔔", if (criticalAlertCount > 0) "告警($criticalAlertCount)" else "审计网关")
    )

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .background(SurfaceCard)
    ) {
        HorizontalDivider(color = BorderSubtle, thickness = 1.dp)
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(vertical = 8.dp, horizontal = 8.dp),
            horizontalArrangement = Arrangement.SpaceEvenly
        ) {
            for ((index, icon, label) in items) {
                val selected = selectedTab == index
                val activeAccent = when (index) {
                    0 -> AccentEmerald
                    1 -> AccentCrimson
                    else -> AccentCyan
                }
                val pillBg = if (selected) SurfaceElevated else Color.Transparent
                val contentColor = if (selected) activeAccent else TextSecondary

                Box(
                    modifier = Modifier
                        .weight(1f)
                        .padding(horizontal = 4.dp)
                        .clip(RoundedCornerShape(12.dp))
                        .background(pillBg)
                        .border(
                            width = if (selected) 1.dp else 0.dp,
                            color = if (selected) activeAccent.copy(alpha = 0.6f) else Color.Transparent,
                            shape = RoundedCornerShape(12.dp)
                        )
                        .clickable { onSelectTab(index) }
                        .padding(vertical = 8.dp),
                    contentAlignment = Alignment.Center
                ) {
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Text(text = icon, fontSize = 16.sp)
                            if ((index == 0 && paperHasPos) || (index == 1 && liveHasPos)) {
                                Spacer(modifier = Modifier.width(4.dp))
                                Box(
                                    modifier = Modifier
                                        .size(6.dp)
                                        .clip(CircleShape)
                                        .background(activeAccent)
                                )
                            }
                        }
                        Spacer(modifier = Modifier.height(2.dp))
                        Text(
                            text = label,
                            color = contentColor,
                            fontSize = 11.sp,
                            fontWeight = if (selected) FontWeight.Bold else FontWeight.Normal,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis
                        )
                    }
                }
            }
        }
    }
}

// ============================================================================
// Unified Dedicated Workspace Panel for Tab 0 (🧪 模拟盘) & Tab 1 (🔥 实盘交易)
// Integrates Live Market + Active Position + Custom Position & Risk Config!
// ============================================================================
@Composable
private fun TradingWorkspacePanel(
    workspaceMode: String, // "PAPER" or "LIVE"
    status: StatusSnapshotEntity?,
    isLiveTradingEnabled: Boolean,
    totalCapitalInput: String,
    singleOrderInput: String,
    maxPosPctInput: String,
    stopLossPctInput: String,
    takeProfitPctInput: String,
    onTotalCapitalChange: (String) -> Unit,
    onSingleOrderChange: (String) -> Unit,
    onMaxPosPctChange: (String) -> Unit,
    onStopLossPctChange: (String) -> Unit,
    onTakeProfitPctChange: (String) -> Unit,
    onRequestToggleLive: (Boolean) -> Unit,
    onSaveConfig: () -> Unit,
    onOpenPosition: () -> Unit,
    onClosePosition: () -> Unit
) {
    val isLiveTab = workspaceMode == "LIVE"
    val themeAccent = if (isLiveTab) AccentCrimson else AccentEmerald
    val bid = status?.suiBid?.toDoubleOrNull()?.takeIf { it > 0 } ?: 0.8958
    val ask = status?.suiAsk?.toDoubleOrNull()?.takeIf { it > 0 } ?: 0.8960
    val spreadBps = if (bid > 0 && ask >= bid) ((ask - bid) / bid) * 10000.0 else 1.12
    val confFloat = status?.latestRegimeConfidence?.toFloatOrNull() ?: 0.81f

    val hasPos = if (isLiveTab) status?.liveHasActivePosition == true else status?.paperHasActivePosition == true
    val posSummary = if (isLiveTab) {
        status?.liveActivePositionSummary ?: "实盘当前空仓 (FLAT) — 实盘开关开启后按下方实盘仓位配置执行"
    } else {
        status?.paperActivePositionSummary ?: "模拟盘当前空仓 (FLAT) — 实时接入币安主网行情全链路模拟中"
    }
    val realizedPnl = if (isLiveTab) {
        status?.liveRealizedPnlUsdt ?: "0.0000"
    } else {
        status?.paperRealizedPnlUsdt ?: "0.0000"
    }

    // Financial Analyst Pre-Trade Calculations for this specific workspace
    val totalCap = totalCapitalInput.toDoubleOrNull() ?: if (isLiveTab) 500.0 else 1000.0
    val singleOrd = singleOrderInput.toDoubleOrNull() ?: if (isLiveTab) 50.0 else 100.0
    val slPct = stopLossPctInput.toDoubleOrNull() ?: 1.0
    val tpPct = takeProfitPctInput.toDoubleOrNull() ?: 1.5

    val estQtySui = if (ask > 0) singleOrd / ask else 0.0
    val slPrice = ask * (1.0 - slPct / 100.0)
    val tpPrice = ask * (1.0 + tpPct / 100.0)
    val roundTripFeeUsdt = singleOrd * 0.0020
    val maxLossUsdt = singleOrd * (slPct / 100.0) + roundTripFeeUsdt
    val netProfitUsdt = singleOrd * (tpPct / 100.0) - roundTripFeeUsdt
    val accountRiskPct = if (totalCap > 0) (maxLossUsdt / totalCap) * 100.0 else 0.0
    val rrRatio = if (maxLossUsdt > 0) max(0.0, netProfitUsdt) / maxLossUsdt else 0.0
    val breakevenWinRate = if (rrRatio > 0) (1.0 / (1.0 + rrRatio)) * 100.0 else 100.0

    val textFieldColors = OutlinedTextFieldDefaults.colors(
        focusedTextColor = TextPrimary,
        unfocusedTextColor = TextPrimary,
        focusedBorderColor = themeAccent,
        unfocusedBorderColor = BorderSubtle,
        focusedLabelColor = themeAccent,
        unfocusedLabelColor = TextSecondary,
        cursorColor = themeAccent
    )

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(14.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        // --------------------------------------------------------------------
        // 1. Workspace Header & Mode Master Switch Card
        // --------------------------------------------------------------------
        if (isLiveTab) {
            Card(
                modifier = Modifier.fillMaxWidth(),
                shape = RoundedCornerShape(16.dp),
                colors = CardDefaults.cardColors(
                    containerColor = if (isLiveTradingEnabled) AccentCrimsonDark.copy(alpha = 0.45f) else SurfaceCard
                ),
                border = BorderStroke(
                    width = 1.5.dp,
                    color = if (isLiveTradingEnabled) AccentCrimson else BorderSubtle
                )
            ) {
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(16.dp),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Column(modifier = Modifier.weight(1f)) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            StatusPill(
                                text = if (isLiveTradingEnabled) "🔥 LIVE 实盘已开启" else "🔒 LIVE 实盘已关闭 (默认)",
                                bgColor = if (isLiveTradingEnabled) AccentCrimson else SurfaceElevated,
                                textColor = if (isLiveTradingEnabled) Color.White else TextSecondary
                            )
                            Spacer(modifier = Modifier.width(8.dp))
                            Text(
                                text = "单笔 ${singleOrderInput}U",
                                color = AccentCrimson,
                                fontSize = 12.sp,
                                fontFamily = FontFamily.Monospace,
                                fontWeight = FontWeight.Bold
                            )
                        }
                        Spacer(modifier = Modifier.height(6.dp))
                        Text(
                            text = if (isLiveTradingEnabled)
                                "实盘交易通道已激活｜信号触发或手动建仓将使用下方实盘专属仓位配置"
                            else
                                "实盘开关默认关闭｜请先在下方设置实盘专属仓位，确认无误后拨动右侧开关启动实盘",
                            color = TextSecondary,
                            fontSize = 12.sp
                        )
                    }
                    Spacer(modifier = Modifier.width(12.dp))
                    Switch(
                        checked = isLiveTradingEnabled,
                        onCheckedChange = onRequestToggleLive,
                        colors = SwitchDefaults.colors(
                            checkedThumbColor = Color.White,
                            checkedTrackColor = AccentCrimson,
                            uncheckedThumbColor = Color.White,
                            uncheckedTrackColor = SurfaceElevated
                        )
                    )
                }
            }
        } else {
            Card(
                modifier = Modifier.fillMaxWidth(),
                shape = RoundedCornerShape(16.dp),
                colors = CardDefaults.cardColors(containerColor = SurfaceCard),
                border = BorderStroke(1.5.dp, AccentEmerald.copy(alpha = 0.6f))
            ) {
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(16.dp),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Column(modifier = Modifier.weight(1f)) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            StatusPill(
                                text = "🧪 PAPER 实网模拟盘 (常驻运行)",
                                bgColor = AccentEmerald,
                                textColor = BgObsidian
                            )
                            Spacer(modifier = Modifier.width(8.dp))
                            Text(
                                text = "单笔 ${singleOrderInput}U",
                                color = AccentEmerald,
                                fontSize = 12.sp,
                                fontFamily = FontFamily.Monospace,
                                fontWeight = FontWeight.Bold
                            )
                        }
                        Spacer(modifier = Modifier.height(6.dp))
                        Text(
                            text = "接入币安实时 SUIUSDT 盘口行情，独立模拟资金与模拟仓位配置，100% 零真实资金风险",
                            color = TextSecondary,
                            fontSize = 12.sp
                        )
                    }
                }
            }
        }

        // --------------------------------------------------------------------
        // 2. Real-Time Binance Market Quote & Workspace KPI Row
        // --------------------------------------------------------------------
        Card(
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(16.dp),
            colors = CardDefaults.cardColors(containerColor = SurfaceCard),
            border = BorderStroke(1.dp, BorderSubtle)
        ) {
            Column(
                modifier = Modifier.padding(16.dp),
                verticalArrangement = Arrangement.spacedBy(10.dp)
            ) {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Column {
                        Text("SUI / USDT 币安实时现价", color = TextSecondary, fontSize = 12.sp)
                        Text(
                            text = status?.suiLastPrice?.takeIf { it != "0.0000" } ?: String.format(Locale.US, "%.4f", (bid + ask) / 2.0),
                            color = TextPrimary,
                            fontSize = 28.sp,
                            fontWeight = FontWeight.ExtraBold,
                            fontFamily = FontFamily.Monospace
                        )
                    }
                    Column(horizontalAlignment = Alignment.End) {
                        Text("BTC / USDT 宏观锚定", color = TextSecondary, fontSize = 12.sp)
                        Text(
                            text = "$${status?.btcLastPrice?.takeIf { it != "0.00" } ?: "77520.00"}",
                            color = AccentAmber,
                            fontSize = 17.sp,
                            fontWeight = FontWeight.Bold,
                            fontFamily = FontFamily.Monospace
                        )
                        Text(
                            text = "买一 ${String.format(Locale.US, "%.4f", bid)} / 卖一 ${String.format(Locale.US, "%.4f", ask)} (${String.format(Locale.US, "%.1f", spreadBps)} bps)",
                            color = TextMuted,
                            fontSize = 11.sp,
                            fontFamily = FontFamily.Monospace
                        )
                    }
                }

                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    MiniKpiBox(
                        modifier = Modifier.weight(1f),
                        label = if (isLiveTab) "实盘总资金" else "模拟总资金",
                        value = "$totalCapitalInput U",
                        valueColor = TextPrimary
                    )
                    MiniKpiBox(
                        modifier = Modifier.weight(1f),
                        label = if (isLiveTab) "实盘单笔额" else "模拟单笔额",
                        value = "$singleOrderInput U",
                        valueColor = themeAccent
                    )
                    val pnlPositive = !realizedPnl.startsWith("-")
                    MiniKpiBox(
                        modifier = Modifier.weight(1f),
                        label = if (isLiveTab) "实盘净盈亏" else "模拟净盈亏",
                        value = "${if (pnlPositive) "+" else ""}$realizedPnl U",
                        valueColor = if (pnlPositive) AccentEmerald else AccentCrimson
                    )
                }

                LinearProgressIndicator(
                    progress = { confFloat.coerceIn(0f, 1f) },
                    modifier = Modifier
                        .fillMaxWidth()
                        .height(5.dp)
                        .clip(RoundedCornerShape(3.dp)),
                    color = themeAccent,
                    trackColor = BgObsidian
                )
            }
        }

        // --------------------------------------------------------------------
        // 3. Active Position Monitor & 1-Tap Execution Card (PAPER or LIVE)
        // --------------------------------------------------------------------
        Card(
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(16.dp),
            colors = CardDefaults.cardColors(containerColor = SurfaceCard),
            border = BorderStroke(
                width = 1.dp,
                color = if (hasPos) themeAccent else BorderSubtle
            )
        ) {
            Column(
                modifier = Modifier.padding(16.dp),
                verticalArrangement = Arrangement.spacedBy(10.dp)
            ) {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Text(
                        text = if (isLiveTab) "🎯 实盘当前持仓与实盘执行台" else "🎯 模拟盘当前持仓与模拟演练台",
                        color = TextPrimary,
                        fontWeight = FontWeight.Bold,
                        fontSize = 15.sp
                    )
                    StatusPill(
                        text = if (hasPos) "PROTECTED 持仓保护中" else "FLAT 空仓待命",
                        bgColor = if (hasPos) themeAccent.copy(alpha = 0.2f) else SurfaceElevated,
                        textColor = if (hasPos) themeAccent else TextSecondary
                    )
                }

                Box(
                    modifier = Modifier
                        .fillMaxWidth()
                        .clip(RoundedCornerShape(10.dp))
                        .background(BgObsidian)
                        .padding(12.dp)
                ) {
                    Text(
                        text = posSummary,
                        color = if (hasPos) themeAccent else TextSecondary,
                        fontSize = 13.sp,
                        fontFamily = FontFamily.Monospace,
                        lineHeight = 19.sp
                    )
                }

                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.spacedBy(10.dp)
                ) {
                    Button(
                        modifier = Modifier.weight(1f),
                        colors = ButtonDefaults.buttonColors(
                            containerColor = themeAccent,
                            contentColor = if (isLiveTab) Color.White else BgObsidian
                        ),
                        onClick = {
                            if (isLiveTab && !isLiveTradingEnabled) {
                                onRequestToggleLive(true)
                            } else {
                                onOpenPosition()
                            }
                        }
                    ) {
                        Text(
                            text = if (isLiveTab) "🔥 实盘按配置建仓" else "⚡ 模拟按配置建仓",
                            fontWeight = FontWeight.Bold
                        )
                    }
                    OutlinedButton(
                        modifier = Modifier.weight(1f),
                        border = BorderStroke(1.dp, AccentCrimson),
                        onClick = onClosePosition
                    ) {
                        Text(
                            text = if (isLiveTab) "实盘市价平仓" else "模拟市价平仓",
                            color = AccentCrimson,
                            fontWeight = FontWeight.Bold
                        )
                    }
                }
            }
        }

        // --------------------------------------------------------------------
        // 4. Integrated Position & Risk Configuration Studio (专属仓位配置面板)
        // --------------------------------------------------------------------
        Card(
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(16.dp),
            colors = CardDefaults.cardColors(containerColor = SurfaceCard),
            border = BorderStroke(1.dp, themeAccent.copy(alpha = 0.45f))
        ) {
            Column(
                modifier = Modifier.padding(16.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp)
            ) {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Text(
                        text = if (isLiveTab) "⚙️ 实盘专属仓位与风控配置面板" else "⚙️ 模拟盘专属仓位与风控配置面板",
                        color = TextPrimary,
                        fontWeight = FontWeight.Bold,
                        fontSize = 15.sp
                    )
                    StatusPill(
                        text = "R:R = 1 : ${String.format(Locale.US, "%.2f", rrRatio)}",
                        bgColor = if (rrRatio >= 1.2) AccentEmeraldDark else AccentAmberDark,
                        textColor = if (rrRatio >= 1.2) AccentEmerald else AccentAmber
                    )
                }

                // Quick Presets Row
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    if (isLiveTab) {
                        PresetChip(
                            modifier = Modifier.weight(1f),
                            title = "保守实盘",
                            subtitle = "200U / 单笔20U",
                            accentColor = themeAccent,
                            onClick = {
                                onTotalCapitalChange("200.00")
                                onSingleOrderChange("20.00")
                                onMaxPosPctChange("10.00")
                                onStopLossPctChange("0.80")
                                onTakeProfitPctChange("1.50")
                            }
                        )
                        PresetChip(
                            modifier = Modifier.weight(1f),
                            title = "标准实盘",
                            subtitle = "500U / 单笔50U",
                            accentColor = themeAccent,
                            onClick = {
                                onTotalCapitalChange("500.00")
                                onSingleOrderChange("50.00")
                                onMaxPosPctChange("10.00")
                                onStopLossPctChange("1.00")
                                onTakeProfitPctChange("1.50")
                            }
                        )
                        PresetChip(
                            modifier = Modifier.weight(1f),
                            title = "进阶实盘",
                            subtitle = "2000U / 单笔200U",
                            accentColor = themeAccent,
                            onClick = {
                                onTotalCapitalChange("2000.00")
                                onSingleOrderChange("200.00")
                                onMaxPosPctChange("10.00")
                                onStopLossPctChange("1.00")
                                onTakeProfitPctChange("2.00")
                            }
                        )
                    } else {
                        PresetChip(
                            modifier = Modifier.weight(1f),
                            title = "轻量模拟",
                            subtitle = "500U / 单笔25U",
                            accentColor = themeAccent,
                            onClick = {
                                onTotalCapitalChange("500.00")
                                onSingleOrderChange("25.00")
                                onMaxPosPctChange("5.00")
                                onStopLossPctChange("0.80")
                                onTakeProfitPctChange("1.50")
                            }
                        )
                        PresetChip(
                            modifier = Modifier.weight(1f),
                            title = "标准 C1 模拟",
                            subtitle = "1000U / 单笔100U",
                            accentColor = themeAccent,
                            onClick = {
                                onTotalCapitalChange("1000.00")
                                onSingleOrderChange("100.00")
                                onMaxPosPctChange("10.00")
                                onStopLossPctChange("1.00")
                                onTakeProfitPctChange("1.50")
                            }
                        )
                        PresetChip(
                            modifier = Modifier.weight(1f),
                            title = "深度压力模拟",
                            subtitle = "5000U / 单笔500U",
                            accentColor = themeAccent,
                            onClick = {
                                onTotalCapitalChange("5000.00")
                                onSingleOrderChange("500.00")
                                onMaxPosPctChange("10.00")
                                onStopLossPctChange("1.00")
                                onTakeProfitPctChange("2.20")
                            }
                        )
                    }
                }

                // Input Fields
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.spacedBy(10.dp)
                ) {
                    OutlinedTextField(
                        value = totalCapitalInput,
                        onValueChange = onTotalCapitalChange,
                        label = { Text(if (isLiveTab) "实盘总资金 (USDT)" else "模拟总资金 (USDT)") },
                        modifier = Modifier.weight(1f),
                        singleLine = true,
                        colors = textFieldColors
                    )
                    OutlinedTextField(
                        value = singleOrderInput,
                        onValueChange = onSingleOrderChange,
                        label = { Text(if (isLiveTab) "实盘单笔开仓 (USDT)" else "模拟单笔开仓 (USDT)") },
                        modifier = Modifier.weight(1f),
                        singleLine = true,
                        colors = textFieldColors
                    )
                }

                val sliderOrderRatio = ((singleOrd / max(totalCap, 10.0)).toFloat()).coerceIn(0.01f, 0.50f)
                Column {
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween
                    ) {
                        Text("单笔开仓占总资金比例滑块", color = TextSecondary, fontSize = 12.sp)
                        Text(
                            text = "${String.format(Locale.US, "%.1f", sliderOrderRatio * 100f)}%",
                            color = themeAccent,
                            fontSize = 12.sp,
                            fontFamily = FontFamily.Monospace,
                            fontWeight = FontWeight.Bold
                        )
                    }
                    Slider(
                        value = sliderOrderRatio,
                        onValueChange = { ratio ->
                            val newOrderUsdt = max(5.0, (totalCap * ratio))
                            onSingleOrderChange(String.format(Locale.US, "%.2f", newOrderUsdt))
                            onMaxPosPctChange(String.format(Locale.US, "%.2f", ratio * 100f))
                        },
                        valueRange = 0.01f..0.50f,
                        colors = SliderDefaults.colors(
                            thumbColor = themeAccent,
                            activeTrackColor = themeAccent,
                            inactiveTrackColor = BgObsidian
                        )
                    )
                }

                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    OutlinedTextField(
                        value = maxPosPctInput,
                        onValueChange = onMaxPosPctChange,
                        label = { Text("最大仓位 (%)") },
                        modifier = Modifier.weight(1f),
                        singleLine = true,
                        colors = textFieldColors
                    )
                    OutlinedTextField(
                        value = stopLossPctInput,
                        onValueChange = onStopLossPctChange,
                        label = { Text("止损触发 (%)") },
                        modifier = Modifier.weight(1f),
                        singleLine = true,
                        colors = textFieldColors
                    )
                    OutlinedTextField(
                        value = takeProfitPctInput,
                        onValueChange = onTakeProfitPctChange,
                        label = { Text("止盈目标 (%)") },
                        modifier = Modifier.weight(1f),
                        singleLine = true,
                        colors = textFieldColors
                    )
                }

                // Financial Analyst Real-Time Risk & Breakeven Box
                Column(
                    modifier = Modifier
                        .fillMaxWidth()
                        .clip(RoundedCornerShape(10.dp))
                        .background(BgObsidian)
                        .padding(12.dp),
                    verticalArrangement = Arrangement.spacedBy(4.dp)
                ) {
                    MetricRow("预计建仓数量 (按卖一价 $ask)", "${String.format(Locale.US, "%.1f", estQtySui)} SUI")
                    MetricRow("止损触发价 / 止盈目标价", "${String.format(Locale.US, "%.4f", slPrice)} / ${String.format(Locale.US, "%.4f", tpPrice)} U")
                    MetricRow(
                        label = "单笔含费最大亏损 (占总资金比)",
                        value = "-${String.format(Locale.US, "%.2f", maxLossUsdt)} U (${String.format(Locale.US, "%.2f", accountRiskPct)}%)",
                        valueColor = AccentCrimson
                    )
                    MetricRow(
                        label = "单笔扣费净止盈 / 平衡胜率要求",
                        value = "+${String.format(Locale.US, "%.2f", netProfitUsdt)} U (需胜率 >= ${String.format(Locale.US, "%.1f", breakevenWinRate)}%)",
                        valueColor = AccentEmerald
                    )
                }

                Button(
                    modifier = Modifier
                        .fillMaxWidth()
                        .height(50.dp),
                    shape = RoundedCornerShape(12.dp),
                    colors = ButtonDefaults.buttonColors(
                        containerColor = themeAccent,
                        contentColor = if (isLiveTab) Color.White else BgObsidian
                    ),
                    onClick = onSaveConfig
                ) {
                    Text(
                        text = if (isLiveTab) "💾 保存并同步【实盘】仓位配置" else "💾 保存并同步【模拟盘】仓位配置",
                        fontWeight = FontWeight.ExtraBold,
                        fontSize = 15.sp
                    )
                }
            }
        }
    }
}

// ============================================================================
// TAB 2: 🛡️ 风控演练中心 (Execution & Defense Control Center Tab)
// ============================================================================
@Composable
private fun ExecutionAndDefenseTab(
    status: StatusSnapshotEntity?,
    onTriggerControlAction: (String, String) -> Unit,
    onRequestDefensiveConfirm: (String) -> Unit
) {
    val currentState = status?.tradingState ?: "READY_NO_POSITION"
    val states = listOf(
        "READY_NO_POSITION" to "空仓就绪",
        "PROTECTED" to "持仓保护",
        "ENTRY_FROZEN" to "冻结禁买",
        "SECURITY_LOCKDOWN" to "安全锁仓"
    )

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(14.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        Card(
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(16.dp),
            colors = CardDefaults.cardColors(containerColor = SurfaceCard),
            border = BorderStroke(1.dp, BorderSubtle)
        ) {
            Column(
                modifier = Modifier.padding(16.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp)
            ) {
                Text("🔄 D-01 交易引擎状态机矩阵", color = TextPrimary, fontWeight = FontWeight.Bold, fontSize = 15.sp)
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.spacedBy(6.dp)
                ) {
                    for ((code, label) in states) {
                        val active = currentState == code
                        val activeColor = when (code) {
                            "SECURITY_LOCKDOWN" -> AccentCrimson
                            "ENTRY_FROZEN" -> AccentAmber
                            "PROTECTED" -> AccentEmerald
                            else -> AccentCyan
                        }
                        Box(
                            modifier = Modifier
                                .weight(1f)
                                .clip(RoundedCornerShape(10.dp))
                                .background(if (active) activeColor.copy(alpha = 0.2f) else BgObsidian)
                                .border(
                                    width = if (active) 1.5.dp else 1.dp,
                                    color = if (active) activeColor else BorderSubtle,
                                    shape = RoundedCornerShape(10.dp)
                                )
                                .padding(vertical = 10.dp, horizontal = 4.dp),
                            contentAlignment = Alignment.Center
                        ) {
                            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                                Text(
                                    text = label,
                                    color = if (active) activeColor else TextSecondary,
                                    fontWeight = if (active) FontWeight.ExtraBold else FontWeight.Normal,
                                    fontSize = 12.sp
                                )
                                Text(
                                    text = if (active) "● 当前状态" else "○ 待命",
                                    color = if (active) activeColor else TextMuted,
                                    fontSize = 10.sp
                                )
                            }
                        }
                    }
                }
            }
        }

        Card(
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(16.dp),
            colors = CardDefaults.cardColors(containerColor = SurfaceCard),
            border = BorderStroke(1.dp, BorderSubtle)
        ) {
            Column(
                modifier = Modifier.padding(16.dp),
                verticalArrangement = Arrangement.spacedBy(10.dp)
            ) {
                Text("🛡️ 防重放紧急风控指令台", color = TextPrimary, fontWeight = FontWeight.Bold, fontSize = 15.sp)
                Text(
                    text = "支持在手机端一键下达战术风控指令（冻结买单但 100% 保留在途持仓的止损保护单）：",
                    color = TextSecondary,
                    fontSize = 12.sp
                )

                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.spacedBy(10.dp)
                ) {
                    Button(
                        modifier = Modifier
                            .weight(1f)
                            .height(48.dp),
                        shape = RoundedCornerShape(12.dp),
                        colors = ButtonDefaults.buttonColors(containerColor = AccentAmber, contentColor = BgObsidian),
                        onClick = { onRequestDefensiveConfirm("FREEZE_ENTRY") }
                    ) {
                        Text("❄️ 冻结新开仓", fontWeight = FontWeight.ExtraBold)
                    }
                    Button(
                        modifier = Modifier
                            .weight(1f)
                            .height(48.dp),
                        shape = RoundedCornerShape(12.dp),
                        colors = ButtonDefaults.buttonColors(containerColor = AccentCrimson, contentColor = Color.White),
                        onClick = { onRequestDefensiveConfirm("SECURITY_LOCKDOWN") }
                    ) {
                        Text("🚨 紧急安全锁仓", fontWeight = FontWeight.ExtraBold)
                    }
                }

                OutlinedButton(
                    modifier = Modifier
                        .fillMaxWidth()
                        .height(46.dp),
                    shape = RoundedCornerShape(12.dp),
                    border = BorderStroke(1.dp, AccentCyan),
                    onClick = { onTriggerControlAction("RESUME_READY", "Operator manual reconcile & resume") }
                ) {
                    Text("✅ 解除冻结并恢复就绪 (SYNCING -> READY)", color = AccentCyan, fontWeight = FontWeight.Bold)
                }
            }
        }

        Card(
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(16.dp),
            colors = CardDefaults.cardColors(containerColor = SurfaceCard),
            border = BorderStroke(1.dp, BorderSubtle)
        ) {
            Column(
                modifier = Modifier.padding(16.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp)
            ) {
                Text("🧠 E-01 签名模型仓库与全链路健康度", color = TextPrimary, fontWeight = FontWeight.Bold, fontSize = 15.sp)
                HorizontalDivider(color = BorderSubtle)
                MetricRow("当前激活模型包 (Active Bundle)", status?.activePaperBundleId ?: "B-SUI-5M-V1.0", valueColor = AccentCyan)
                MetricRow("影子伴飞模型包 (Shadow Bundle)", status?.shadowBundleId ?: "B-SUI-5M-V2.0-SHADOW")
                MetricRow("激活退出策略版本 (C-01 Exit)", status?.activePaperExitVersion ?: "C1-v1.0", valueColor = AccentEmerald)
                MetricRow("在途持仓锁定退出策略", status?.pinnedPositionExitVersion ?: "NONE (当前空仓)")
                MetricRow("F-01 行情流健康状态", if (status?.wsStreamHealthy == true) "HEALTHY (2s 实时同步)" else "DIRECT_BINANCE_FALLBACK", valueColor = AccentEmerald)
                MetricRow("F-02 守护进程心跳状态", if (status?.f02HeartbeatHealthy == true) "HEALTHY" else "LOCAL_GUARD", valueColor = AccentEmerald)
            }
        }
    }
}

// ============================================================================
// TAB 3: 🔔 审计日志与网关连接设置 (Alerts & Gateway Settings Tab)
// ============================================================================
@Composable
private fun AlertsAndGatewayTab(
    activeAlerts: List<AlertEntity>,
    serverUrl: String,
    connectionHealthy: Boolean,
    onUpdateServerUrl: (String) -> Unit
) {
    var serverUrlDraft by remember(serverUrl) { mutableStateOf(serverUrl) }
    var selectedFilter by remember { mutableStateOf("ALL") }

    val filteredAlerts = remember(activeAlerts, selectedFilter) {
        when (selectedFilter) {
            "CRITICAL_HIGH" -> activeAlerts.filter { it.severity == "CRITICAL" || it.severity == "HIGH" }
            "INFO" -> activeAlerts.filter { it.severity == "INFO" }
            else -> activeAlerts
        }
    }

    val textFieldColors = OutlinedTextFieldDefaults.colors(
        focusedTextColor = TextPrimary,
        unfocusedTextColor = TextPrimary,
        focusedBorderColor = AccentCyan,
        unfocusedBorderColor = BorderSubtle,
        focusedLabelColor = AccentCyan,
        unfocusedLabelColor = TextSecondary,
        cursorColor = AccentCyan
    )

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(14.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        // 1. Gateway Connection & Endpoint Switcher Card
        Card(
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(16.dp),
            colors = CardDefaults.cardColors(containerColor = SurfaceCard),
            border = BorderStroke(1.dp, BorderSubtle)
        ) {
            Column(
                modifier = Modifier.padding(16.dp),
                verticalArrangement = Arrangement.spacedBy(10.dp)
            ) {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Text("🌐 实时网关连接配置 (G-01 Gateway)", color = TextPrimary, fontWeight = FontWeight.Bold, fontSize = 15.sp)
                    StatusPill(
                        text = if (connectionHealthy) "ONLINE 云端已连通" else "DIRECT 币安直连兜底",
                        bgColor = if (connectionHealthy) AccentEmeraldDark else AccentAmberDark,
                        textColor = if (connectionHealthy) AccentEmerald else AccentAmber
                    )
                }

                OutlinedTextField(
                    value = serverUrlDraft,
                    onValueChange = { serverUrlDraft = it },
                    label = { Text("网关 HTTP 地址 (默认东京云服务器 8765 端口)") },
                    modifier = Modifier.fillMaxWidth(),
                    singleLine = true,
                    colors = textFieldColors
                )

                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.spacedBy(6.dp)
                ) {
                    OutlinedButton(
                        modifier = Modifier.weight(1f),
                        border = BorderStroke(1.dp, AccentCyan),
                        onClick = {
                            serverUrlDraft = "http://43.163.203.127"
                            onUpdateServerUrl(serverUrlDraft)
                        }
                    ) {
                        Text("东京云主网", color = AccentCyan, fontSize = 11.sp, fontWeight = FontWeight.Bold)
                    }
                    OutlinedButton(
                        modifier = Modifier.weight(1f),
                        border = BorderStroke(1.dp, BorderSubtle),
                        onClick = {
                            serverUrlDraft = "http://192.168.1.241:8765"
                            onUpdateServerUrl(serverUrlDraft)
                        }
                    ) {
                        Text("局域网主机", color = TextPrimary, fontSize = 11.sp)
                    }
                    Button(
                        colors = ButtonDefaults.buttonColors(containerColor = AccentCyan, contentColor = BgObsidian),
                        onClick = { onUpdateServerUrl(serverUrlDraft) }
                    ) {
                        Text("立即连接", fontWeight = FontWeight.Bold, fontSize = 12.sp)
                    }
                }
            }
        }

        // 2. Graded Alert Stream with Severity Filters
        Card(
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(16.dp),
            colors = CardDefaults.cardColors(containerColor = SurfaceCard),
            border = BorderStroke(1.dp, BorderSubtle)
        ) {
            Column(
                modifier = Modifier.padding(16.dp),
                verticalArrangement = Arrangement.spacedBy(10.dp)
            ) {
                Text(
                    text = "🔔 G-02 实时分级告警与审计流 (${filteredAlerts.size})",
                    color = TextPrimary,
                    fontWeight = FontWeight.Bold,
                    fontSize = 15.sp
                )

                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    FilterTabChip(
                        label = "全部 (${activeAlerts.size})",
                        selected = selectedFilter == "ALL",
                        onClick = { selectedFilter = "ALL" }
                    )
                    FilterTabChip(
                        label = "高优/紧急",
                        selected = selectedFilter == "CRITICAL_HIGH",
                        onClick = { selectedFilter = "CRITICAL_HIGH" }
                    )
                    FilterTabChip(
                        label = "常规审计",
                        selected = selectedFilter == "INFO",
                        onClick = { selectedFilter = "INFO" }
                    )
                }

                if (filteredAlerts.isEmpty()) {
                    Box(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(vertical = 20.dp),
                        contentAlignment = Alignment.Center
                    ) {
                        Text("当前筛选条件下暂无告警记录", color = TextMuted, fontSize = 13.sp)
                    }
                } else {
                    for (alert in filteredAlerts.take(20)) {
                        val badgeColor = when (alert.severity) {
                            "CRITICAL" -> AccentCrimson
                            "HIGH" -> AccentAmber
                            else -> AccentCyan
                        }
                        Box(
                            modifier = Modifier
                                .fillMaxWidth()
                                .clip(RoundedCornerShape(12.dp))
                                .background(BgObsidian)
                                .border(1.dp, badgeColor.copy(alpha = 0.35f), RoundedCornerShape(12.dp))
                                .padding(12.dp)
                        ) {
                            Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                                Row(
                                    modifier = Modifier.fillMaxWidth(),
                                    horizontalArrangement = Arrangement.SpaceBetween,
                                    verticalAlignment = Alignment.CenterVertically
                                ) {
                                    StatusPill(
                                        text = "${alert.severity} · ${alert.source}",
                                        bgColor = badgeColor.copy(alpha = 0.2f),
                                        textColor = badgeColor
                                    )
                                    Text(
                                        text = "#${alert.alertSeq}",
                                        color = TextMuted,
                                        fontSize = 11.sp,
                                        fontFamily = FontFamily.Monospace
                                    )
                                }
                                Text(
                                    text = alert.dedupKey,
                                    color = TextPrimary,
                                    fontWeight = FontWeight.SemiBold,
                                    fontSize = 13.sp,
                                    fontFamily = FontFamily.Monospace
                                )
                                Text(
                                    text = "执行动作: ${alert.actionsTakenJson}",
                                    color = AccentEmerald,
                                    fontSize = 11.sp
                                )
                                Text(
                                    text = "UTC: ${alert.firstSeenUtc}",
                                    color = TextMuted,
                                    fontSize = 11.sp,
                                    fontFamily = FontFamily.Monospace
                                )
                            }
                        }
                    }
                }
            }
        }
    }
}

// ============================================================================
// Reusable Institutional Design Components
// ============================================================================
@Composable
private fun StatusPill(
    text: String,
    bgColor: Color,
    textColor: Color
) {
    Surface(
        color = bgColor,
        shape = RoundedCornerShape(6.dp)
    ) {
        Text(
            text = text,
            color = textColor,
            fontSize = 11.sp,
            fontWeight = FontWeight.ExtraBold,
            modifier = Modifier.padding(horizontal = 8.dp, vertical = 3.dp)
        )
    }
}

@Composable
private fun MetricRow(
    label: String,
    value: String,
    valueColor: Color = TextPrimary
) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically
    ) {
        Text(text = label, color = TextSecondary, fontSize = 12.sp)
        Text(
            text = value,
            color = valueColor,
            fontSize = 12.sp,
            fontWeight = FontWeight.Bold,
            fontFamily = FontFamily.Monospace
        )
    }
}

@Composable
private fun MiniKpiBox(
    modifier: Modifier = Modifier,
    label: String,
    value: String,
    valueColor: Color
) {
    Column(
        modifier = modifier
            .clip(RoundedCornerShape(10.dp))
            .background(BgObsidian)
            .border(1.dp, BorderSubtle, RoundedCornerShape(10.dp))
            .padding(10.dp),
        horizontalAlignment = Alignment.Start
    ) {
        Text(text = label, color = TextMuted, fontSize = 11.sp, maxLines = 1)
        Spacer(modifier = Modifier.height(4.dp))
        Text(
            text = value,
            color = valueColor,
            fontWeight = FontWeight.ExtraBold,
            fontSize = 13.sp,
            fontFamily = FontFamily.Monospace,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis
        )
    }
}

@Composable
private fun PresetChip(
    modifier: Modifier = Modifier,
    title: String,
    subtitle: String,
    accentColor: Color = AccentCyan,
    onClick: () -> Unit
) {
    Column(
        modifier = modifier
            .clip(RoundedCornerShape(10.dp))
            .background(BgObsidian)
            .border(1.dp, BorderSubtle, RoundedCornerShape(10.dp))
            .clickable { onClick() }
            .padding(10.dp),
        horizontalAlignment = Alignment.CenterHorizontally
    ) {
        Text(text = title, color = accentColor, fontWeight = FontWeight.Bold, fontSize = 12.sp)
        Spacer(modifier = Modifier.height(2.dp))
        Text(text = subtitle, color = TextSecondary, fontSize = 10.sp, textAlign = TextAlign.Center)
    }
}

@Composable
private fun FilterTabChip(
    label: String,
    selected: Boolean,
    onClick: () -> Unit
) {
    Box(
        modifier = Modifier
            .clip(RoundedCornerShape(8.dp))
            .background(if (selected) AccentCyan.copy(alpha = 0.2f) else BgObsidian)
            .border(
                width = 1.dp,
                color = if (selected) AccentCyan else BorderSubtle,
                shape = RoundedCornerShape(8.dp)
            )
            .clickable { onClick() }
            .padding(horizontal = 12.dp, vertical = 6.dp)
    ) {
        Text(
            text = label,
            color = if (selected) AccentCyan else TextSecondary,
            fontSize = 12.sp,
            fontWeight = if (selected) FontWeight.Bold else FontWeight.Normal
        )
    }
}
