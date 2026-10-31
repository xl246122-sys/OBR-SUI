package com.suiquant.monitor.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.suiquant.monitor.data.AlertEntity
import com.suiquant.monitor.data.StatusSnapshotEntity

/**
 * G-01 Native Jetpack Compose Dashboard & Foreground Modal Alert Screen (§12.1, QA-11).
 *
 * Security & Governance Boundaries:
 * - Strictly Read-Only telemetry for `PAPER` execution mode (`SUIUSDT` spot only).
 * - Holds ZERO exchange API keys and provides NO manual buy/sell, unfreeze, or risk-relaxation controls.
 * - Provides only Ed25519-authenticated defensive degradation (`FREEZE_ENTRY` and `SECURITY_LOCKDOWN`),
 *   preserving existing `-1%` stop-loss protection orders on any open position.
 * - Self-checks `POST_NOTIFICATIONS` permission on startup/resume and pins a high-visibility red warning
 *   banner at the top of the screen if system notifications are disabled (`QA-11`).
 */
@Composable
fun MonitorDashboardScreen(
    status: StatusSnapshotEntity?,
    activeAlerts: List<AlertEntity>,
    foregroundModalAlert: AlertEntity?,
    notificationsPermissionGranted: Boolean,
    onAcknowledgeForegroundModal: (String) -> Unit,
    onTriggerFreezeEntry: (String) -> Unit,
    onTriggerSecurityLockdown: (String) -> Unit
) {
    var pendingDefensiveAction by remember { mutableStateOf<String?>(null) }

    // 1. Foreground Native Modal AlertDialog for HIGH / CRITICAL alerts (QA-11)
    if (foregroundModalAlert != null) {
        AlertDialog(
            onDismissRequest = { /* Require explicit user acknowledgment */ },
            title = {
                Text(
                    text = "[${foregroundModalAlert.severity}] ${foregroundModalAlert.source}",
                    fontWeight = FontWeight.Bold,
                    color = if (foregroundModalAlert.severity == "CRITICAL") Color(0xFFB71C1C) else Color(0xFFE65100)
                )
            },
            text = {
                Column {
                    Text("Alert ID: ${foregroundModalAlert.alertId}")
                    Text("Event: ${foregroundModalAlert.dedupKey}")
                    Text("First Seen (UTC): ${foregroundModalAlert.firstSeenUtc}")
                    Text("Auto Protection Actions: ${foregroundModalAlert.actionsTakenJson}")
                    Text("Sanitized Details: ${foregroundModalAlert.sanitizedDetailsJson}")
                }
            },
            confirmButton = {
                Button(onClick = { onAcknowledgeForegroundModal(foregroundModalAlert.alertId) }) {
                    Text("Acknowledge & Sync ACK")
                }
            }
        )
    }

    // 2. Confirmation Dialog for Ed25519-Signed Defensive Control (FREEZE_ENTRY / SECURITY_LOCKDOWN)
    if (pendingDefensiveAction != null) {
        val actionName = pendingDefensiveAction!!
        AlertDialog(
            onDismissRequest = { pendingDefensiveAction = null },
            title = { Text("Confirm Defensive Control: $actionName") },
            text = {
                Text(
                    "This sends an Ed25519-signed non-replayable command to transition D-01 to $actionName. " +
                        "Open BUY orders will be canceled while existing -1% stop-loss SELL protection orders " +
                        "remain strictly active. Unfreezing requires full server SYNCING reconciliation."
                )
            },
            confirmButton = {
                Button(
                    colors = ButtonDefaults.buttonColors(containerColor = Color(0xFFB71C1C)),
                    onClick = {
                        if (actionName == "FREEZE_ENTRY") {
                            onTriggerFreezeEntry("Manual mobile operator entry freeze")
                        } else {
                            onTriggerSecurityLockdown("Manual mobile operator emergency security lockdown")
                        }
                        pendingDefensiveAction = null
                    }
                ) {
                    Text("Sign & Execute $actionName")
                }
            },
            dismissButton = {
                TextButton(onClick = { pendingDefensiveAction = null }) {
                    Text("Cancel")
                }
            }
        )
    }

    Scaffold { innerPadding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(innerPadding)
                .padding(16.dp)
        ) {
            // 3. Persistent Top Warning Banner when POST_NOTIFICATIONS is denied (QA-11)
            if (!notificationsPermissionGranted) {
                Column(
                    modifier = Modifier
                        .fillMaxWidth()
                        .background(Color(0xFFB71C1C))
                        .padding(12.dp)
                ) {
                    Text(
                        text = "CRITICAL WARNING: System Notification Permission (POST_NOTIFICATIONS) Denied!",
                        color = Color.White,
                        fontWeight = FontWeight.Bold
                    )
                    Text(
                        text = "Background OS notifications are blocked. In-app WorkManager fallback sync is active.",
                        color = Color.White,
                        style = MaterialTheme.typography.bodySmall
                    )
                }
                Spacer(modifier = Modifier.height(12.dp))
            }

            // 4. Read-Only System Telemetry Card (PAPER Mode Locked)
            Card(modifier = Modifier.fillMaxWidth()) {
                Column(modifier = Modifier.padding(16.dp)) {
                    Text(
                        text = "SUI-Quant V1.0｜Mode: ${status?.executionMode ?: "PAPER"} (LIVE PHYSICALLY LOCKED)",
                        style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.Bold
                    )
                    Spacer(modifier = Modifier.height(6.dp))
                    Text("Trading Symbol: ${status?.tradingSymbol ?: "SUIUSDT"} (Ref: BTCUSDT)")
                    Text("D-01 State: ${status?.tradingState ?: "SYNCING"}")
                    Text("A-01 Regime: ${status?.latestRegime ?: "N/A"} (Conf: ${status?.latestRegimeConfidence ?: "0"})")
                    Text("E-01 Active Bundle: ${status?.activePaperBundleId ?: "N/A"} (Exit: ${status?.activePaperExitVersion ?: "C1-v1.0"})")
                    Text("Pinned In-Flight Exit: ${status?.pinnedPositionExitVersion ?: "NONE (FLAT)"}")
                    Text("R-01 Rolling 24h Drawdown: ${status?.rolling24hDrawdownPct ?: "0.000000"} / Limit ${status?.maxRolling24hLossLimitPct ?: "0.01"}")
                    Text("Stream Health: WS=${status?.wsStreamHealthy ?: false} | F-02 Heartbeat=${status?.f02HeartbeatHealthy ?: false}")
                }
            }

            Spacer(modifier = Modifier.height(12.dp))

            // 5. Authenticated Defensive Control Buttons (Strictly No Manual Order / No Unfreeze)
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(12.dp)
            ) {
                Button(
                    modifier = Modifier.weight(1f),
                    colors = ButtonDefaults.buttonColors(containerColor = Color(0xFFE65100)),
                    onClick = { pendingDefensiveAction = "FREEZE_ENTRY" }
                ) {
                    Text("Freeze New Entry")
                }
                Button(
                    modifier = Modifier.weight(1f),
                    colors = ButtonDefaults.buttonColors(containerColor = Color(0xFFB71C1C)),
                    onClick = { pendingDefensiveAction = "SECURITY_LOCKDOWN" }
                ) {
                    Text("Security Lockdown")
                }
            }

            Spacer(modifier = Modifier.height(16.dp))
            Text(
                text = "G-02 High/Critical Alert Feed (${activeAlerts.size})",
                style = MaterialTheme.typography.titleSmall,
                fontWeight = FontWeight.Bold
            )
            Spacer(modifier = Modifier.height(8.dp))

            LazyColumn(
                modifier = Modifier.fillMaxSize(),
                verticalArrangement = Arrangement.spacedBy(8.dp)
            ) {
                items(activeAlerts, key = { it.alertId }) { alert ->
                    Card(modifier = Modifier.fillMaxWidth()) {
                        Column(modifier = Modifier.padding(12.dp)) {
                            Text(
                                text = "[${alert.severity}] ${alert.dedupKey} (${alert.deliveryStatus})",
                                fontWeight = FontWeight.Bold
                            )
                            Text("Channel: ${alert.presentationChannel} | Seen: ${alert.firstSeenUtc}")
                            Text("Actions: ${alert.actionsTakenJson}")
                        }
                    }
                }
            }
        }
    }
}
