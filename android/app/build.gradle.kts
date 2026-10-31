import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.security.MessageDigest
import java.util.Locale
import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.google.devtools.ksp")
    id("com.google.gms.google-services")
}

val versionPropertiesFile = File(rootDir, "version.properties")
val versionProps = Properties()
if (versionPropertiesFile.exists()) {
    FileInputStream(versionPropertiesFile).use { versionProps.load(it) }
} else {
    versionProps.setProperty("major", "1")
    versionProps.setProperty("minor", "1")
    FileOutputStream(versionPropertiesFile).use { versionProps.store(it, "SUI-Quant Monitor Build Version Configuration") }
}

val currentMajor = versionProps.getProperty("major", "1").trim().toInt()
val currentMinor = versionProps.getProperty("minor", "1").trim().toInt()
val currentVersionName = String.format(Locale.US, "%d.%02d", currentMajor, currentMinor)
val currentVersionCode = currentMajor * 100 + currentMinor

android {
    namespace = "com.suiquant.monitor"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.suiquant.monitor"
        minSdk = 28
        targetSdk = 34
        versionCode = currentVersionCode
        versionName = currentVersionName

        // G-01 Governance Invariant: Mobile client strictly holds ZERO exchange API keys or trading secrets (§1, §12.1)
        buildConfigField("String", "EXECUTION_MODE_LOCK", "\"PAPER\"")
        buildConfigField("String", "TRADING_SYMBOL_LOCK", "\"SUIUSDT\"")
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    composeOptions {
        kotlinCompilerExtensionVersion = "1.5.14"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    val composeBom = platform("androidx.compose:compose-bom:2024.06.00")
    implementation(composeBom)
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.activity:activity-compose:1.9.0")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.2")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.8.2")

    // Room offline cache for alerts & status snapshots (§12.1, QA-11)
    val roomVersion = "2.6.1"
    implementation("androidx.room:room-runtime:$roomVersion")
    implementation("androidx.room:room-ktx:$roomVersion")
    ksp("androidx.room:room-compiler:$roomVersion")

    // WorkManager for offline reconnect incremental alert sync compensation (QA-11)
    implementation("androidx.work:work-runtime-ktx:2.9.0")

    // Firebase Cloud Messaging for high-priority background alert push (G-02)
    implementation(platform("com.google.firebase:firebase-bom:33.1.1"))
    implementation("com.google.firebase:firebase-messaging-ktx")

    // OkHttp + Retrofit for HTTPS communication with G01MobileGateway
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("com.squareup.retrofit2:retrofit:2.11.0")
    implementation("com.squareup.retrofit2:converter-kotlinx-serialization:2.11.0")
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.6.3")
}

afterEvaluate {
    tasks.register("exportReleaseApk") {
        group = "distribution"
        description = "Exports and renames built APK to D:\\OBR-SUI\\Release\\OBR-SUI V{major}.{minor}.apk and increments version by 0.01"
        dependsOn("assembleDebug")

        doLast {
            val releaseDir = File(rootDir.parentFile, "Release")
            if (!releaseDir.exists()) {
                releaseDir.mkdirs()
            }

            val sourceApk = File(layout.buildDirectory.asFile.get(), "outputs/apk/debug/app-debug.apk")
            if (!sourceApk.exists()) {
                throw GradleException("Built APK not found at: ${sourceApk.absolutePath}")
            }

            // Re-read version file to get the exact version for this build
            val vProps = Properties()
            FileInputStream(versionPropertiesFile).use { vProps.load(it) }
            val vMaj = vProps.getProperty("major", "1").trim().toInt()
            val vMin = vProps.getProperty("minor", "1").trim().toInt()
            val exportApkFileName = String.format(Locale.US, "OBR-SUI V%d.%02d.apk", vMaj, vMin)
            val targetApk = File(releaseDir, exportApkFileName)

            sourceApk.copyTo(targetApk, overwrite = true)

            // Compute SHA-256
            val digest = MessageDigest.getInstance("SHA-256")
            targetApk.inputStream().use { input ->
                val buf = ByteArray(8192)
                var bytesRead: Int
                while (input.read(buf).also { bytesRead = it } != -1) {
                    digest.update(buf, 0, bytesRead)
                }
            }
            val sha256 = digest.digest().joinToString("") { "%02X".format(it) }

            println("==================================================")
            println(" [APK RELEASE EXPORTED]")
            println(" Target APK: ${targetApk.absolutePath}")
            println(" File Size : ${targetApk.length()} bytes")
            println(" SHA-256   : $sha256")
            println(" Version   : V${vMaj}.${String.format(Locale.US, "%02d", vMin)}")
            println("==================================================")

            // Increment minor by 1 (每次增加0.01)
            var nextMajor = vMaj
            var nextMinor = vMin + 1
            if (nextMinor >= 100) {
                nextMajor += nextMinor / 100
                nextMinor %= 100
            }
            vProps.setProperty("major", nextMajor.toString())
            vProps.setProperty("minor", nextMinor.toString())
            FileOutputStream(versionPropertiesFile).use {
                vProps.store(it, "SUI-Quant Monitor Build Version Configuration (Next build incremented by 0.01)")
            }

            // Sync copy to Release directory
            val releaseVersionFile = File(releaseDir, "version.properties")
            FileOutputStream(releaseVersionFile).use {
                vProps.store(it, "Latest release version config (Next build incremented by 0.01)")
            }
        }
    }

    tasks.named("assembleDebug") {
        finalizedBy("exportReleaseApk")
    }
}
