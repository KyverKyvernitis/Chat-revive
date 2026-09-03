package dev.core.worker;

import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.content.res.AssetManager;
import android.os.BatteryManager;
import android.os.Build;
import android.os.PowerManager;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.HashSet;
import java.util.Locale;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.zip.ZipEntry;
import java.util.zip.ZipInputStream;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/**
 * Autobuilder controlado do Core Worker.
 *
 * O primeiro APK compatível continua sendo compilado no Termux, mas não contém
 * JDK/Gradle/SDK nos assets. Esta classe migra um bundle legado se ele já existir,
 * ou baixa o toolchain externo autenticado para slots privados e só anuncia
 * apk-builder depois de cinco smokes reais. A VPS nunca executa Gradle.
 */
final class CoreWorkerApkBuildManager {
    private static final String PREFS = "core_worker_private";
    private static final String TOOLCHAIN_ASSET = "core-linux/android-builder/android-builder-toolchain.zip";
    private static final String TOOLCHAIN_CHUNKS_MANIFEST = "core-linux/android-builder/android-builder-toolchain.parts.json";
    private static final String TOOLCHAIN_CHUNKS_DIR = "core-linux/android-builder";
    private static final String BOX64_ASSET = "core-linux/bin/box64";
    private static final String EMBEDDED_MANIFEST_ASSET = "core-linux/embedded-binaries-manifest.json";
    private static final long PREFLIGHT_CACHE_MS = 45_000L;
    private static final long PERSISTED_READY_MAX_MS = TimeUnit.MINUTES.toMillis(5);
    private static final long MAX_TOOLCHAIN_EXPANDED_BYTES = 4L * 1024L * 1024L * 1024L;
    private static final int MAX_TOOLCHAIN_ENTRIES = 50_000;

    private static volatile JSONObject cachedPreflight;
    private static volatile long cachedPreflightAt;
    private static final AtomicBoolean preflightRefreshRunning = new AtomicBoolean(false);
    private static final ExecutorService preflightExecutor = Executors.newSingleThreadExecutor(r -> {
        Thread thread = new Thread(r, "core-worker-apk-builder-preflight");
        thread.setDaemon(true);
        return thread;
    });

    private CoreWorkerApkBuildManager() { }

    static boolean supports(String type) {
        String value = type == null ? "" : type.trim();
        return "apk_build_debug".equals(value)
                || "apk_publish_last".equals(value)
                || "apk_builder_status".equals(value);
    }

    static JSONArray availableTasks(Context context) {
        JSONArray out = new JSONArray().put("apk_builder_status");
        JSONObject preflight = preflight(context, false);
        if (preflight.optBoolean("ready", false)) out.put("apk_build_debug");
        if (preflight.optBoolean("ready", false) || preflight.optBoolean("publishReady", false)) {
            out.put("apk_publish_last");
        }
        return out;
    }

    static JSONArray dynamicCapabilities(Context context) {
        JSONArray out = new JSONArray();
        JSONObject preflight = preflight(context, false);
        if (preflight.optBoolean("ready", false)) {
            out.put("apk-builder");
            out.put("apk-self-builder");
        }
        if (preflight.optBoolean("publishReady", false)) out.put("apk-publisher");
        return out;
    }

    static JSONArray dynamicRoles(Context context) {
        JSONArray out = new JSONArray();
        JSONObject preflight = preflight(context, false);
        if (preflight.optBoolean("ready", false)) {
            out.put("apk-builder");
        }
        return out;
    }

    static void refreshAsync(Context rawContext) {
        if (rawContext == null || !preflightRefreshRunning.compareAndSet(false, true)) return;
        Context context = rawContext.getApplicationContext();
        preflightExecutor.execute(() -> {
            try { preflight(context, true); }
            finally { preflightRefreshRunning.set(false); }
        });
    }

