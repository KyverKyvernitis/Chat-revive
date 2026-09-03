package dev.core.worker;

import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONObject;

/**
 * Mantém identidades separadas para o celular físico e para o runtime APK.
 *
 * Instalações antigas herdaram do Termux o mesmo worker_id. Durante o bootstrap,
 * isso fazia o heartbeat Android sobrescrever versão/roles/tasks do phone-worker.
 * O runtime APK passa a usar `<worker-id>-apk`, mantendo o token compartilhado
 * apenas para a transição autenticada. Pareamentos criados diretamente pelo APK
 * continuam usando o próprio ID sem sufixo.
 */
final class CoreWorkerRuntimeIdentity {
    private static final String PREFS = "core_worker_private";
    private static final int TERMUX_PORT = 8766;
    private static final int APK_BOOTSTRAP_PORT = 8767;

    private CoreWorkerRuntimeIdentity() { }

    static SharedPreferences prefs(Context context) {
        return context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    static void migrate(Context context) {
        if (context == null) return;
        SharedPreferences prefs = prefs(context);
        String canonical = canonicalWorkerId(prefs);
        if (canonical.isEmpty()) return;
        String runtime = runtimeWorkerId(prefs);
        boolean sharedBootstrap = !runtime.equals(canonical);
        SharedPreferences.Editor editor = prefs.edit()
                .putString("runtime_worker_id", runtime)
                .putString("physical_worker_id", canonical)
                .putString("runtime_kind", "apk")
                .putBoolean("bootstrap_shared_worker_identity", sharedBootstrap);
        if (sharedBootstrap) {
            editor.putString("parent_worker_id", canonical);
            if (!prefs.getBoolean("direct_http_port_migrated_v072", false)
                    || prefs.getInt("direct_http_port", TERMUX_PORT) == TERMUX_PORT) {
                editor.putInt("direct_http_port", APK_BOOTSTRAP_PORT);
            }
        } else {
            editor.remove("parent_worker_id");
            if (!prefs.contains("direct_http_port")) editor.putInt("direct_http_port", TERMUX_PORT);
        }
        editor.putBoolean("direct_http_port_migrated_v072", true).apply();
    }

    static void markDedicatedApkPair(SharedPreferences prefs, String workerId) {
        if (prefs == null) return;
        String safe = safeId(workerId);
        prefs.edit()
                .putString("pairing_owner", "apk")
                .putString("runtime_worker_id", safe)
                .putString("physical_worker_id", safe)
                .remove("parent_worker_id")
                .putBoolean("bootstrap_shared_worker_identity", false)
                .putInt("direct_http_port", TERMUX_PORT)
                .putBoolean("direct_http_port_migrated_v072", true)
                .apply();
    }

    static void clear(SharedPreferences.Editor editor) {
        if (editor == null) return;
        editor.remove("pairing_owner")
                .remove("runtime_worker_id")
                .remove("physical_worker_id")
                .remove("parent_worker_id")
                .remove("runtime_kind")
                .remove("bootstrap_shared_worker_identity")
                .remove("direct_http_port_migrated_v072");
    }

    static String canonicalWorkerId(Context context) {
        return canonicalWorkerId(prefs(context));
    }

    static String canonicalWorkerId(SharedPreferences prefs) {
        if (prefs == null) return "";
        String value = safeId(prefs.getString("worker_id", ""));
        if (!value.isEmpty()) return value;
        return safeId(prefs.getString("native_worker_id", ""));
    }

    static String runtimeWorkerId(Context context) {
        SharedPreferences prefs = prefs(context);
        migrate(context);
        return runtimeWorkerId(prefs);
    }

    static String runtimeWorkerId(SharedPreferences prefs) {
        if (prefs == null) return "";
        String saved = safeId(prefs.getString("runtime_worker_id", ""));
        if (!saved.isEmpty()) return saved;
        String canonical = canonicalWorkerId(prefs);
        if (canonical.isEmpty()) return "";
        if (isDedicatedApkPair(prefs, canonical)) return canonical;
        return apkChildId(canonical);
    }

    static String parentWorkerId(Context context) {
        migrate(context);
        SharedPreferences prefs = prefs(context);
        String value = safeId(prefs.getString("parent_worker_id", ""));
        if (!value.isEmpty()) return value;
        String canonical = canonicalWorkerId(prefs);
        String runtime = runtimeWorkerId(prefs);
        return !canonical.isEmpty() && !canonical.equals(runtime) ? canonical : "";
    }

    static boolean sharedBootstrapIdentity(Context context) {
        migrate(context);
        SharedPreferences prefs = prefs(context);
        String canonical = canonicalWorkerId(prefs);
        String runtime = runtimeWorkerId(prefs);
        return !canonical.isEmpty() && !runtime.isEmpty() && !canonical.equals(runtime);
    }

    static int directHttpPort(Context context) {
        migrate(context);
        SharedPreferences prefs = prefs(context);
        String canonical = canonicalWorkerId(prefs);
        String runtime = runtimeWorkerId(prefs);
        int fallback = !canonical.isEmpty() && !runtime.isEmpty() && !canonical.equals(runtime)
                ? APK_BOOTSTRAP_PORT : TERMUX_PORT;
        int value = prefs.getInt("direct_http_port", fallback);
        return value >= 1024 && value <= 65535 ? value : fallback;
    }

    static int effectiveDirectHttpPort(Context context) {
        SharedPreferences prefs = prefs(context);
        int configured = directHttpPort(context);
        int value = prefs.getInt("direct_http_effective_port", configured);
        return value >= 1024 && value <= 65535 ? value : configured;
    }

    static void putRuntimeFields(Context context, JSONObject payload) throws Exception {
        if (context == null || payload == null) return;
        String runtime = runtimeWorkerId(context);
        String physical = canonicalWorkerId(context);
        String parent = parentWorkerId(context);
        payload.put("worker_id", runtime);
        payload.put("id", runtime);
        payload.put("workerId", runtime);
        payload.put("runtime_kind", "apk");
        payload.put("platform", "android");
        payload.put("physical_worker_id", physical.isEmpty() ? runtime : physical);
        if (!parent.isEmpty()) payload.put("parent_worker_id", parent);
        payload.put("bootstrap_shared_worker_identity", !parent.isEmpty());
    }

    private static boolean isDedicatedApkPair(SharedPreferences prefs, String canonical) {
        String owner = String.valueOf(prefs.getString("pairing_owner", "")).trim().toLowerCase();
        return "apk".equals(owner) || canonical.startsWith("apk-");
    }

    private static String apkChildId(String canonical) {
        String safe = safeId(canonical);
        if (safe.endsWith("-apk")) return safe;
        if (safe.length() > 60) safe = safe.substring(0, 60);
        return safe + "-apk";
    }

    private static String safeId(String value) {
        String clean = value == null ? "" : value.trim().toLowerCase();
        clean = clean.replaceAll("[^a-z0-9_.:-]+", "-").replaceAll("^[-._:]+|[-._:]+$", "");
        return clean.length() <= 64 ? clean : clean.substring(0, 64);
    }
}
