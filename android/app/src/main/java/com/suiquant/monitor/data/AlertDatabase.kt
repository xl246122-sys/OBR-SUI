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
 * Room offline cache entity for HIGH / CRITICAL / INFO alerts (§12.1, QA-11).
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
    val presentationChannel: String,
    val actionsTakenJson: String,
    val sanitizedDetailsJson: String,
    val userDismissedModal: Boolean = false
)

/**
 * Room offline cache entity for the latest status summary snapshot (`GET /api/v1/status/summary`).
 * Holds independent PAPER and LIVE position configs, active positions, and PnL.
 */
@Entity(tableName = "status_snapshots")
data class StatusSnapshotEntity(
    @PrimaryKey val snapshotKey: String = "LATEST",
    val generatedAtUtc: String,
    val executionMode: String,         // "PAPER" (default) or "LIVE"
    val liveTradingEnabled: Boolean = false,
    val tradingSymbol: String = "SUIUSDT",
    val suiBid: String = "0.0000",
    val suiAsk: String = "0.0000",
    val suiLastPrice: String = "0.0000",
    val btcLastPrice: String = "0.00",
    val tradingState: String = "READY_NO_POSITION",
    val allowNewEntry: Boolean = true,
    val latestRegime: String = "RANGING_NORMAL",
    val latestRegimeConfidence: String = "0.8120",
    val activePaperBundleId: String = "B-SUI-5M-V1.0",
    val activePaperExitVersion: String = "C1-v1.0",
    val shadowBundleId: String? = "B-SUI-5M-V2.0-SHADOW",
    val pinnedPositionExitVersion: String? = null,
    val hasActivePosition: Boolean = false,
    val activePositionSummary: String = "当前空仓 (FLAT)",
    val realizedPnlUsdt: String = "0.0000",
    val rolling24hDrawdownPct: String = "0.000000",
    val maxRolling24hLossLimitPct: String = "0.01",
    val undeliveredAlertsCount: Int = 0,
    val wsStreamHealthy: Boolean = true,
    val f02HeartbeatHealthy: Boolean = true,
    // Independent PAPER Workspace State & Config
    val paperHasActivePosition: Boolean = false,
    val paperActivePositionSummary: String = "模拟盘当前空仓 (FLAT) — 实时扫描 SUIUSDT 信号中",
    val paperRealizedPnlUsdt: String = "0.0000",
    val totalCapitalUsdt: String = "1000.00",
    val singleOrderUsdt: String = "100.00",
    val maxPositionPct: String = "10.00",
    val stopLossPct: String = "1.00",
    val takeProfitPct: String = "1.50",
    // Independent LIVE Workspace State & Config
    val liveHasActivePosition: Boolean = false,
    val liveActivePositionSummary: String = "实盘当前空仓 (FLAT) — 实盘开关开启后按配置执行",
    val liveRealizedPnlUsdt: String = "0.0000",
    val liveTotalCapitalUsdt: String = "500.00",
    val liveSingleOrderUsdt: String = "50.00",
    val liveMaxPositionPct: String = "10.00",
    val liveStopLossPct: String = "1.00",
    val liveTakeProfitPct: String = "1.50"
)

@Dao
interface AlertDao {
    @Query("SELECT * FROM alerts ORDER BY alertSeq DESC")
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

    @Query("SELECT * FROM status_snapshots WHERE snapshotKey = 'LATEST' LIMIT 1")
    suspend fun getLatestStatusSnapshotNow(): StatusSnapshotEntity?
}

@Database(
    entities = [AlertEntity::class, StatusSnapshotEntity::class],
    version = 3,
    exportSchema = false
)
abstract class AlertDatabase : RoomDatabase() {
    abstract fun alertDao(): AlertDao
}
