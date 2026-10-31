package com.suiquant.monitor.data

import androidx.room.Dao
import androidx.room.Database
import androidx.room.Entity
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.PrimaryKey
import androidx.room.Query
import androidx.room.RoomDatabase
import kotlinx.coroutines.flow.Flow

/**
 * Room offline cache entity for HIGH / CRITICAL alerts and daily digest items (§12.1, QA-11).
 * Persists alerts across app restarts and offline periods until acknowledged as DELIVERED.
 */
@Entity(tableName = "alerts")
data class AlertEntity(
    @PrimaryKey val alertId: String,
    val alertSeq: Long,
    val severity: String,              // "INFO", "HIGH", "CRITICAL"
    val source: String,
    val dedupKey: String,
    val firstSeenUtc: String,
    val lastSeenUtc: String,
    val occurrenceCount: Int,
    val deliveryStatus: String,        // "PENDING", "SENT_AWAITING_ACK", "DELIVERED"
    val presentationChannel: String,   // "FOREGROUND_MODAL_DIALOG", "BACKGROUND_SYSTEM_NOTIFICATION", "IN_APP_PERMISSION_FALLBACK_BANNER"
    val actionsTakenJson: String,
    val sanitizedDetailsJson: String,
    val userDismissedModal: Boolean = false
)

/**
 * Room offline cache entity for the latest read-only status summary snapshot (`GET /api/v1/status/summary`).
 */
@Entity(tableName = "status_snapshots")
data class StatusSnapshotEntity(
    @PrimaryKey val snapshotKey: String = "LATEST",
    val generatedAtUtc: String,
    val executionMode: String,         // Strictly "PAPER"
    val tradingSymbol: String,         // Strictly "SUIUSDT"
    val tradingState: String,
    val allowNewEntry: Boolean,
    val latestRegime: String,
    val latestRegimeConfidence: String,
    val activePaperBundleId: String,
    val activePaperExitVersion: String,
    val shadowBundleId: String?,
    val pinnedPositionExitVersion: String?,
    val hasActivePosition: Boolean,
    val rolling24hDrawdownPct: String,
    val maxRolling24hLossLimitPct: String,
    val undeliveredAlertsCount: Int,
    val wsStreamHealthy: Boolean,
    val f02HeartbeatHealthy: Boolean
)

@Dao
interface AlertDao {
    @Query("SELECT * FROM alerts WHERE severity IN ('HIGH', 'CRITICAL') ORDER BY alertSeq DESC")
    fun observeHighAndCriticalAlerts(): Flow<List<AlertEntity>>

    @Query("SELECT * FROM alerts WHERE severity IN ('HIGH', 'CRITICAL') AND userDismissedModal = 0 ORDER BY alertSeq ASC")
    fun observeActiveForegroundModals(): Flow<List<AlertEntity>>

    @Query("SELECT * FROM alerts WHERE deliveryStatus != 'DELIVERED' AND severity IN ('HIGH', 'CRITICAL') ORDER BY alertSeq ASC")
    suspend fun listUnacknowledgedAlerts(): List<AlertEntity>

    @Query("SELECT COALESCE(MAX(alertSeq), 0) FROM alerts")
    suspend fun getMaxSyncedAlertSeq(): Long

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsertAlerts(alerts: List<AlertEntity>)

    @Query("UPDATE alerts SET deliveryStatus = 'DELIVERED', presentationChannel = :channel WHERE alertId = :alertId")
    suspend fun markAlertDelivered(alertId: String, channel: String)

    @Query("UPDATE alerts SET userDismissedModal = 1 WHERE alertId = :alertId")
    suspend fun dismissForegroundModal(alertId: String)

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsertStatusSnapshot(snapshot: StatusSnapshotEntity)

    @Query("SELECT * FROM status_snapshots WHERE snapshotKey = 'LATEST' LIMIT 1")
    fun observeLatestStatusSnapshot(): Flow<StatusSnapshotEntity?>
}

@Database(
    entities = [AlertEntity::class, StatusSnapshotEntity::class],
    version = 1,
    exportSchema = false
)
abstract class AlertDatabase : RoomDatabase() {
    abstract fun alertDao(): AlertDao
}