    static JSONObject preflight(Context rawContext, boolean force) {
        Context context = rawContext.getApplicationContext();
        long now = System.currentTimeMillis();
        JSONObject current = cachedPreflight;
        if (!force) {
            if (current != null && now - cachedPreflightAt < PREFLIGHT_CACHE_MS) {
                return cloneJson(current);
            }
            JSONObject persisted = readPersistedPreflight(context, now);
            cachedPreflight = persisted;
            cachedPreflightAt = now;
            refreshAsync(context);
            return cloneJson(persisted);
        }

        PowerManager.WakeLock provisionWakeLock = null;
        try {
            PowerManager power = (PowerManager) context.getSystemService(Context.POWER_SERVICE);
            if (power != null) {
                provisionWakeLock = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "CoreWorker:ApkBuilderProvision");
                provisionWakeLock.setReferenceCounted(false);
                provisionWakeLock.acquire(TimeUnit.MINUTES.toMillis(30));
            }
            provisionPrivateAssets(context);
            JSONObject value = callPythonPreflight(context, true);
            value = finalizeToolchainPreflight(context, value);
            SharedPreferences preflightPrefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
            value.put("toolchainReleaseFingerprint", preflightPrefs.getString("apk_self_builder_toolchain_fingerprint", ""));
            value.put("toolchainUpdateState", preflightPrefs.getString("apk_self_builder_toolchain_update_state", value.optString("toolchainUpdateState", "")));
            value.put("appVersionCode", BuildConfig.VERSION_CODE);
            value.put("checkedAt", now);
            cachedPreflight = value;
            cachedPreflightAt = now;
            persistPreflight(context, value);
            return cloneJson(value);
        } catch (Throwable error) {
            JSONObject failed = new JSONObject();
            try {
                failed.put("ok", false);
                failed.put("ready", false);
                failed.put("publishReady", latestArtifactAvailable(context));
                failed.put("state", "apk_self_builder_preflight_error");
                failed.put("summary", "Autobuild do APK indisponível: " + shortThrowable(error));
                failed.put("error", shortThrowable(error));
                failed.put("appVersionCode", BuildConfig.VERSION_CODE);
                failed.put("checkedAt", now);
                failed.put("updatedAt", now);
            } catch (Throwable ignored) { }
            cachedPreflight = failed;
            cachedPreflightAt = now;
            persistPreflight(context, failed);
            return cloneJson(failed);
        } finally {
            if (provisionWakeLock != null && provisionWakeLock.isHeld()) provisionWakeLock.release();
        }
    }

    static JSONObject execute(Context rawContext, String type, JSONObject payload, String serverUrl) throws Exception {
        Context context = rawContext.getApplicationContext();
        if (!supports(type)) {
            return new JSONObject().put("ok", false).put("type", type).put("error", "task de autobuild não permitida");
        }
        JSONObject gate = preflight(context, true);
        if ("apk_build_debug".equals(type) && !gate.optBoolean("ready", false)) {
            return new JSONObject()
                    .put("ok", false)
                    .put("type", type)
                    .put("message", gate.optString("summary", "autobuilder não está pronto"))
                    .put("error", gate.optString("summary", "autobuilder não está pronto"))
                    .put("preflight", gate)
                    .put("retryable", true);
        }
        if ("apk_publish_last".equals(type)
                && !gate.optBoolean("ready", false)
                && !gate.optBoolean("publishReady", false)) {
            return new JSONObject()
                    .put("ok", false)
                    .put("type", type)
                    .put("message", "nenhum APK autoconstrído disponível para republicar")
                    .put("error", "nenhum artifact persistido")
                    .put("preflight", gate);
        }

        SharedPreferences prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        String workerId = CoreWorkerRuntimeIdentity.runtimeWorkerId(context);
        String token = prefs.getString("worker_token", "").trim();
        if (workerId.isEmpty() || token.isEmpty()) {
            return new JSONObject().put("ok", false).put("type", type)
                    .put("error", "APK não pareado; autobuilder sem credenciais de publicação");
        }

        PowerManager.WakeLock wakeLock = null;
        long started = System.currentTimeMillis();
        try {
            PowerManager power = (PowerManager) context.getSystemService(Context.POWER_SERVICE);
            if (power != null) {
                wakeLock = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "CoreWorker:ApkSelfBuild");
                wakeLock.setReferenceCounted(false);
                wakeLock.acquire(TimeUnit.HOURS.toMillis(4));
            }
            provisionPrivateAssets(context);
            if (!Python.isStarted()) Python.start(new AndroidPlatform(context));
            PyObject module = Python.getInstance().getModule("coreworker.apk_self_builder");
            JSONObject effectivePayload = payload == null ? new JSONObject() : new JSONObject(payload.toString());
            effectivePayload.put("builderResources", buildResourceSnapshot(context));
            PyObject response = module.callAttr(
                    "run",
                    type,
                    effectivePayload.toString(),
                    context.getFilesDir().getAbsolutePath(),
                    context.getCacheDir().getAbsolutePath(),
                    context.getApplicationInfo().nativeLibraryDir,
                    serverUrl == null ? "" : serverUrl,
                    workerId,
                    token,
                    BuildConfig.VERSION_NAME
            );
            JSONObject result = new JSONObject(response == null ? "{}" : response.toString());
            result.put("durationMs", Math.max(0L, System.currentTimeMillis() - started));
            result.put("bootstrapBuilder", "termux");
            result.put("currentBuilder", "core-worker-apk");
            prefs.edit()
                    .putLong("apk_self_builder_last_run_at", System.currentTimeMillis())
                    .putString("apk_self_builder_last_task", type)
                    .putBoolean("apk_self_builder_last_ok", result.optBoolean("ok", false))
                    .putString("apk_self_builder_last_summary", compact(result.optString("summary", result.optString("error", ""))))
                    .apply();
            cachedPreflight = null;
            cachedPreflightAt = 0L;
            return result;
        } finally {
            if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        }
    }

    private static JSONObject buildResourceSnapshot(Context context) {
        JSONObject out = new JSONObject();
        try {
            Intent battery = context.registerReceiver(null, new IntentFilter(Intent.ACTION_BATTERY_CHANGED));
            if (battery != null) {
                int level = battery.getIntExtra(BatteryManager.EXTRA_LEVEL, -1);
                int scale = battery.getIntExtra(BatteryManager.EXTRA_SCALE, 100);
                int temp = battery.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, -1);
                int status = battery.getIntExtra(BatteryManager.EXTRA_STATUS, -1);
                out.put("batteryPercent", level >= 0 ? Math.round(level * 100.0 / Math.max(1, scale)) : -1);
                out.put("charging", status == BatteryManager.BATTERY_STATUS_CHARGING
                        || status == BatteryManager.BATTERY_STATUS_FULL
                        || battery.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) != 0);
                if (temp >= 0) out.put("temperatureC", temp / 10.0);
            }
        } catch (Throwable ignored) { }
        try {
            PowerManager power = (PowerManager) context.getSystemService(Context.POWER_SERVICE);
            if (power != null && Build.VERSION.SDK_INT >= 29) {
                out.put("thermalStatus", power.getCurrentThermalStatus());
            }
        } catch (Throwable ignored) { }
        try {
            File files = context.getFilesDir();
            out.put("storageFreeBytes", files.getUsableSpace());
            out.put("storageTotalBytes", files.getTotalSpace());
        } catch (Throwable ignored) { }
        return out;
    }

    private static JSONObject callPythonPreflight(Context context, boolean runSmoke) throws Exception {
        if (!Python.isStarted()) Python.start(new AndroidPlatform(context));
        PyObject module = Python.getInstance().getModule("coreworker.apk_self_builder");
        PyObject response = module.callAttr(
                "preflight",
                context.getFilesDir().getAbsolutePath(),
                context.getApplicationInfo().nativeLibraryDir,
                runSmoke
        );
        return new JSONObject(response == null ? "{}" : response.toString());
    }

    private static JSONObject readPersistedPreflight(Context context, long now) {
        SharedPreferences prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        int version = prefs.getInt("apk_self_builder_checked_version_code", 0);
        long checkedAt = prefs.getLong("apk_self_builder_checked_at", 0L);
        boolean fresh = version == BuildConfig.VERSION_CODE
                && checkedAt > 0L
                && now - checkedAt <= PERSISTED_READY_MAX_MS;
        JSONObject value = new JSONObject();
        try {
            value.put("ok", fresh && prefs.getBoolean("apk_self_builder_ready", false));
            value.put("ready", fresh && prefs.getBoolean("apk_self_builder_ready", false));
            value.put("publishReady", prefs.getBoolean("apk_self_builder_publish_ready", false) && latestArtifactAvailable(context));
            value.put("state", fresh ? prefs.getString("apk_self_builder_state", "apk_self_builder_refreshing") : "apk_self_builder_refreshing");
            value.put("summary", fresh
                    ? prefs.getString("apk_self_builder_summary", "Autobuild aguardando refresh")
                    : "Autobuild aguardando preflight real em segundo plano");
            value.put("refreshing", true);
            value.put("appVersionCode", BuildConfig.VERSION_CODE);
            value.put("checkedAt", checkedAt);
            value.put("toolchainReleaseFingerprint", prefs.getString("apk_self_builder_toolchain_fingerprint", ""));
            value.put("toolchainUpdateState", prefs.getString("apk_self_builder_toolchain_update_state", ""));
        } catch (Throwable ignored) { }
        return value;
    }

    private static void persistPreflight(Context context, JSONObject value) {
        try {
            context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
                    .putBoolean("apk_self_builder_ready", value.optBoolean("ready", false))
                    .putBoolean("apk_self_builder_publish_ready", value.optBoolean("publishReady", false))
                    .putString("apk_self_builder_state", value.optString("state", ""))
                    .putString("apk_self_builder_summary", compact(value.optString("summary", "")))
                    .putInt("apk_self_builder_checked_version_code", BuildConfig.VERSION_CODE)
                    .putLong("apk_self_builder_checked_at", System.currentTimeMillis())
                    .apply();
        } catch (Throwable ignored) { }
    }

    private static boolean latestArtifactAvailable(Context context) {
        File metadata = new File(context.getFilesDir(), "apk-self-builder/artifacts/latest-artifact.json");
        if (!metadata.isFile()) return false;
        try {
            byte[] raw = java.nio.file.Files.readAllBytes(metadata.toPath());
            JSONObject json = new JSONObject(new String(raw, StandardCharsets.UTF_8));
            String path = json.optString("artifact_path", "");
            File apk = new File(path);
            File root = new File(context.getFilesDir(), "apk-self-builder").getCanonicalFile();
            File canonical = apk.getCanonicalFile();
            return canonical.isFile() && canonical.length() > 1024L * 1024L
                    && canonical.getPath().startsWith(root.getPath() + File.separator);
        } catch (Throwable ignored) {
            return false;
        }
    }

    private static synchronized void provisionPrivateAssets(Context context) throws Exception {
        SharedPreferences prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        File builder = new File(context.getFilesDir(), "apk-self-builder");
        File toolchain = new File(builder, "toolchain");
        File manifest = new File(toolchain, "manifest.json");
        File repro = new File(builder, "repro-assets");
        if (!builder.exists() && !builder.mkdirs()) throw new IllegalStateException("não consegui criar diretório do autobuilder");
        if (!repro.exists() && !repro.mkdirs()) throw new IllegalStateException("não consegui criar repro-assets");

        // Compatibilidade de migração: instalações antigas podem ter o toolchain em
        // ZIP/.cwpart nos assets. Novos APKs nunca geram esses assets; só os lemos
        // uma vez se ainda não existir um toolchain privado.
        if (!manifest.isFile()) {
            provisionLegacyToolchainIfPresent(context, builder, toolchain, repro, prefs);
        }

        try {
            provisionExternalToolchain(context, builder, toolchain, prefs);
        } catch (Throwable error) {
            prefs.edit()
                    .putString("apk_self_builder_toolchain_update_state", "failed_transient")
                    .putString("apk_self_builder_toolchain_update_error", compact(shortThrowable(error)))
                    .putLong("apk_self_builder_toolchain_update_at", System.currentTimeMillis())
                    .apply();
            // Um update externo quebrado não destrói o último toolchain conhecido.
            // Se não houver fallback local, o preflight precisa falhar e o Termux
            // permanece como builder canônico.
            if (!new File(toolchain, "manifest.json").isFile()) throw error;
        }

        retainAssetIfPresent(context, BOX64_ASSET, new File(repro, BOX64_ASSET));
        retainAssetIfPresent(context, EMBEDDED_MANIFEST_ASSET, new File(repro, EMBEDDED_MANIFEST_ASSET));
    }

    private static void provisionLegacyToolchainIfPresent(
            Context context, File builder, File toolchain, File repro, SharedPreferences prefs) throws Exception {
        boolean chunkedAsset = assetExists(context.getAssets(), TOOLCHAIN_CHUNKS_MANIFEST);
        boolean zipAsset = assetExists(context.getAssets(), TOOLCHAIN_ASSET);
        if (!chunkedAsset && !zipAsset) return;

        JSONObject chunks = chunkedAsset ? readChunkDescriptor(context) : null;
        JSONObject chunksArchive = chunks == null ? null : chunks.optJSONObject("archive");
        String chunksSha = chunksArchive == null ? "" : chunksArchive.optString("sha256", "").toLowerCase(Locale.ROOT);
        File retained;
        if (chunkedAsset) {
            retained = materializeChunkedToolchain(context, builder, repro, chunks);
        } else {
            retained = new File(repro, TOOLCHAIN_ASSET);
            copyAsset(context.getAssets(), TOOLCHAIN_ASSET, retained);
        }
        File staging = new File(builder, "toolchain-next");
        deleteTree(staging);
        if (!staging.mkdirs()) throw new IllegalStateException("não consegui criar staging legado do toolchain");
        try {
            extractZip(retained, staging);
            File stagedManifest = new File(staging, "manifest.json");
            restoreExecutablePaths(staging, stagedManifest);
            promoteToolchain(builder, toolchain, staging, prefs,
                    chunksSha.matches("[0-9a-f]{64}") ? chunksSha : "legacy-assets");
            prefs.edit()
                    .putInt("apk_self_builder_asset_version_code", BuildConfig.VERSION_CODE)
                    .putString("apk_self_builder_toolchain_update_state", "legacy_migrated")
                    .putLong("apk_self_builder_toolchain_update_at", System.currentTimeMillis())
                    .apply();
        } finally {
            if (chunkedAsset) retained.delete();
        }
    }

    private static void provisionExternalToolchain(
            Context context, File builder, File toolchain, SharedPreferences prefs) throws Exception {
        String token = prefs.getString("worker_token", "").trim();
        String workerId = CoreWorkerRuntimeIdentity.runtimeWorkerId(context);
        String physicalWorkerId = CoreWorkerRuntimeIdentity.canonicalWorkerId(context);
        String serverUrl = prefs.getString("server_url", "").trim();
        if (serverUrl.isEmpty()) serverUrl = BuildConfig.CORE_WORKER_VPS_URL == null ? "" : BuildConfig.CORE_WORKER_VPS_URL.trim();
        if (token.isEmpty() || workerId.isEmpty() || serverUrl.isEmpty()) {
            prefs.edit().putString("apk_self_builder_toolchain_update_state", "blocked_by_config").apply();
            return;
        }
        serverUrl = trimTrailingSlash(serverUrl);
        URL latestUrl = sameOriginUrl(serverUrl, serverUrl + "/core-worker/toolchain/latest?worker_id=" + urlEncode(workerId));
        HttpResult latest = authenticatedGet(latestUrl, workerId, token, 512 * 1024L);
        verifySignedResponse(latest, token);
        JSONObject target = new JSONObject(new String(latest.body, StandardCharsets.UTF_8));
        if (!"core-worker-toolchain-release-v2".equals(target.optString("schema", ""))) {
            throw new IllegalStateException("manifesto latest do toolchain possui schema inválido");
        }
        String fingerprint = target.optString("toolchainFingerprint", "").trim().toLowerCase(Locale.ROOT);
        String sha256 = target.optString("sha256", "").trim().toLowerCase(Locale.ROOT);
        long compactBytes = target.optLong("bytes", 0L);
        long expandedBytes = target.optLong("expandedBytes", 0L);
        if (!fingerprint.matches("[0-9a-f]{64}") || !fingerprint.equals(sha256)
                || compactBytes <= 0L || compactBytes > 1024L * 1024L * 1024L
                || expandedBytes <= 0L || expandedBytes > MAX_TOOLCHAIN_EXPANDED_BYTES) {
            throw new IllegalStateException("metadados de tamanho/fingerprint inválidos no toolchain externo");
        }
        String declaredPhysical = target.optString("physicalWorkerId", "").trim();
        if (!declaredPhysical.isEmpty() && !physicalWorkerId.isEmpty() && !declaredPhysical.equals(physicalWorkerId)) {
            throw new IllegalStateException("toolchain publicado para outro worker físico");
        }
        JSONObject versions = target.optJSONObject("versions");
        if (versions == null || versions.optInt("jdkMajor", 0) != 17
                || !"8.9".equals(versions.optString("gradle", ""))
                || versions.optInt("compileSdk", 0) != 34
                || !"34.0.0".equals(versions.optString("buildTools", ""))) {
            throw new IllegalStateException("matriz de versões do toolchain externo incompatível");
        }

        File manifest = new File(toolchain, "manifest.json");
        String currentFingerprint = prefs.getString("apk_self_builder_toolchain_fingerprint", "").trim().toLowerCase(Locale.ROOT);
        if (manifest.isFile() && fingerprint.equals(currentFingerprint)) {
            restoreExecutablePaths(toolchain, manifest);
            prefs.edit().putString("apk_self_builder_toolchain_update_state", "succeeded").apply();
            return;
        }

        long requiredFree = compactBytes + expandedBytes + 256L * 1024L * 1024L;
        long usable = builder.getUsableSpace();
        if (usable > 0L && usable < requiredFree) {
            prefs.edit()
                    .putString("apk_self_builder_toolchain_update_state", "preflight_blocked")
                    .putString("apk_self_builder_toolchain_update_error", "espaço insuficiente para atualizar toolchain")
                    .putLong("apk_self_builder_toolchain_required_bytes", requiredFree)
                    .putLong("apk_self_builder_toolchain_free_bytes", usable)
                    .apply();
            if (!manifest.isFile()) throw new IllegalStateException("preflight_blocked: espaço insuficiente para toolchain");
            return;
        }

        String rawUrl = target.optString("url", "").trim();
        URL releaseUrl = sameOriginUrl(serverUrl, rawUrl.startsWith("http://") || rawUrl.startsWith("https://")
                ? rawUrl : serverUrl + (rawUrl.startsWith("/") ? rawUrl : "/" + rawUrl));
        File archivePart = new File(builder, "toolchain-download.part");
        File staging = new File(builder, "toolchain-next");
        deleteTree(staging);
        archivePart.delete();
        prefs.edit()
                .putString("apk_self_builder_toolchain_update_state", "toolchain_downloading")
                .putString("apk_self_builder_toolchain_target", fingerprint)
                .putLong("apk_self_builder_toolchain_update_at", System.currentTimeMillis())
                .apply();
        try {
            downloadAuthenticated(releaseUrl, workerId, token, archivePart, compactBytes, fingerprint);
            if (!staging.mkdirs()) throw new IllegalStateException("não consegui criar staging do toolchain externo");
            extractZip(archivePart, staging);
            File stagedManifest = new File(staging, "manifest.json");
            restoreExecutablePaths(staging, stagedManifest);
            JSONObject internal = new JSONObject(new String(java.nio.file.Files.readAllBytes(stagedManifest.toPath()), StandardCharsets.UTF_8));
            if (!"core-worker-android-builder-v2".equals(internal.optString("schema", ""))) {
                throw new IllegalStateException("schema interno do toolchain externo inválido");
            }
            String internalPhysical = internal.optString("physicalWorkerId", "").trim();
            if (!internalPhysical.isEmpty() && !physicalWorkerId.isEmpty() && !internalPhysical.equals(physicalWorkerId)) {
                throw new IllegalStateException("toolchain interno pertence a outro worker físico");
            }
            prefs.edit().putString("apk_self_builder_toolchain_update_state", "validating").apply();
            promoteToolchain(builder, toolchain, staging, prefs, fingerprint);
            prefs.edit().putString("apk_self_builder_toolchain_update_state", "verifying_runtime").apply();
        } finally {
            archivePart.delete();
            if (staging.exists()) deleteTree(staging);
        }
    }

    private static void promoteToolchain(
            File builder, File toolchain, File staging, SharedPreferences prefs, String newFingerprint) throws Exception {
        File previous = new File(builder, "toolchain-previous");
        String oldFingerprint = prefs.getString("apk_self_builder_toolchain_fingerprint", "");
        deleteTree(previous);
        if (toolchain.exists()) {
            if (!toolchain.renameTo(previous)) {
                copyTree(toolchain, previous);
                deleteTree(toolchain);
            }
        }
        try {
            if (!staging.renameTo(toolchain)) {
                copyTree(staging, toolchain);
                deleteTree(staging);
            }
        } catch (Throwable error) {
            deleteTree(toolchain);
            if (previous.exists()) {
                if (!previous.renameTo(toolchain)) copyTree(previous, toolchain);
            }
            throw error;
        }
        prefs.edit()
                .putString("apk_self_builder_previous_toolchain_fingerprint", oldFingerprint)
                .putString("apk_self_builder_toolchain_fingerprint", newFingerprint)
                .putString("apk_self_builder_pending_toolchain_fingerprint", newFingerprint)
                .putLong("apk_self_builder_toolchain_promoted_at", System.currentTimeMillis())
                .apply();
    }

    private static JSONObject finalizeToolchainPreflight(Context context, JSONObject value) throws Exception {
        SharedPreferences prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        String pending = prefs.getString("apk_self_builder_pending_toolchain_fingerprint", "").trim();
        if (pending.isEmpty()) return value;
        File builder = new File(context.getFilesDir(), "apk-self-builder");
        File current = new File(builder, "toolchain");
        File previous = new File(builder, "toolchain-previous");
        if (value.optBoolean("ready", false)) {
            prefs.edit()
                    .putString("apk_self_builder_known_good_toolchain_fingerprint", pending)
                    .remove("apk_self_builder_pending_toolchain_fingerprint")
                    .putString("apk_self_builder_toolchain_update_state", "succeeded")
                    .putString("apk_self_builder_toolchain_update_error", "")
                    .putLong("apk_self_builder_toolchain_verified_at", System.currentTimeMillis())
                    .apply();
            value.put("toolchainUpdateState", "succeeded");
            value.put("toolchainFingerprint", pending);
            return value;
        }
        if (!previous.isDirectory()) {
            prefs.edit().putString("apk_self_builder_toolchain_update_state", "failed").apply();
            value.put("toolchainUpdateState", "failed");
            return value;
        }

        String previousFingerprint = prefs.getString("apk_self_builder_previous_toolchain_fingerprint", "");
        File failed = new File(builder, "toolchain-failed");
        deleteTree(failed);
        if (current.exists() && !current.renameTo(failed)) {
            copyTree(current, failed);
            deleteTree(current);
        }
        if (!previous.renameTo(current)) {
            copyTree(previous, current);
            deleteTree(previous);
        }
        restoreExecutablePaths(current, new File(current, "manifest.json"));
        prefs.edit()
                .putString("apk_self_builder_toolchain_fingerprint", previousFingerprint)
                .remove("apk_self_builder_pending_toolchain_fingerprint")
                .putString("apk_self_builder_toolchain_update_state", "rolled_back")
                .putString("apk_self_builder_toolchain_update_error", compact(value.optString("summary", "smoke do toolchain novo falhou")))
                .putLong("apk_self_builder_toolchain_verified_at", System.currentTimeMillis())
                .apply();
        JSONObject rollback = callPythonPreflight(context, true);
        rollback.put("toolchainUpdateState", "rolled_back");
        rollback.put("rolledBackFrom", pending);
        rollback.put("toolchainFingerprint", previousFingerprint);
        deleteTree(failed);
        return rollback;
    }

    private static String trimTrailingSlash(String value) {
        String out = value == null ? "" : value.trim();
        while (out.endsWith("/")) out = out.substring(0, out.length() - 1);
        return out;
    }

    private static String urlEncode(String value) throws Exception {
        return java.net.URLEncoder.encode(value == null ? "" : value, StandardCharsets.UTF_8.name());
    }

    private static URL sameOriginUrl(String serverUrl, String candidate) throws Exception {
        URL base = new URL(serverUrl);
        URL target = new URL(candidate);
        int basePort = base.getPort() >= 0 ? base.getPort() : base.getDefaultPort();
        int targetPort = target.getPort() >= 0 ? target.getPort() : target.getDefaultPort();
        if (!("http".equalsIgnoreCase(target.getProtocol()) || "https".equalsIgnoreCase(target.getProtocol()))
                || !base.getProtocol().equalsIgnoreCase(target.getProtocol())
                || !base.getHost().equalsIgnoreCase(target.getHost())
                || basePort != targetPort) {
            throw new IllegalStateException("URL do toolchain aponta para origem não autorizada");
        }
        return target;
    }

    private static final class HttpResult {
        final byte[] body;
        final String signature;
        final String timestamp;
        HttpResult(byte[] body, String signature, String timestamp) {
            this.body = body;
            this.signature = signature == null ? "" : signature;
            this.timestamp = timestamp == null ? "" : timestamp;
        }
    }

    private static HttpResult authenticatedGet(URL url, String workerId, String token, long maxBytes) throws Exception {
        HttpURLConnection connection = (HttpURLConnection) url.openConnection();
        connection.setConnectTimeout(15_000);
        connection.setReadTimeout(30_000);
        connection.setInstanceFollowRedirects(false);
        connection.setRequestProperty("Authorization", "Bearer " + token);
        connection.setRequestProperty("X-Core-Worker-Id", workerId);
        connection.setRequestProperty("Accept-Encoding", "identity");
        int status = connection.getResponseCode();
        if (status < 200 || status >= 300) {
            connection.disconnect();
            throw new IllegalStateException("VPS recusou manifesto do toolchain: HTTP " + status);
        }
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        try (InputStream input = new BufferedInputStream(connection.getInputStream())) {
            byte[] buffer = new byte[64 * 1024];
            long total = 0L;
            int read;
            while ((read = input.read(buffer)) >= 0) {
                if (read <= 0) continue;
                total += read;
                if (total > maxBytes) throw new IllegalStateException("resposta do manifesto excede limite");
                output.write(buffer, 0, read);
            }
        }
        HttpResult result = new HttpResult(output.toByteArray(),
                connection.getHeaderField("X-Core-Worker-Signature"),
                connection.getHeaderField("X-Core-Worker-Timestamp"));
        connection.disconnect();
        return result;
    }

    private static Mac newHmac(String token) throws Exception {
        Mac mac = Mac.getInstance("HmacSHA256");
        mac.init(new SecretKeySpec(token.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
        return mac;
    }

    private static void verifySignedResponse(HttpResult result, String token) throws Exception {
        verifyTimestamp(result.timestamp);
        String expected = result.signature.startsWith("sha256=") ? result.signature.substring(7) : "";
        Mac mac = newHmac(token);
        mac.update(result.timestamp.trim().getBytes(StandardCharsets.US_ASCII));
        mac.update((byte) '\n');
        String actual = hex(mac.doFinal(result.body));
        if (!expected.matches("[0-9a-fA-F]{64}") || !MessageDigest.isEqual(
                expected.toLowerCase(Locale.ROOT).getBytes(StandardCharsets.US_ASCII),
                actual.getBytes(StandardCharsets.US_ASCII))) {
            throw new SecurityException("HMAC inválido no manifesto do toolchain");
        }
    }

    private static void verifyTimestamp(String raw) throws Exception {
        long timestamp;
        try { timestamp = Long.parseLong(raw == null ? "" : raw.trim()); }
        catch (NumberFormatException error) { throw new SecurityException("timestamp autenticado ausente"); }
        long now = System.currentTimeMillis() / 1000L;
        if (Math.abs(now - timestamp) > 300L) throw new SecurityException("resposta autenticada expirada");
    }

    private static void downloadAuthenticated(
            URL url, String workerId, String token, File target, long expectedBytes, String expectedSha) throws Exception {
        HttpURLConnection connection = (HttpURLConnection) url.openConnection();
        connection.setConnectTimeout(20_000);
        connection.setReadTimeout(120_000);
        connection.setInstanceFollowRedirects(false);
        connection.setRequestProperty("Authorization", "Bearer " + token);
        connection.setRequestProperty("X-Core-Worker-Id", workerId);
        connection.setRequestProperty("Accept-Encoding", "identity");
        int status = connection.getResponseCode();
        if (status < 200 || status >= 300) {
            connection.disconnect();
            throw new IllegalStateException("download do toolchain falhou: HTTP " + status);
        }
        String signature = connection.getHeaderField("X-Core-Worker-Signature");
        String timestamp = connection.getHeaderField("X-Core-Worker-Timestamp");
        verifyTimestamp(timestamp);
        String expectedHmac = signature != null && signature.startsWith("sha256=") ? signature.substring(7) : "";
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        Mac mac = newHmac(token);
        mac.update(timestamp.trim().getBytes(StandardCharsets.US_ASCII));
        mac.update((byte) '\n');
        long total = 0L;
        try (InputStream input = new BufferedInputStream(connection.getInputStream());
             BufferedOutputStream output = new BufferedOutputStream(new FileOutputStream(target, false))) {
            byte[] buffer = new byte[1024 * 1024];
            int read;
            while ((read = input.read(buffer)) >= 0) {
                if (read <= 0) continue;
                total += read;
                if (total > expectedBytes || total > 1024L * 1024L * 1024L) {
                    throw new IllegalStateException("download do toolchain excedeu tamanho declarado");
                }
                output.write(buffer, 0, read);
                digest.update(buffer, 0, read);
                mac.update(buffer, 0, read);
            }
            output.flush();
        } catch (Throwable error) {
            target.delete();
            connection.disconnect();
            throw error;
        }
        connection.disconnect();
        String actualSha = hex(digest.digest());
        String actualHmac = hex(mac.doFinal());
        if (total != expectedBytes || !actualSha.equals(expectedSha)
                || !expectedHmac.matches("[0-9a-fA-F]{64}")
                || !MessageDigest.isEqual(expectedHmac.toLowerCase(Locale.ROOT).getBytes(StandardCharsets.US_ASCII),
                actualHmac.getBytes(StandardCharsets.US_ASCII))) {
            target.delete();
            throw new SecurityException("tamanho/SHA-256/HMAC divergente no toolchain externo");
        }
    }

    private static void retainAssetIfPresent(Context context, String asset, File target) {
        try {
            if (assetExists(context.getAssets(), asset)
                    && (!target.isFile() || target.length() == 0L)) {
                copyAsset(context.getAssets(), asset, target);
            }
        } catch (Throwable ignored) { }
    }

    private static boolean assetExists(AssetManager assets, String path) {
        try (InputStream ignored = assets.open(path, AssetManager.ACCESS_STREAMING)) {
            return true;
        } catch (Throwable ignored) {
            return false;
        }
    }

    private static void copyAsset(AssetManager assets, String path, File target) throws Exception {
        File parent = target.getParentFile();
        if (parent != null && !parent.exists() && !parent.mkdirs()) {
            throw new IllegalStateException("não consegui criar " + parent);
        }
        File temp = new File(target.getPath() + ".tmp");
        try (InputStream input = new BufferedInputStream(assets.open(path, AssetManager.ACCESS_STREAMING));
             BufferedOutputStream output = new BufferedOutputStream(new FileOutputStream(temp, false))) {
            byte[] buffer = new byte[1024 * 1024];
            int read;
            while ((read = input.read(buffer)) >= 0) {
                if (read > 0) output.write(buffer, 0, read);
            }
            output.flush();
        }
        if (target.exists() && !target.delete()) throw new IllegalStateException("não consegui substituir asset retido");
        if (!temp.renameTo(target)) throw new IllegalStateException("não consegui promover asset retido");
    }

    private static JSONObject readChunkDescriptor(Context context) throws Exception {
        try (InputStream input = new BufferedInputStream(context.getAssets().open(
                TOOLCHAIN_CHUNKS_MANIFEST, AssetManager.ACCESS_STREAMING));
             ByteArrayOutputStream output = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[64 * 1024];
            int read;
            int total = 0;
            while ((read = input.read(buffer)) >= 0) {
                if (read == 0) continue;
                total += read;
                if (total > 1024 * 1024) throw new IllegalStateException("manifesto de partes grande demais");
                output.write(buffer, 0, read);
            }
            JSONObject descriptor = new JSONObject(output.toString(StandardCharsets.UTF_8.name()));
            if (!"core-worker-toolchain-chunks-v1".equals(descriptor.optString("schema", ""))
                    || descriptor.optInt("version", 0) != 1) {
                throw new IllegalStateException("schema/versão inválido no envelope particionado");
            }
            return descriptor;
        }
    }

    private static boolean retainedChunkAssetsPresent(File repro, JSONObject descriptor) {
        try {
            JSONArray parts = descriptor.optJSONArray("parts");
            if (parts == null || parts.length() == 0 || parts.length() > 256) return false;
            if (!new File(repro, TOOLCHAIN_CHUNKS_MANIFEST).isFile()) return false;
            for (int index = 0; index < parts.length(); index++) {
                JSONObject part = parts.optJSONObject(index);
                if (part == null) return false;
                String expected = String.format(Locale.ROOT, "android-builder-toolchain.part-%03d.cwpart", index);
                if (!expected.equals(part.optString("name", ""))) return false;
                File file = new File(repro, TOOLCHAIN_CHUNKS_DIR + "/" + expected);
                if (!file.isFile() || file.length() != part.optLong("bytes", -1L)) return false;
            }
            return true;
        } catch (Throwable ignored) {
            return false;
        }
    }

    private static File materializeChunkedToolchain(
            Context context, File builder, File repro, JSONObject descriptor) throws Exception {
        JSONObject archive = descriptor.optJSONObject("archive");
        JSONArray parts = descriptor.optJSONArray("parts");
        long expectedBytes = archive == null ? 0L : archive.optLong("bytes", 0L);
        String expectedSha = archive == null ? "" : archive.optString("sha256", "").toLowerCase(Locale.ROOT);
        long chunkSize = descriptor.optLong("chunkSize", 0L);
        if (expectedBytes < 1024L * 1024L || expectedBytes > 1024L * 1024L * 1024L
                || !expectedSha.matches("[0-9a-f]{64}") || chunkSize < 4L * 1024L * 1024L
                || chunkSize > 32L * 1024L * 1024L || parts == null
                || parts.length() == 0 || parts.length() > 256) {
            throw new IllegalStateException("metadados inválidos no envelope particionado do toolchain");
        }
        File temp = new File(builder, "toolchain-source.zip.tmp");
        File outputArchive = new File(builder, "toolchain-source.zip");
        temp.delete();
        outputArchive.delete();
        MessageDigest fullDigest = MessageDigest.getInstance("SHA-256");
        long total = 0L;
        Set<String> declared = new HashSet<>();
        try (BufferedOutputStream output = new BufferedOutputStream(new FileOutputStream(temp, false))) {
            byte[] buffer = new byte[1024 * 1024];
            for (int index = 0; index < parts.length(); index++) {
                JSONObject part = parts.optJSONObject(index);
                String name = part == null ? "" : part.optString("name", "");
                String expectedName = String.format(Locale.ROOT, "android-builder-toolchain.part-%03d.cwpart", index);
                long expectedPartBytes = part == null ? 0L : part.optLong("bytes", 0L);
                String expectedPartSha = part == null ? "" : part.optString("sha256", "").toLowerCase(Locale.ROOT);
                if (!expectedName.equals(name) || !declared.add(name) || expectedPartBytes <= 0L
                        || expectedPartBytes > chunkSize || !expectedPartSha.matches("[0-9a-f]{64}")) {
                    throw new IllegalStateException("metadados inválidos da parte " + index);
                }
                if (total + expectedPartBytes > expectedBytes) {
                    throw new IllegalStateException("partes excedem o tamanho total declarado");
                }
                String assetPath = TOOLCHAIN_CHUNKS_DIR + "/" + name;
                File retained = new File(repro, assetPath);
                copyAsset(context.getAssets(), assetPath, retained);
                if (retained.length() != expectedPartBytes) {
                    throw new IllegalStateException("parte ausente/truncada: " + name);
                }
                MessageDigest partDigest = MessageDigest.getInstance("SHA-256");
                try (BufferedInputStream input = new BufferedInputStream(new FileInputStream(retained))) {
                    int read;
                    while ((read = input.read(buffer)) >= 0) {
                        if (read == 0) continue;
                        output.write(buffer, 0, read);
                        partDigest.update(buffer, 0, read);
                        fullDigest.update(buffer, 0, read);
                        total += read;
                        if (total > expectedBytes) {
                            throw new IllegalStateException("partes excedem o tamanho total declarado");
                        }
                    }
                }
                if (!hex(partDigest.digest()).equals(expectedPartSha)) {
                    throw new IllegalStateException("sha256 divergente da parte " + name);
                }
            }
        } catch (Exception error) {
            temp.delete();
            throw error;
        }
        if (total != expectedBytes || !hex(fullDigest.digest()).equals(expectedSha)) {
            temp.delete();
            throw new IllegalStateException("tamanho/sha256 total divergente no envelope particionado");
        }
        copyAsset(context.getAssets(), TOOLCHAIN_CHUNKS_MANIFEST, new File(repro, TOOLCHAIN_CHUNKS_MANIFEST));
        new File(repro, TOOLCHAIN_ASSET).delete();
        if (!temp.renameTo(outputArchive)) {
            temp.delete();
            throw new IllegalStateException("não consegui promover o toolchain reconstituído");
        }
        return outputArchive;
    }

    private static String hex(byte[] raw) {
        StringBuilder out = new StringBuilder(raw.length * 2);
        for (byte value : raw) out.append(String.format(Locale.ROOT, "%02x", value & 0xff));
        return out.toString();
    }

    private static void extractZip(File source, File target) throws Exception {
        File canonicalRoot = target.getCanonicalFile();
        long expanded = 0L;
        int entries = 0;
        try (ZipInputStream zip = new ZipInputStream(new BufferedInputStream(new java.io.FileInputStream(source)))) {
            ZipEntry entry;
            byte[] buffer = new byte[1024 * 1024];
            while ((entry = zip.getNextEntry()) != null) {
                entries++;
                if (entries > MAX_TOOLCHAIN_ENTRIES) throw new IllegalStateException("toolchain contém arquivos demais");
                String name = entry.getName() == null ? "" : entry.getName().replace('\\', '/');
                if (name.isEmpty() || name.startsWith("/") || hasParentTraversal(name)) {
                    throw new IllegalStateException("caminho inseguro no toolchain");
                }
                File destination = new File(canonicalRoot, name).getCanonicalFile();
                if (!destination.getPath().startsWith(canonicalRoot.getPath() + File.separator)
                        && !destination.equals(canonicalRoot)) {
                    throw new IllegalStateException("toolchain tenta sair do staging");
                }
                if (entry.isDirectory()) {
                    if (!destination.exists() && !destination.mkdirs()) throw new IllegalStateException("falha criando pasta do toolchain");
                    continue;
                }
                File parent = destination.getParentFile();
                if (parent != null && !parent.exists() && !parent.mkdirs()) throw new IllegalStateException("falha criando pasta do toolchain");
                try (BufferedOutputStream output = new BufferedOutputStream(new FileOutputStream(destination, false))) {
                    int read;
                    while ((read = zip.read(buffer)) >= 0) {
                        if (read <= 0) continue;
                        expanded += read;
                        if (expanded > MAX_TOOLCHAIN_EXPANDED_BYTES) throw new IllegalStateException("toolchain expandido excede o limite");
                        output.write(buffer, 0, read);
                    }
                    output.flush();
                }
                destination.setReadable(true, true);
                if (looksExecutable(name)) destination.setExecutable(true, true);
            }
        }
    }

    private static boolean hasParentTraversal(String path) {
        String[] parts = path.split("/");
        for (String part : parts) if ("..".equals(part)) return true;
        return false;
    }

    private static boolean looksExecutable(String name) {
        String value = name.toLowerCase(Locale.ROOT);
        return value.endsWith("/java") || value.endsWith("/gradle") || value.endsWith("/aapt2")
                || value.endsWith("/adb") || value.endsWith("/zipalign") || value.endsWith("/apksigner")
                || value.endsWith("/lib/jspawnhelper") || value.endsWith("/lib/jexec")
                || value.contains("/bin/");
    }

    private static void restoreExecutablePaths(File root, File manifestFile) throws Exception {
        File canonicalRoot = root.getCanonicalFile();
        if (!manifestFile.isFile()) throw new IllegalStateException("manifest.json ausente no toolchain extraído");
        byte[] raw = java.nio.file.Files.readAllBytes(manifestFile.toPath());
        JSONObject manifest = new JSONObject(new String(raw, StandardCharsets.UTF_8));
        String schema = manifest.optString("schema", "");
        boolean legacyV1 = "core-worker-android-builder-v1".equals(schema);
        boolean externalV2 = "core-worker-android-builder-v2".equals(schema);
        if (!legacyV1 && !externalV2) {
            throw new IllegalStateException("schema inválido no toolchain extraído");
        }
        if (legacyV1) {
            if (manifest.optInt("version", 0) < 7) {
                throw new IllegalStateException("toolchain legado antigo: validação executável v7 ausente");
            }
            JSONObject gradleLauncher = manifest.optJSONObject("gradleLauncher");
            if (gradleLauncher == null || !"android-sh-resolved-app-home-jvm-opts-v2".equals(
                    gradleLauncher.optString("strategy", ""))) {
                throw new IllegalStateException("launcher Gradle legado incompatível com /system/bin/sh");
            }
        } else {
            if (manifest.optInt("version", 0) < 2) {
                throw new IllegalStateException("toolchain externo v2 antigo");
            }
            JSONObject versions = manifest.optJSONObject("versions");
            if (versions == null || versions.optInt("jdkMajor", 0) != 17
                    || !"8.9".equals(versions.optString("gradle", ""))
                    || !"8.7.3".equals(versions.optString("agp", ""))
                    || versions.optInt("compileSdk", 0) != 34
                    || !"34.0.0".equals(versions.optString("buildTools", ""))
                    || !"17.0.0".equals(versions.optString("chaquopy", ""))) {
                throw new IllegalStateException("matriz de versões do toolchain externo incompatível");
            }
        }
        JSONObject validation = manifest.optJSONObject("validation");
        if (validation == null || !"required-executable-smoke-v2".equals(
                validation.optString("strategy", ""))) {
            throw new IllegalStateException("estratégia de validação executável ausente no toolchain");
        }
        JSONArray requiredSmoke = validation.optJSONArray("requiredSmokeChecks");
        Set<String> requiredNames = new HashSet<>();
        if (requiredSmoke != null) {
            for (int i = 0; i < requiredSmoke.length(); i++) requiredNames.add(requiredSmoke.optString(i, ""));
        }
        if (externalV2 && !(requiredNames.size() == 5 && requiredNames.contains("java")
                && requiredNames.contains("javac") && requiredNames.contains("jar")
                && requiredNames.contains("gradle") && requiredNames.contains("aapt2"))) {
            throw new IllegalStateException("toolchain externo não declara os cinco smokes obrigatórios");
        }
        JSONArray executablePaths = manifest.optJSONArray("executablePaths");
        if (executablePaths == null || executablePaths.length() == 0
                || executablePaths.length() > MAX_TOOLCHAIN_ENTRIES) {
            throw new IllegalStateException("lista executablePaths inválida no toolchain");
        }

        Set<String> declared = new HashSet<>();
        for (int index = 0; index < executablePaths.length(); index++) {
            String name = executablePaths.optString(index, "").replace('\\', '/');
            if (name.isEmpty() || name.startsWith("/") || hasParentTraversal(name) || !declared.add(name)) {
                throw new IllegalStateException("caminho executável inválido no toolchain");
            }
            File executable = new File(canonicalRoot, name).getCanonicalFile();
            if (!executable.getPath().startsWith(canonicalRoot.getPath() + File.separator)
                    || !executable.isFile()) {
                throw new IllegalStateException("executável declarado ausente ou fora do toolchain: " + name);
            }
            executable.setReadable(true, true);
            if (!executable.setExecutable(true, true) && !executable.canExecute()) {
                throw new IllegalStateException("não consegui restaurar permissão executável: " + name);
            }
        }

        JSONObject paths = manifest.optJSONObject("paths");
        String jdkPath = normalizedToolchainPath(paths == null ? null : paths.optString("jdk", "jdk"), "jdk");
        String gradlePath = normalizedToolchainPath(paths == null ? null : paths.optString("gradle", "gradle/bin/gradle"), "gradle/bin/gradle");
        String aapt2Path = normalizedToolchainPath(paths == null ? null : paths.optString("aapt2", "bin/aapt2"), "bin/aapt2");
        String[] mandatory = {
                jdkPath + "/bin/java",
                jdkPath + "/bin/javac",
                jdkPath + "/bin/jar",
                gradlePath,
                aapt2Path
        };
        for (String name : mandatory) {
            if (!declared.contains(name) || !new File(canonicalRoot, name).canExecute()) {
                throw new IllegalStateException("executável obrigatório não restaurado: " + name);
            }
        }
        String spawnHelperPath = jdkPath + "/lib/jspawnhelper";
        File spawnHelper = new File(canonicalRoot, spawnHelperPath);
        if (spawnHelper.isFile()
                && (!declared.contains(spawnHelperPath) || !spawnHelper.canExecute())) {
            throw new IllegalStateException("jdk/lib/jspawnhelper não foi restaurado como executável");
        }
    }

    private static String normalizedToolchainPath(String raw, String fallback) {
        String value = raw == null ? "" : raw.replace('\\', '/').trim();
        while (value.startsWith("/")) value = value.substring(1);
        while (value.endsWith("/")) value = value.substring(0, value.length() - 1);
        if (value.isEmpty()) value = fallback;
        if (value.startsWith("/") || hasParentTraversal(value)) {
            throw new IllegalStateException("caminho inválido no manifest do toolchain");
        }
        return value;
    }

    private static void copyTree(File source, File target) throws Exception {
        if (source.isDirectory()) {
            if (!target.exists() && !target.mkdirs()) throw new IllegalStateException("falha copiando toolchain");
            File[] children = source.listFiles();
            if (children != null) for (File child : children) copyTree(child, new File(target, child.getName()));
            return;
        }
        File parent = target.getParentFile();
        if (parent != null && !parent.exists() && !parent.mkdirs()) throw new IllegalStateException("falha criando destino");
        try (InputStream input = new java.io.FileInputStream(source);
             BufferedOutputStream output = new BufferedOutputStream(new FileOutputStream(target, false))) {
            byte[] buffer = new byte[1024 * 1024];
            int read;
            while ((read = input.read(buffer)) >= 0) if (read > 0) output.write(buffer, 0, read);
        }
        target.setExecutable(source.canExecute(), true);
    }

    private static void deleteTree(File file) {
        if (file == null || !file.exists()) return;
        if (file.isDirectory()) {
            File[] children = file.listFiles();
            if (children != null) for (File child : children) deleteTree(child);
        }
        file.delete();
    }

    private static JSONObject cloneJson(JSONObject value) {
        try { return new JSONObject(value == null ? "{}" : value.toString()); }
        catch (Throwable ignored) { return new JSONObject(); }
    }

    private static String compact(String value) {
        String clean = value == null ? "" : value.replaceAll("\\s+", " ").trim();
        return clean.length() <= 600 ? clean : clean.substring(0, 600);
    }

    private static String shortThrowable(Throwable error) {
        if (error == null) return "erro desconhecido";
        String message = error.getMessage() == null ? "" : error.getMessage().replace('\n', ' ').replace('\r', ' ').trim();
        String value = error.getClass().getSimpleName() + (message.isEmpty() ? "" : ": " + message);
        return value.length() <= 300 ? value : value.substring(0, 300);
    }
}
