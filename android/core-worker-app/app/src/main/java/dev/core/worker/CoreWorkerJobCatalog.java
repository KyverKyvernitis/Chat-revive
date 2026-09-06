package dev.core.worker;

import android.content.Context;

import org.json.JSONArray;

import java.util.Collections;
import java.util.LinkedHashSet;
import java.util.Set;

/** Catálogo único do worker Android, sem dependência operacional de Termux. */
public final class CoreWorkerJobCatalog {
    private static final String[] APK_JOBS = new String[] {
            "apk_ping", "apk_status_refresh", "apk_upload_app_logs", "apk_diagnostic",
            "apk_check_update", "apk_test_vps_connection", "apk_sync_runtime_state",
            "apk_job_history", "apk_device_diagnostic", "apk_push_diagnostic",
            "apk_update_diagnostic", "apk_runtime_diagnostic", "apk_worker_bridge_status",
            "apk_test_notification", "apk_repair_local_state", "apk_reset_job_history",
            "apk_trim_cache", "apk_update_storage_cleanup", "apk_sync_profile",
            "apk_sync_profile_now", "apk_verify_update_state", "apk_native_worker_status",
            "apk_native_boot_status", "apk_local_shell_probe",
            "apk_core_linux_native_executor_probe", "apk_core_linux_native_executor_test",
            "apk_core_linux_native_runtime_status", "apk_core_linux_rootfs_status",
            "apk_core_linux_rootfs_prepare", "apk_core_linux_rootfs_validate",
            "apk_core_linux_rootfs_preflight", "apk_core_linux_rootfs_clean_staging",
            "apk_core_linux_rootfs_import_status", "apk_core_linux_rootfs_import_validate",
            "apk_core_linux_rootfs_import_abort", "apk_core_linux_rootfs_real_status",
            "apk_core_linux_rootfs_glibc_preflight", "apk_core_linux_runner_status",
            "apk_core_linux_runner_preflight", "apk_core_linux_runner_requirements",
            "apk_core_linux_runtime_smoke_test", "apk_core_linux_rootfs_smoke_test",
            "apk_core_linux_box64_preflight", "apk_core_linux_box64_smoke_test"
    };

    private static final String[] DIRECT_REGISTRY_TASKS = new String[] {
            "ping", "health", "status", "diagnostic_basic", "worker_self_check",
            "network_probe", "endpoint_probe", "tailscale_status", "vps_assist_probe",
            "emoji_recolor", "sha256", "hash_batch", "text_stats", "log_extract", "log_summary",
            "log_digest", "zip", "zip_validate", "zip_audit", "maintenance_plan",
            "ffmpeg_check", "ffprobe_check", "ffmpeg_convert", "ffprobe_media",
            "media_probe", "audio_convert", "tts_agent_status", "tts_agent_synthesize",
            "tts_android_voices", "tts_atts_voices", "android_tts_voices",
            "tts_synthesize_benchmark", "tts_synthesize_piper", "tts_cache_lookup", "tts_cache_store",
            "worker_logs", "boot_status", "service_status"
    };

    private static final String[] CAPABILITIES = new String[] {
            "apk-native", "apk-direct-worker", "android-status", "native-boot",
            "safe-shell-probe", "internal-jobs", "background-job-agent",
            "authenticated-job-registry", "durable-job-results", "direct-http-api",
            "safe-file-transfer", "safe-media-tools", "android-native-tts",
            "core-linux-runtime", "core-linux-rootfs-manager", "core-linux-rootfs-import-v1",
            "core-linux-runner-preflight-v11", "core-linux-base-tools-smoke-v12",
            "core-linux-rootfs-proot-smoke-v13.3", "core-linux-box64-intake-preflight-v14.2.1",
            "core-linux-box64-glibc-preflight-v15.3.1",
            "core-linux-rootfs-glibc-intake-preflight-v17",
            "core-linux-embedded-binaries-intake-v11", "apk-runtime-autonomous",
            "apk-durable-jobs-v1", "apk-job-lease-token-v1", "termux-bootstrap-builder-compatible"
    };

    private static final String[] ROLES = new String[] {
            "apk-worker", "diagnostics", "media", "tts"
    };

    private static final Set<String> APK_JOB_SET;

    static {
        LinkedHashSet<String> values = new LinkedHashSet<>();
        Collections.addAll(values, APK_JOBS);
        APK_JOB_SET = Collections.unmodifiableSet(values);
    }

    private CoreWorkerJobCatalog() { }

    public static JSONArray supportedJobs() {
        return array(APK_JOBS);
    }

    public static JSONArray remoteSupportedTasks() {
        return combine(array(APK_JOBS), array(DIRECT_REGISTRY_TASKS));
    }

    public static JSONArray remoteSupportedTasks(Context context) {
        JSONArray out = remoteSupportedTasks();
        appendUnique(out, CoreWorkerApkBuildManager.availableTasks(context));
        return out;
    }

    public static JSONArray directRegistryTasks() {
        return array(DIRECT_REGISTRY_TASKS);
    }

    public static JSONArray capabilities() {
        return array(CAPABILITIES);
    }

    public static JSONArray capabilities(Context context) {
        JSONArray out = capabilities();
        appendUnique(out, CoreWorkerApkBuildManager.dynamicCapabilities(context));
        JSONArray runtime = new JSONArray();
        runtime.put("direct-http-" + CoreWorkerRuntimeIdentity.directHttpPort(context));
        runtime.put(CoreWorkerRuntimeIdentity.sharedBootstrapIdentity(context)
                ? "termux-bootstrap-active" : "termux-replaced");
        appendUnique(out, runtime);
        return out;
    }

    public static JSONArray roles() {
        return array(ROLES);
    }

    public static JSONArray roles(Context context) {
        JSONArray out = roles();
        appendUnique(out, CoreWorkerApkBuildManager.dynamicRoles(context));
        return out;
    }

    public static boolean supports(String type) {
        return type != null && APK_JOB_SET.contains(type.trim());
    }

    public static int size() {
        return APK_JOBS.length;
    }

    private static JSONArray array(String[] values) {
        JSONArray out = new JSONArray();
        for (String value : values) out.put(value);
        return out;
    }

    private static JSONArray combine(JSONArray first, JSONArray second) {
        JSONArray out = new JSONArray();
        appendUnique(out, first);
        appendUnique(out, second);
        return out;
    }

    private static void appendUnique(JSONArray target, JSONArray source) {
        if (target == null || source == null) return;
        LinkedHashSet<String> values = new LinkedHashSet<>();
        for (int i = 0; i < target.length(); i++) {
            String value = target.optString(i, "").trim();
            if (!value.isEmpty()) values.add(value);
        }
        for (int i = 0; i < source.length(); i++) {
            String value = source.optString(i, "").trim();
            if (!value.isEmpty() && values.add(value)) target.put(value);
        }
    }
}
