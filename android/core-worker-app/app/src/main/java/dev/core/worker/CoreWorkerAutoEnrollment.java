package dev.core.worker;

import android.content.Context;
import android.content.SharedPreferences;
import android.util.Base64;

import org.json.JSONObject;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.UUID;

/**
 * Bootstrap automático do runtime APK usando o Termux já autenticado como raiz
 * de confiança do mesmo aparelho. O endpoint local nunca expõe credenciais; ele
 * publica apenas um nonce efêmero e metadados não secretos do APK.
 */
final class CoreWorkerAutoEnrollment {
    private static final String PREFS = "core_worker_private";
    private static final long CHALLENGE_TTL_MS = 10 * 60 * 1000L;
    private static final SecureRandom RANDOM = new SecureRandom();

    private CoreWorkerAutoEnrollment() { }

    static boolean supported() {
        return !safe(BuildConfig.CORE_WORKER_PARENT_WORKER_ID).isEmpty()
                && !safe(BuildConfig.CORE_WORKER_VPS_URL).isEmpty();
    }

    static JSONObject status(Context context) throws Exception {
        SharedPreferences prefs = prefs(context);
        JSONObject out = new JSONObject();
        out.put("ok", true);
        out.put("protocol", "core-worker-apk-auto-enrollment-v1");
        out.put("versionName", BuildConfig.VERSION_NAME);
        out.put("versionCode", BuildConfig.VERSION_CODE);
        out.put("sourceFingerprint", safe(BuildConfig.CORE_WORKER_SOURCE_FINGERPRINT));
        out.put("parent_worker_id", safe(BuildConfig.CORE_WORKER_PARENT_WORKER_ID));
        out.put("install_id", installId(prefs));

        String token = safe(prefs.getString("worker_token", ""));
        String runtime = CoreWorkerRuntimeIdentity.runtimeWorkerId(context);
        String parent = CoreWorkerRuntimeIdentity.parentWorkerId(context);
        if (!token.isEmpty() && !runtime.isEmpty()) {
            out.put("state", "paired");
            out.put("worker_id", runtime);
            out.put("parent_worker_id", parent);
            out.put("challenge", "");
            return out;
        }
        if (!supported()) {
            out.put("state", "manual_recovery_available");
            out.put("error", "APK sem parent_hint/VPS embutidos");
            return out;
        }
        String challenge = challenge(prefs);
        out.put("state", "waiting_parent");
        out.put("worker_id", childId(BuildConfig.CORE_WORKER_PARENT_WORKER_ID));
        out.put("challenge", challenge);
        return out;
    }

    static JSONObject complete(Context context, JSONObject payload) throws Exception {
        SharedPreferences prefs = prefs(context);
        if (!supported()) throw new IllegalStateException("auto-enrollment não habilitado neste APK");
        String expectedChallenge = challenge(prefs);
        String suppliedChallenge = safe(payload.optString("challenge", ""));
        if (!constantTimeEquals(expectedChallenge, suppliedChallenge)) {
            throw new SecurityException("challenge local inválido");
        }
        long createdAt = prefs.getLong("auto_enrollment_challenge_created_at", 0L);
        if (createdAt <= 0L || System.currentTimeMillis() - createdAt > CHALLENGE_TTL_MS) {
            rotateChallenge(prefs);
            throw new SecurityException("challenge local expirado");
        }

        String parent = safe(payload.optString("parent_worker_id", ""));
        String expectedParent = safe(BuildConfig.CORE_WORKER_PARENT_WORKER_ID);
        if (!expectedParent.equals(parent)) throw new SecurityException("parent_worker_id não corresponde ao APK");
        String workerId = safe(payload.optString("worker_id", ""));
        if (!childId(expectedParent).equals(workerId)) throw new SecurityException("worker_id filho inválido");
        String token = safe(payload.optString("token", ""));
        if (token.length() < 24) throw new SecurityException("credencial filha inválida");
        String directHttpToken = safe(payload.optString("direct_http_token", ""));
        String serverUrl = safe(BuildConfig.CORE_WORKER_VPS_URL);
        if (serverUrl.isEmpty()) throw new IllegalStateException("VPS embutida ausente");

        prefs.edit()
                .putString("server_url", serverUrl)
                .putString("worker_id", expectedParent)
                .putString("native_worker_id", expectedParent)
                .putString("worker_token", token)
                .putString("direct_http_token", directHttpToken.isEmpty() ? token : directHttpToken)
                .putBoolean("paired_via_native_apk", true)
                .putBoolean("auto_enrolled_apk", true)
                .putBoolean("agent_enabled", true)
                .putString("auto_enrollment_state", "paired")
                .putString("auto_enrollment_install_id", installId(prefs))
                .remove("auto_enrollment_challenge")
                .remove("auto_enrollment_challenge_created_at")
                .apply();
        CoreWorkerRuntimeIdentity.markChildApkPair(prefs, expectedParent);
        CoreWorkerApkBuildManager.refreshAsync(context.getApplicationContext());
        CoreWorkerRuntimeService.requestStart(context, "auto_enrollment_success");
        CoreWorkerRuntimeService.requestPoll(context, "auto_enrollment_success");

        return new JSONObject()
                .put("ok", true)
                .put("state", "paired")
                .put("worker_id", workerId)
                .put("parent_worker_id", expectedParent);
    }

    private static SharedPreferences prefs(Context context) {
        return context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    private static String installId(SharedPreferences prefs) {
        String value = safe(prefs.getString("install_id", ""));
        if (!value.isEmpty()) return value;
        value = UUID.randomUUID().toString();
        prefs.edit().putString("install_id", value).apply();
        return value;
    }

    private static String challenge(SharedPreferences prefs) {
        String value = safe(prefs.getString("auto_enrollment_challenge", ""));
        long createdAt = prefs.getLong("auto_enrollment_challenge_created_at", 0L);
        if (!value.isEmpty() && createdAt > 0L && System.currentTimeMillis() - createdAt <= CHALLENGE_TTL_MS) {
            return value;
        }
        return rotateChallenge(prefs);
    }

    private static String rotateChallenge(SharedPreferences prefs) {
        byte[] bytes = new byte[32];
        RANDOM.nextBytes(bytes);
        String value = Base64.encodeToString(bytes, Base64.NO_WRAP | Base64.URL_SAFE).replace("=", "");
        prefs.edit()
                .putString("auto_enrollment_challenge", value)
                .putLong("auto_enrollment_challenge_created_at", System.currentTimeMillis())
                .putString("auto_enrollment_state", "waiting_parent")
                .apply();
        return value;
    }

    private static String childId(String parent) {
        String safe = safe(parent).toLowerCase().replaceAll("[^a-z0-9_.:-]+", "-");
        if (safe.endsWith("-apk")) return safe;
        if (safe.length() > 60) safe = safe.substring(0, 60);
        return safe + "-apk";
    }

    private static boolean constantTimeEquals(String expected, String supplied) {
        return MessageDigest.isEqual(
                safe(expected).getBytes(StandardCharsets.UTF_8),
                safe(supplied).getBytes(StandardCharsets.UTF_8));
    }

    private static String safe(String value) {
        return value == null ? "" : value.trim();
    }
}
