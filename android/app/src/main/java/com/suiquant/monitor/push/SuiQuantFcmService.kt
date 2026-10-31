package com.suiquant.monitor.push

import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.work.Constraints
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage

/**
 * G-02 High-Priority FCM Push Receiver (§12.2, QA-11).
 *
 * Routing Rules:
 * 1. When app is in FOREGROUND: writes alert to Room DB so Compose UI immediately pops a native modal
 *    `AlertDialog` (`FOREGROUND_MODAL_DIALOG`) and dispatches a signed `ACK_ALERT` command.
 * 2. When app is in BACKGROUND and `POST_NOTIFICATIONS` permission is granted: posts a system notification
 *    on `NotificationChannel(IMPORTANCE_HIGH)` (`BACKGROUND_SYSTEM_NOTIFICATION`) and enqueues `AlertSyncWorker`
 *    to persist and acknowledge receipt (`DELIVERED`).
 * 3. When `POST_NOTIFICATIONS` is denied or FCM data is incomplete: schedules `AlertSyncWorker` to pull all
 *    unacknowledged `HIGH / CRITICAL` alerts via `GET /api/v1/alerts/sync` as soon as connectivity allows.
 */
class SuiQuantFcmService : FirebaseMessagingService() {

    override fun onCreate() {
        super.onCreate()
        ensureHighPriorityNotificationChannel(applicationContext)
    }

    override fun onMessageReceived(message: RemoteMessage) {
        super.onMessageReceived(message)
        val data = message.data
        val alertId = data["alert_id"] ?: return
        val severity = data["severity"] ?: "HIGH"
        val source = data["source"] ?: "SUI-QUANT-CLOUD"
        val dedupKey = data["dedup_key"] ?: "ALERT"
        val isAppInForeground = AppForegroundTracker.isInForeground

        if (!isAppInForeground && areSystemNotificationsEnabled(applicationContext)) {
            postHighPrioritySystemNotification(
                context = applicationContext,
                alertId = alertId,
                severity = severity,
                source = source,
                summary = dedupKey
            )
        }

        // Always trigger incremental WorkManager sync to guarantee Room persistence + Ed25519 signed ACK
        enqueueImmediateIncrementalSync(applicationContext)
    }

    override fun onNewToken(token: String) {
        super.onNewToken(token)
        enqueueImmediateIncrementalSync(applicationContext)
    }

    companion object {
        const val CRITICAL_CHANNEL_ID = "sui_quant_critical_alerts_v1"
        private const val SYNC_WORK_NAME = "sui_quant_g02_alert_incremental_sync"

        fun ensureHighPriorityNotificationChannel(context: Context) {
            val manager = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            val existing = manager.getNotificationChannel(CRITICAL_CHANNEL_ID)
            if (existing == null) {
                val channel = NotificationChannel(
                    CRITICAL_CHANNEL_ID,
                    "SUI-Quant Critical & High Risk Alerts",
                    NotificationManager.IMPORTANCE_HIGH
                ).apply {
                    description = "Immediate risk veto, protection order, and security lockdown alerts (G-02)"
                    enableVibration(true)
                    setBypassDnd(true)
                }
                manager.createNotificationChannel(channel)
            }
        }

        fun areSystemNotificationsEnabled(context: Context): Boolean {
            return NotificationManagerCompat.from(context).areNotificationsEnabled()
        }

        fun postHighPrioritySystemNotification(
            context: Context,
            alertId: String,
            severity: String,
            source: String,
            summary: String
        ) {
            ensureHighPriorityNotificationChannel(context)
            val notification = NotificationCompat.Builder(context, CRITICAL_CHANNEL_ID)
                .setSmallIcon(android.R.drawable.stat_sys_warning)
                .setContentTitle("[$severity] SUI-Quant ($source)")
                .setContentText("$summary (ID: $alertId)")
                .setPriority(NotificationCompat.PRIORITY_MAX)
                .setCategory(NotificationCompat.CATEGORY_ALARM)
                .setAutoCancel(true)
                .build()

            try {
                NotificationManagerCompat.from(context).notify(alertId.hashCode(), notification)
            } catch (_: SecurityException) {
                // Handled by persistent UI permission warning banner + WorkManager sync fallback (QA-11)
            }
        }

        fun enqueueImmediateIncrementalSync(context: Context) {
            val constraints = Constraints.Builder()
                .setRequiredNetworkType(NetworkType.CONNECTED)
                .build()
            val workRequest = OneTimeWorkRequestBuilder<AlertSyncWorker>()
                .setConstraints(constraints)
                .build()
            WorkManager.getInstance(context).enqueueUniqueWork(
                SYNC_WORK_NAME,
                ExistingWorkPolicy.REPLACE,
                workRequest
            )
        }
    }
}

object AppForegroundTracker {
    @Volatile
    var isInForeground: Boolean = false
}
