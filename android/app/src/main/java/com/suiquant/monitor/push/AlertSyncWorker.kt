package com.suiquant.monitor.push

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import com.suiquant.monitor.data.AlertDao
import com.suiquant.monitor.data.AlertEntity

/**
 * WorkManager offline reconnect & FCM packet-drop compensation worker (`QA-11`).
 *
 * Whenever the phone regains network connectivity, wakes from Doze mode, or resumes foreground:
 * 1. Calls `GET /api/v1/alerts/sync?since_seq=0` on `G01MobileGateway` to fetch all HIGH and CRITICAL
 *    alerts whose server `delivery_status != DELIVERED`.
 * 2. Persists all recovered alerts into local Room `AlertDatabase`.
 * 3. Determines presentation channel (`FOREGROUND_MODAL_DIALOG`, `BACKGROUND_SYSTEM_NOTIFICATION`,
 *    or `IN_APP_PERMISSION_FALLBACK_BANNER` if `POST_NOTIFICATIONS` is denied).
 * 4. Signs an Ed25519 `ACK_ALERT` `MobileCommandEnvelope` with the Android Keystore keypair and posts
 *    it to the server to complete the `DELIVERED` closed loop (`missed_alert_loss_rate == 0%`).
 */
class AlertSyncWorker(
    appContext: Context,
    workerParams: WorkerParameters
) : CoroutineWorker(appContext, workerParams) {

    override suspend fun doWork(): Result {
        val gatewayClient = GatewayClientRegistry.client ?: return Result.retry()
        val alertDao: AlertDao = GatewayClientRegistry.alertDao ?: return Result.retry()

        return try {
            val pendingAlerts = gatewayClient.fetchPendingHighCriticalAlerts(sinceSeq = 0L)
            if (pendingAlerts.isEmpty()) {
                return Result.success()
            }

            val isForeground = AppForegroundTracker.isInForeground
            val notificationsEnabled = SuiQuantFcmService.areSystemNotificationsEnabled(applicationContext)

            val channel = when {
                isForeground && notificationsEnabled -> "FOREGROUND_MODAL_DIALOG"
                !isForeground && notificationsEnabled -> "BACKGROUND_SYSTEM_NOTIFICATION"
                else -> "IN_APP_PERMISSION_FALLBACK_BANNER"
            }

            alertDao.upsertAlerts(pendingAlerts)

            for (alert in pendingAlerts) {
                if (channel == "BACKGROUND_SYSTEM_NOTIFICATION") {
                    SuiQuantFcmService.postHighPrioritySystemNotification(
                        context = applicationContext,
                        alertId = alert.alertId,
                        severity = alert.severity,
                        source = alert.source,
                        summary = alert.dedupKey
                    )
                }
                val ackOk = gatewayClient.sendSignedAlertAck(
                    alertId = alert.alertId,
                    presentationChannel = channel
                )
                if (ackOk) {
                    alertDao.markAlertDelivered(alert.alertId, channel)
                }
            }
            Result.success()
        } catch (_: Exception) {
            Result.retry()
        }
    }
}

interface G01GatewayApiClient {
    suspend fun fetchPendingHighCriticalAlerts(sinceSeq: Long): List<AlertEntity>
    suspend fun sendSignedAlertAck(alertId: String, presentationChannel: String): Boolean
    suspend fun sendSignedDefensiveControl(action: String, reason: String): Boolean
}

object GatewayClientRegistry {
    @Volatile
    var client: G01GatewayApiClient? = null

    @Volatile
    var alertDao: AlertDao? = null
}
