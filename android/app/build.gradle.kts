plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.google.devtools.ksp")
    id("com.google.gms.google-services")
}

android {
    namespace = "com.suiquant.monitor"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.suiquant.monitor"
        minSdk = 28
        targetSdk = 34
        versionCode = 1
        versionName = "1.0.0-paper"

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
