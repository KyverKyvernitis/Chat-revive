package dev.core.worker;

import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Canvas;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.os.BatteryManager;
import android.os.Build;
import android.util.Base64;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.Enumeration;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.TimeUnit;
import java.util.regex.Pattern;
import java.util.zip.ZipEntry;
import java.util.zip.ZipFile;
import java.util.zip.ZipInputStream;
import java.util.zip.ZipOutputStream;

/**
 * Compatibilidade direta do antigo phone-worker, agora executada no APK.
 *
 * Não expõe shell arbitrário. Cada operação possui validação própria e os
 * binários opcionais são procurados apenas em diretórios privados do APK.
 */
final class CoreWorkerDirectTaskExecutor {
    private static final String PREFS = "core_worker_private";
    private static final int MAX_BODY_BYTES = 32 * 1024 * 1024;
    private static final int MAX_OUTPUT_BYTES = 32 * 1024 * 1024;
    private static final int MAX_TEXT_BYTES = 8 * 1024 * 1024;
    private static final int MAX_ZIP_ENTRIES = 2000;
    private static final int MAX_LOG_LINES = 500;

    private final Context context;
    private final SharedPreferences prefs;
    private final NativeTtsManager tts;

    CoreWorkerDirectTaskExecutor(Context context, SharedPreferences prefs, NativeTtsManager tts) {
        this.context = context.getApplicationContext();
        this.prefs = prefs == null ? this.context.getSharedPreferences(PREFS, Context.MODE_PRIVATE) : prefs;
        this.tts = tts;
    }

    static JSONArray directSupportedTasks() {
        JSONArray out = new JSONArray();
        String[] tasks = new String[] {
                "ping", "health", "status", "diagnostic_basic", "worker_self_check",
                "network_probe", "endpoint_probe", "tailscale_status", "vps_assist_probe",
                "emoji_recolor", "sha256", "hash_batch", "text_stats", "log_extract", "log_summary",
                "log_digest", "zip", "zip_validate", "zip_audit", "maintenance_plan", "ffmpeg_check",
                "ffprobe_check", "ffmpeg_convert", "ffprobe_media", "media_probe",
                "audio_convert", "tts_agent_status", "tts_agent_synthesize",
                "tts_android_voices", "tts_atts_voices", "android_tts_voices",
                "tts_synthesize_benchmark", "tts_synthesize_piper", "tts_cache_lookup", "tts_cache_store",
                "worker_logs", "boot_status", "service_status"
        };
        for (String task : tasks) out.put(task);
        return out;
    }

    static boolean supports(String rawTask) {
        String task = normalizeTask(rawTask);
        JSONArray supported = directSupportedTasks();
        for (int i = 0; i < supported.length(); i++) {
            if (task.equals(supported.optString(i, ""))) return true;
        }
        return false;
    }

    JSONObject execute(JSONObject request) throws Exception {
        JSONObject body = request == null ? new JSONObject() : request;
        String task = normalizeTask(body.optString("task", body.optString("type", "")));
        if (!supports(task)) {
            return new JSONObject()
                    .put("ok", false)
                    .put("task", task)
                    .put("error", "task não suportada pelo APK")
                    .put("runtime_mode", "apk-native-direct");
        }

        if ("ping".equals(task) || "health".equals(task) || "status".equals(task)) return health();
        if ("diagnostic_basic".equals(task) || "worker_self_check".equals(task)) return diagnostic();
        if ("network_probe".equals(task) || "tailscale_status".equals(task)) return networkProbe(body);
        if ("endpoint_probe".equals(task)) return endpointProbe(body);
        if ("vps_assist_probe".equals(task)) return vpsAssistProbe(body);
        if ("emoji_recolor".equals(task)) return emojiRecolor(body);
        if ("sha256".equals(task)) return sha256Task(body);
        if ("hash_batch".equals(task)) return hashBatch(body);
        if ("text_stats".equals(task)) return textStats(body);
        if ("log_extract".equals(task)) return logExtract(body);
        if ("log_summary".equals(task) || "log_digest".equals(task)) return logSummary(body);
        if ("zip".equals(task)) return zip(body);
        if ("zip_validate".equals(task) || "zip_audit".equals(task)) return zipValidate(body);
        if ("maintenance_plan".equals(task)) return maintenancePlan(body);
        if ("ffmpeg_check".equals(task)) return binaryCheck("ffmpeg");
        if ("ffprobe_check".equals(task)) return binaryCheck("ffprobe");
        if ("ffmpeg_convert".equals(task) || "audio_convert".equals(task)) return ffmpegConvert(body);
        if ("ffprobe_media".equals(task) || "media_probe".equals(task)) return ffprobeMedia(body);
        if ("tts_agent_status".equals(task)) return ttsStatus();
        if ("tts_agent_synthesize".equals(task) || "tts_synthesize_benchmark".equals(task) || "tts_synthesize_piper".equals(task)) return ttsSynthesize(body);
        if ("tts_android_voices".equals(task) || "tts_atts_voices".equals(task) || "android_tts_voices".equals(task)) return ttsVoices(body);
        if ("tts_cache_lookup".equals(task)) return ttsCacheLookup(body);
        if ("tts_cache_store".equals(task)) return ttsCacheStore(body);
        if ("worker_logs".equals(task)) return workerLogs(body);
        if ("boot_status".equals(task)) return bootStatus();
        if ("service_status".equals(task)) return serviceStatus(body);

        return new JSONObject().put("ok", false).put("task", task).put("error", "task sem handler");
    }

    NativeTtsManager.SynthesisResult synthesizeRaw(JSONObject body) throws Exception {
        if (tts == null) throw new IllegalStateException("Android TTS indisponível");
        return tts.synthesizeRaw(body == null ? new JSONObject() : body);
    }

    JSONObject health() throws Exception {
        JSONObject out = new JSONObject();
        out.put("ok", true);
        out.put("status", "ok");
        out.put("runtime_mode", "apk-native-direct");
        out.put("runtime", "core-worker-apk");
        out.put("source", "core-worker-apk-direct-http");
        out.put("version", BuildConfig.VERSION_NAME);
        out.put("version_code", BuildConfig.VERSION_CODE);
        out.put("worker_id", workerId());
        out.put("runtime_kind", "apk");
        out.put("parent_worker_id", CoreWorkerRuntimeIdentity.parentWorkerId(context));
        out.put("physical_worker_id", CoreWorkerRuntimeIdentity.canonicalWorkerId(context));
        out.put("bootstrap_shared_worker_identity", CoreWorkerRuntimeIdentity.sharedBootstrapIdentity(context));
        out.put("profile", prefs.getString("profile", "midia"));
        out.put("uptime_seconds", Math.max(0L, (System.currentTimeMillis() - prefs.getLong("foreground_runtime_started_at", System.currentTimeMillis())) / 1000L));
        out.put("supported_tasks", directSupportedTasks());
        out.put("capabilities", CoreWorkerJobCatalog.capabilities(context));
        out.put("roles", CoreWorkerJobCatalog.roles(context));
        out.put("tts_agent", ttsStatus());
        out.put("music_agent", new JSONObject()
                .put("ok", false)
                .put("available", false)
                .put("state", "vps-local-runtime")
                .put("reason", "música e voz permanecem no processo principal da VPS"));
        out.put("agent", agentStatus());
        out.put("apk_self_builder", CoreWorkerApkBuildManager.preflight(context, false));
        return out;
    }

    private JSONObject diagnostic() throws Exception {
        JSONObject out = health();
        out.put("device", deviceStatus());
        out.put("network", networkStatus());
        out.put("storage", storageStatus());
        out.put("summary", CoreWorkerRuntimeIdentity.sharedBootstrapIdentity(context)
                ? "diagnóstico coletado pelo runtime APK; Termux reservado ao bootstrap"
                : "diagnóstico coletado diretamente pelo APK");
        return out;
    }

    private JSONObject agentStatus() throws Exception {
        return new JSONObject()
                .put("enabled", prefs.getBoolean("agent_enabled", false))
                .put("executor_ready", prefs.getBoolean("job_executor_ready", false))
                .put("foreground_active", prefs.getBoolean("foreground_runtime_active", false))
                .put("direct_http_active", prefs.getBoolean("direct_http_active", false))
                .put("direct_http_port", CoreWorkerRuntimeIdentity.effectiveDirectHttpPort(context))
                .put("last_job", prefs.getString("internal_light_jobs_last_summary", ""))
                .put("last_error", prefs.getString("agent_last_error", ""));
    }

    private JSONObject deviceStatus() throws Exception {
        JSONObject out = new JSONObject();
        out.put("manufacturer", Build.MANUFACTURER);
        out.put("model", Build.MODEL);
        out.put("device", Build.DEVICE);
        out.put("android", Build.VERSION.RELEASE);
        out.put("sdk", Build.VERSION.SDK_INT);
        JSONArray abis = new JSONArray();
        if (Build.SUPPORTED_ABIS != null) for (String abi : Build.SUPPORTED_ABIS) abis.put(abi);
        out.put("abis", abis);
        try {
            Intent battery = context.registerReceiver(null, new android.content.IntentFilter(Intent.ACTION_BATTERY_CHANGED));
            if (battery != null) {
                int level = battery.getIntExtra(BatteryManager.EXTRA_LEVEL, -1);
                int scale = battery.getIntExtra(BatteryManager.EXTRA_SCALE, 100);
                out.put("battery_percent", level >= 0 ? Math.round(level * 100.0 / Math.max(1, scale)) : -1);
                out.put("charging", battery.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) != 0);
            }
        } catch (Throwable ignored) { }
        return out;
    }

    private JSONObject storageStatus() throws Exception {
        File files = context.getFilesDir();
        File cache = context.getCacheDir();
        return new JSONObject()
                .put("files_free_bytes", files.getUsableSpace())
                .put("files_total_bytes", files.getTotalSpace())
                .put("cache_bytes", directorySize(cache))
                .put("files_path", files.getAbsolutePath());
    }

    private JSONObject networkStatus() throws Exception {
        JSONObject out = new JSONObject();
        try {
            ConnectivityManager manager = (ConnectivityManager) context.getSystemService(Context.CONNECTIVITY_SERVICE);
            Network active = manager == null ? null : manager.getActiveNetwork();
            NetworkCapabilities caps = manager == null || active == null ? null : manager.getNetworkCapabilities(active);
            out.put("online", caps != null && caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET));
            out.put("validated", caps != null && caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED));
            out.put("vpn", caps != null && caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN));
            out.put("wifi", caps != null && caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI));
            out.put("cellular", caps != null && caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR));
        } catch (Throwable exc) {
            out.put("online", false);
            out.put("error", shortThrowable(exc));
        }
        return out;
    }

    private JSONObject networkProbe(JSONObject body) throws Exception {
        JSONObject out = new JSONObject().put("ok", true).put("network", networkStatus());
        String server = normalizedServerUrl();
        if (!server.isEmpty()) out.put("vps", probeUrl(server + "/health", 5000));
        out.put("summary", "rede verificada pelo APK");
        return out;
    }

    private JSONObject vpsAssistProbe(JSONObject body) throws Exception {
        JSONObject out = diagnostic();
        out.put("task", "vps_assist_probe");
        out.put("vps", normalizedServerUrl());
        out.put("endpoint", prefs.getString("direct_worker_endpoint", ""));
        out.put("direct_http_port", CoreWorkerRuntimeIdentity.effectiveDirectHttpPort(context));
        boolean bootstrap = CoreWorkerRuntimeIdentity.sharedBootstrapIdentity(context);
        out.put("termux_required", bootstrap);
        out.put("summary", bootstrap
                ? "APK pronto para assistência; Termux permanece reservado ao primeiro build"
                : "APK pronto para assistência direta da VPS");
        return out;
    }

    private JSONObject endpointProbe(JSONObject body) throws Exception {
        JSONArray targets = body.optJSONArray("targets");
        if (targets == null) targets = body.optJSONArray("urls");
        if (targets == null) {
            targets = new JSONArray();
            String single = body.optString("targets", body.optString("urls", "")).trim();
            if (!single.isEmpty()) targets.put(single);
        }
        if (targets.length() == 0) {
            String server = normalizedServerUrl();
            if (!server.isEmpty()) targets.put(server + "/health");
        }
        int timeoutMs = body.has("timeout_ms")
                ? body.optInt("timeout_ms", 5000)
                : (int) Math.round(body.optDouble("timeout_seconds", 3.0) * 1000.0);
        timeoutMs = Math.max(500, Math.min(8000, timeoutMs));
        int limit = Math.max(1, Math.min(8, body.optInt("max_targets", 4)));
        JSONArray results = new JSONArray();
        for (int i = 0; i < Math.min(limit, targets.length()); i++) {
            String target = targets.optString(i, "").trim();
            if ("auto".equalsIgnoreCase(target)) target = normalizedServerUrl() + "/health";
            JSONObject item;
            if (!target.startsWith("http://") && !target.startsWith("https://")) {
                item = new JSONObject().put("ok", false).put("target", target).put("error", "URL inválida");
            } else {
                item = probeUrl(target, timeoutMs);
            }
            item.put("url", item.optString("target", target));
            if (item.has("elapsed_ms")) item.put("latency_ms", item.optDouble("elapsed_ms", 0.0));
            results.put(item);
        }
        boolean anyOk = false;
        for (int i = 0; i < results.length(); i++) {
            JSONObject item = results.optJSONObject(i);
            anyOk = anyOk || (item != null && item.optBoolean("ok", false));
        }
        return new JSONObject()
                .put("ok", anyOk)
                .put("summary", "endpoints testados pelo APK")
                .put("results", results)
                .put("targets", results)
                .put("count", results.length());
    }

    private JSONObject probeUrl(String target, int timeoutMs) throws Exception {
        long started = System.nanoTime();
        HttpURLConnection connection = null;
        try {
            connection = (HttpURLConnection) new URL(target).openConnection();
            connection.setConnectTimeout(timeoutMs);
            connection.setReadTimeout(timeoutMs);
            connection.setRequestMethod("GET");
            connection.setRequestProperty("Accept", "application/json,*/*;q=0.5");
            int status = connection.getResponseCode();
            return new JSONObject()
                    .put("ok", status >= 200 && status < 400)
                    .put("target", target)
                    .put("status", status)
                    .put("elapsed_ms", elapsedMs(started));
        } catch (Throwable exc) {
            return new JSONObject().put("ok", false).put("target", target).put("error", shortThrowable(exc)).put("elapsed_ms", elapsedMs(started));
        } finally {
            if (connection != null) connection.disconnect();
        }
    }

    private JSONObject emojiRecolor(JSONObject body) throws Exception {
        JSONArray emojis = body.optJSONArray("emojis");
        if (emojis == null) throw new IllegalArgumentException("emojis precisa ser lista");
        int baseColor = parseRgb(body.optString("color", "#5865F2"));
        JSONArray items = new JSONArray();
        JSONArray errors = new JSONArray();
        for (int i = 0; i < Math.min(4, emojis.length()); i++) {
            JSONObject source = emojis.optJSONObject(i);
            if (source == null) continue;
            String id = source.optString("id", "").trim();
            if (!id.matches("\\d{15,25}")) continue;
            boolean animated = source.optBoolean("animated", false);
            try {
                byte[] input = downloadEmoji(id, animated);
                byte[] output = recolorEmojiPng(input, baseColor);
                JSONObject item = new JSONObject();
                item.put("raw", source.optString("raw", ""));
                item.put("raw_variants", source.optJSONArray("raw_variants") == null ? new JSONArray() : source.optJSONArray("raw_variants"));
                item.put("key", source.optString("key", ""));
                item.put("id", id);
                item.put("name", limit(source.optString("name", "emoji"), 32));
                item.put("animated", animated);
                item.put("format", "png");
                item.put("size", output.length);
                item.put("data_b64", Base64.encodeToString(output, Base64.NO_WRAP));
                items.put(item);
            } catch (Throwable error) {
                errors.put(new JSONObject().put("id", id).put("error", limit(shortThrowable(error), 160)));
            }
        }
        return new JSONObject()
                .put("ok", true)
                .put("items", items)
                .put("count", items.length())
                .put("errors", errors)
                .put("monochrome", body.optBoolean("monochrome", false))
                .put("summary", items.length() + " emoji(s) recolorido(s)");
    }

    private byte[] downloadEmoji(String id, boolean animated) throws Exception {
        String extension = animated ? "gif" : "png";
        URL url = new URL("https://cdn.discordapp.com/emojis/" + id + "." + extension + "?size=128&quality=lossless");
        HttpURLConnection connection = (HttpURLConnection) url.openConnection();
        connection.setConnectTimeout(5000);
        connection.setReadTimeout(7000);
        connection.setRequestProperty("User-Agent", "CoreWorkerAPK/" + BuildConfig.VERSION_NAME);
        try {
            int status = connection.getResponseCode();
            if (status < 200 || status >= 300) throw new IllegalStateException("CDN respondeu HTTP " + status);
            return readLimited(connection.getInputStream(), 900 * 1024);
        } finally {
            connection.disconnect();
        }
    }

    private byte[] recolorEmojiPng(byte[] input, int baseColor) throws Exception {
        Bitmap decoded = BitmapFactory.decodeByteArray(input, 0, input.length);
        if (decoded == null) throw new IllegalArgumentException("imagem do emoji inválida");
        Bitmap canvasBitmap = null;
        Bitmap output = null;
        try {
            int width = Math.max(1, decoded.getWidth());
            int height = Math.max(1, decoded.getHeight());
            float scale = Math.min(128f / width, 128f / height);
            int scaledWidth = Math.max(1, Math.round(width * scale));
            int scaledHeight = Math.max(1, Math.round(height * scale));
            Bitmap scaled = Bitmap.createScaledBitmap(decoded, scaledWidth, scaledHeight, true);
            canvasBitmap = Bitmap.createBitmap(128, 128, Bitmap.Config.ARGB_8888);
            Canvas canvas = new Canvas(canvasBitmap);
            canvas.drawBitmap(scaled, (128 - scaledWidth) / 2f, (128 - scaledHeight) / 2f, null);
            if (scaled != decoded) scaled.recycle();

            int[] pixels = new int[128 * 128];
            canvasBitmap.getPixels(pixels, 0, 128, 0, 0, 128, 128);
            int baseR = (baseColor >> 16) & 0xff;
            int baseG = (baseColor >> 8) & 0xff;
            int baseB = baseColor & 0xff;
            for (int i = 0; i < pixels.length; i++) {
                int color = pixels[i];
                int alpha = (color >>> 24) & 0xff;
                if (alpha < 8) {
                    pixels[i] = 0;
                    continue;
                }
                int r = (color >> 16) & 0xff;
                int g = (color >> 8) & 0xff;
                int b = color & 0xff;
                double luminance = Math.max(0.0, Math.min(1.0, (r * 0.299 + g * 0.587 + b * 0.114) / 255.0));
                double shade = 0.42 + (luminance * 0.78);
                int outR = clamp((int) Math.round(baseR * shade), 0, 255);
                int outG = clamp((int) Math.round(baseG * shade), 0, 255);
                int outB = clamp((int) Math.round(baseB * shade), 0, 255);
                pixels[i] = (alpha << 24) | (outR << 16) | (outG << 8) | outB;
            }
            output = Bitmap.createBitmap(pixels, 128, 128, Bitmap.Config.ARGB_8888);
            ByteArrayOutputStream bytes = new ByteArrayOutputStream();
            if (!output.compress(Bitmap.CompressFormat.PNG, 100, bytes)) throw new IllegalStateException("falha ao gerar PNG");
            byte[] result = bytes.toByteArray();
            if (result.length <= 0 || result.length > 512 * 1024) throw new IllegalStateException("emoji gerado fora do limite");
            return result;
        } finally {
            if (output != null && !output.isRecycled()) output.recycle();
            if (canvasBitmap != null && !canvasBitmap.isRecycled()) canvasBitmap.recycle();
            if (!decoded.isRecycled()) decoded.recycle();
        }
    }

    private int parseRgb(String raw) {
        String value = raw == null ? "" : raw.trim();
        if (value.startsWith("#")) value = value.substring(1);
        if (!value.matches("[0-9a-fA-F]{6}")) value = "5865F2";
        return Integer.parseInt(value, 16);
    }

    private byte[] readLimited(InputStream input, int limitBytes) throws Exception {
        if (input == null) return new byte[0];
        try (InputStream source = input; ByteArrayOutputStream output = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[16 * 1024];
            int total = 0;
            int read;
            while ((read = source.read(buffer)) >= 0) {
                if (read == 0) continue;
                total += read;
                if (total > limitBytes) throw new IllegalArgumentException("emoji grande demais");
                output.write(buffer, 0, read);
            }
            return output.toByteArray();
        }
    }

    private JSONObject sha256Task(JSONObject body) throws Exception {
        byte[] data = decodeBodyData(body);
        return new JSONObject().put("ok", true).put("sha256", sha256(data)).put("size", data.length);
    }

    private JSONObject hashBatch(JSONObject body) throws Exception {
        JSONArray items = body.optJSONArray("items");
        if (items == null) items = body.optJSONArray("files");
        if (items == null) throw new IllegalArgumentException("items vazio");
        JSONArray results = new JSONArray();
        long total = 0L;
        for (int i = 0; i < Math.min(80, items.length()); i++) {
            JSONObject item = items.optJSONObject(i);
            if (item == null) continue;
            byte[] data = decodeBodyData(item);
            total += data.length;
            if (total > MAX_BODY_BYTES) throw new IllegalArgumentException("entrada total grande demais");
            results.put(new JSONObject()
                    .put("name", safeName(item.optString("name", "item-" + (i + 1))))
                    .put("size", data.length)
                    .put("sha256", sha256(data)));
        }
        return new JSONObject()
                .put("ok", true)
                .put("summary", results.length() + " hash(es) calculados")
                .put("files", results)
                .put("items", results)
                .put("count", results.length())
                .put("total_bytes", total)
                .put("total_size", total);
    }

    private JSONObject textStats(JSONObject body) throws Exception {
        String text = limitedText(body.optString("text", ""));
        int lines = text.isEmpty() ? 0 : text.split("\\R", -1).length;
        String trimmed = text.trim();
        int words = trimmed.isEmpty() ? 0 : trimmed.split("\\s+").length;
        byte[] bytes = text.getBytes(StandardCharsets.UTF_8);
        return new JSONObject()
                .put("ok", true)
                .put("bytes", bytes.length)
                .put("chars", text.length())
                .put("lines", lines)
                .put("words", words)
                .put("sha256", sha256(bytes));
    }

    private JSONObject logExtract(JSONObject body) throws Exception {
        String text = limitedText(body.optString("text", ""));
        String regex = body.optString("pattern", "error|exception|traceback|falhou|failed|fatal|timeout");
        Pattern pattern;
        try { pattern = Pattern.compile(regex, Pattern.CASE_INSENSITIVE); }
        catch (Throwable exc) { throw new IllegalArgumentException("regex inválida"); }
        int maxLines = Math.max(1, Math.min(MAX_LOG_LINES, body.optInt("max_lines", 120)));
        List<String> matches = new ArrayList<>();
        for (String line : text.split("\\R")) if (pattern.matcher(line).find()) matches.add(line);
        int start = Math.max(0, matches.size() - maxLines);
        JSONArray returned = new JSONArray();
        for (int i = start; i < matches.size(); i++) returned.put(matches.get(i));
        return new JSONObject().put("ok", true).put("matches", returned).put("count", matches.size()).put("returned", returned.length());
    }

    private JSONObject logSummary(JSONObject body) throws Exception {
        String text = limitedText(body.optString("text", ""));
        Map<String, Pattern> patterns = new HashMap<>();
        patterns.put("critical", Pattern.compile("critical|crítico|fatal", Pattern.CASE_INSENSITIVE));
        patterns.put("error", Pattern.compile("error|erro", Pattern.CASE_INSENSITIVE));
        patterns.put("warning", Pattern.compile("warning|warn|aviso", Pattern.CASE_INSENSITIVE));
        patterns.put("timeout", Pattern.compile("timeout|timed out|tempo esgotado", Pattern.CASE_INSENSITIVE));
        patterns.put("traceback", Pattern.compile("traceback", Pattern.CASE_INSENSITIVE));
        patterns.put("exception", Pattern.compile("exception|exceção", Pattern.CASE_INSENSITIVE));
        patterns.put("failed", Pattern.compile("failed|falhou|failure|falha", Pattern.CASE_INSENSITIVE));
        JSONObject counts = new JSONObject();
        JSONArray recent = new JSONArray();
        List<String> important = new ArrayList<>();
        for (String line : text.split("\\R")) {
            boolean hit = false;
            for (Map.Entry<String, Pattern> entry : patterns.entrySet()) {
                if (entry.getValue().matcher(line).find()) {
                    counts.put(entry.getKey(), counts.optInt(entry.getKey(), 0) + 1);
                    hit = true;
                }
            }
            if (hit) important.add(line.trim());
        }
        int maxRecent = Math.max(1, Math.min(80, body.optInt("max_recent", 12)));
        for (int i = Math.max(0, important.size() - maxRecent); i < important.size(); i++) recent.put(important.get(i));
        return new JSONObject()
                .put("ok", true)
                .put("bytes", text.getBytes(StandardCharsets.UTF_8).length)
                .put("lines", text.isEmpty() ? 0 : text.split("\\R", -1).length)
                .put("counts", counts)
                .put("important_count", important.size())
                .put("recent", recent)
                .put("summary", "logs resumidos pelo APK");
    }

    private JSONObject zip(JSONObject body) throws Exception {
        JSONArray files = body.optJSONArray("files");
        if (files == null || files.length() == 0) throw new IllegalArgumentException("files vazio");
        if (files.length() > 80) throw new IllegalArgumentException("arquivos demais");
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        long total = 0L;
        try (ZipOutputStream output = new ZipOutputStream(bytes)) {
            for (int i = 0; i < files.length(); i++) {
                JSONObject item = files.optJSONObject(i);
                if (item == null) throw new IllegalArgumentException("files[" + i + "] inválido");
                byte[] data = decodeBodyData(item);
                total += data.length;
                if (total > MAX_BODY_BYTES) throw new IllegalArgumentException("entrada total grande demais");
                String name = safeZipPath(item.optString("name", "file-" + (i + 1) + ".bin"));
                output.putNextEntry(new ZipEntry(name));
                output.write(data);
                output.closeEntry();
            }
        }
        byte[] result = bytes.toByteArray();
        if (result.length > MAX_OUTPUT_BYTES) throw new IllegalArgumentException("ZIP resultante grande demais");
        return new JSONObject()
                .put("ok", true)
                .put("filename", safeName(body.optString("filename", "core-worker.zip")))
                .put("input_size", total)
                .put("size", result.length)
                .put("sha256", sha256(result))
                .put("data_b64", Base64.encodeToString(result, Base64.NO_WRAP));
    }

    private JSONObject zipValidate(JSONObject body) throws Exception {
        byte[] data = decodeBodyData(body);
        if (data.length == 0) throw new IllegalArgumentException("ZIP vazio");
        JSONArray preview = new JSONArray();
        JSONArray warnings = new JSONArray();
        JSONArray errors = new JSONArray();
        int fileCount = 0;
        int dirCount = 0;
        long uncompressed = 0L;
        try (ZipInputStream input = new ZipInputStream(new ByteArrayInputStream(data))) {
            ZipEntry entry;
            int entries = 0;
            while ((entry = input.getNextEntry()) != null) {
                entries++;
                if (entries > Math.min(MAX_ZIP_ENTRIES, Math.max(1, body.optInt("max_entries", 600)))) {
                    errors.put("arquivos demais no ZIP");
                    break;
                }
                String name = entry.getName() == null ? "" : entry.getName();
                if (isSuspiciousZipPath(name)) errors.put("caminho suspeito: " + name);
                if (preview.length() < Math.max(1, Math.min(80, body.optInt("max_preview", 30)))) preview.put(name);
                if (entry.isDirectory()) dirCount++;
                else fileCount++;
                long size = Math.max(0L, entry.getSize());
                uncompressed += size;
                if (size > 64L * 1024L * 1024L) warnings.put("arquivo grande: " + name);
            }
        } catch (Throwable exc) {
            errors.put("ZIP inválido: " + shortThrowable(exc));
        }
        return new JSONObject()
                .put("ok", errors.length() == 0)
                .put("filename", safeName(body.optString("filename", "update.zip")))
                .put("size", data.length)
                .put("sha256", sha256(data))
                .put("file_count", fileCount)
                .put("dir_count", dirCount)
                .put("total_uncompressed", uncompressed)
                .put("preview", preview)
                .put("warnings", warnings)
                .put("errors", errors);
    }

    private JSONObject maintenancePlan(JSONObject body) throws Exception {
        JSONArray entries = body.optJSONArray("entries");
        if (entries != null) {
            int maxEntries = Math.max(1, Math.min(5000, body.optInt("max_entries", 1000)));
            double now = body.optDouble("now", System.currentTimeMillis() / 1000.0);
            int scanned = 0;
            long totalSize = 0L;
            long reclaimableTemp = 0L;
            long reclaimableLogs = 0L;
            JSONObject byKind = new JSONObject();
            List<JSONObject> largest = new ArrayList<>();
            List<JSONObject> oldTemp = new ArrayList<>();
            List<JSONObject> oldLogs = new ArrayList<>();
            for (int i = 0; i < Math.min(maxEntries, entries.length()); i++) {
                JSONObject item = entries.optJSONObject(i);
                if (item == null) continue;
                String path = limit(item.optString("path", ""), 260);
                String kind = limit(item.optString("kind", "other"), 40);
                long size = Math.max(0L, item.optLong("size", 0L));
                double mtime = item.has("mtime") ? item.optDouble("mtime", now) : now;
                long ageSeconds = Math.max(0L, (long) (now - mtime));
                JSONObject bucket = byKind.optJSONObject(kind);
                if (bucket == null) bucket = new JSONObject().put("count", 0).put("size", 0L);
                bucket.put("count", bucket.optInt("count", 0) + 1);
                bucket.put("size", bucket.optLong("size", 0L) + size);
                byKind.put(kind, bucket);
                JSONObject record = new JSONObject()
                        .put("path", path)
                        .put("size", size)
                        .put("age_seconds", ageSeconds)
                        .put("kind", kind);
                largest.add(record);
                String pathLc = path.toLowerCase(Locale.ROOT);
                boolean temp = "tmp_audio".equals(kind) || "cache".equals(kind) || "temp".equals(kind)
                        || pathLc.contains("tmp_audio") || pathLc.contains("/cache/");
                boolean log = "log".equals(kind) || pathLc.endsWith(".log") || pathLc.endsWith(".txt");
                if (temp && ageSeconds >= 3600L) {
                    oldTemp.add(record);
                    reclaimableTemp += size;
                }
                if (log && ageSeconds >= 7L * 86400L) {
                    oldLogs.add(record);
                    reclaimableLogs += size;
                }
                scanned++;
                totalSize += size;
            }
            Comparator<JSONObject> bySizeDesc = (a, b) -> Long.compare(b.optLong("size", 0L), a.optLong("size", 0L));
            Comparator<JSONObject> byAgeSizeDesc = (a, b) -> {
                int age = Long.compare(b.optLong("age_seconds", 0L), a.optLong("age_seconds", 0L));
                return age != 0 ? age : bySizeDesc.compare(a, b);
            };
            largest.sort(bySizeDesc);
            oldTemp.sort(byAgeSizeDesc);
            oldLogs.sort(byAgeSizeDesc);
            long reclaimable = reclaimableTemp + reclaimableLogs;
            JSONArray recommendations = new JSONArray();
            if (!oldTemp.isEmpty()) recommendations.put("limpar " + oldTemp.size() + " cache(s)/temporário(s) antigos");
            if (!oldLogs.isEmpty()) recommendations.put("arquivar ou remover " + oldLogs.size() + " log(s) antigos");
            if (recommendations.length() == 0) recommendations.put("nenhuma limpeza automática necessária agora");
            return new JSONObject()
                    .put("ok", true)
                    .put("safe", true)
                    .put("dry_run", true)
                    .put("note", "Plano apenas sugere limpeza; o APK não remove arquivos automaticamente.")
                    .put("scanned", scanned)
                    .put("total_size", totalSize)
                    .put("by_kind", byKind)
                    .put("largest", jsonArray(largest, 30))
                    .put("old_temp_candidates", jsonArray(oldTemp, 80))
                    .put("old_log_candidates", jsonArray(oldLogs, 80))
                    .put("estimated_reclaimable", reclaimable)
                    .put("estimated_reclaimable_temp", reclaimableTemp)
                    .put("estimated_reclaimable_logs", reclaimableLogs)
                    .put("recommendations", recommendations)
                    .put("termux_required", false)
                    .put("summary", scanned + " arquivo(s) analisados; nada foi apagado");
        }

        JSONObject storage = storageStatus();
        long cacheBytes = directorySize(context.getCacheDir(), 4096);
        long filesFree = storage.optLong("files_free_bytes", 0L);
        JSONArray actions = new JSONArray();
        if (cacheBytes > 64L * 1024L * 1024L) actions.put("limpar cache do APK pelo job apk_trim_cache");
        if (filesFree < 512L * 1024L * 1024L) actions.put("liberar pelo menos 512 MiB no armazenamento do aplicativo");
        if (actions.length() == 0) actions.put("nenhuma manutenção obrigatória");
        return new JSONObject()
                .put("ok", true)
                .put("safe", true)
                .put("dry_run", true)
                .put("cache_bytes", cacheBytes)
                .put("storage", storage)
                .put("actions", actions)
                .put("recommendations", actions)
                .put("termux_required", false)
                .put("summary", "plano de manutenção calculado sem alterar arquivos");
    }

    private JSONArray jsonArray(List<JSONObject> items, int limit) {
        JSONArray out = new JSONArray();
        for (int i = 0; i < Math.min(Math.max(0, limit), items.size()); i++) out.put(items.get(i));
        return out;
    }

    private long directorySize(File root, int maxEntries) {
        if (root == null || !root.exists()) return 0L;
        long total = 0L;
        int visited = 0;
        List<File> pending = new ArrayList<>();
        pending.add(root);
        while (!pending.isEmpty() && visited < maxEntries) {
            File current = pending.remove(pending.size() - 1);
            visited++;
            if (current.isFile()) {
                total += Math.max(0L, current.length());
                continue;
            }
            File[] children = current.listFiles();
            if (children != null) Collections.addAll(pending, children);
        }
        return total;
    }

    private JSONObject binaryCheck(String name) throws Exception {
        File binary = findBinary(name);
        JSONObject out = new JSONObject()
                .put("ok", binary != null)
                .put("available", binary != null)
                .put("command", name)
                .put("runtime", "apk-private");
        if (binary == null) return out.put("error", name + " não encontrado nos binários privados do APK");
        ProcessResult result = runProcess(new String[] {binary.getAbsolutePath(), "-version"}, null, 5000, 256 * 1024);
        String line = firstLine(result.stdout.isEmpty() ? result.stderr : result.stdout);
        return out.put("ok", result.exitCode == 0).put("path", binary.getAbsolutePath()).put("returncode", result.exitCode).put("version_line", line);
    }

    private JSONObject ffprobeMedia(JSONObject body) throws Exception {
        File ffprobe = requireBinary("ffprobe");
        byte[] data = decodeBodyData(body);
        String ext = safeExtension(body.optString("input_ext", "bin"), "bin");
        File dir = tempDir("ffprobe");
        File src = new File(dir, "input." + ext);
        writeBytes(src, data);
        try {
            ProcessResult result = runProcess(new String[] {
                    ffprobe.getAbsolutePath(), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", src.getAbsolutePath()
            }, dir, clamp(body.optInt("timeout_seconds", 20), 3, 120) * 1000L, 2 * 1024 * 1024);
            if (result.exitCode != 0) throw new IllegalStateException("ffprobe falhou: " + limit(result.stderr, 800));
            JSONObject parsed = new JSONObject(result.stdout.isEmpty() ? "{}" : result.stdout);
            parsed.put("ok", true);
            parsed.put("input_size", data.length);
            return parsed;
        } finally {
            deleteTree(dir);
        }
    }

    private JSONObject ffmpegConvert(JSONObject body) throws Exception {
        File ffmpeg = requireBinary("ffmpeg");
        byte[] data = decodeBodyData(body);
        String inputExt = safeExtension(body.optString("input_ext", "bin"), "bin");
        String outputExt = safeExtension(body.optString("output_ext", "ogg"), "ogg");
        JSONArray requested = body.optJSONArray("ffmpeg_args");
        List<String> args = new ArrayList<>();
        if (requested == null || requested.length() == 0) {
            if ("ogg".equals(outputExt) || "opus".equals(outputExt)) {
                Collections.addAll(args, "-vn", "-c:a", "libopus", "-b:a", "48k", "-ar", "48000", "-ac", "1");
                outputExt = "ogg";
            } else if ("mp3".equals(outputExt)) {
                Collections.addAll(args, "-vn", "-c:a", "libmp3lame", "-b:a", "96k");
            } else {
                args.add("-vn");
            }
        } else {
            if (requested.length() > 40) throw new IllegalArgumentException("ffmpeg_args grande demais");
            for (int i = 0; i < requested.length(); i++) {
                String arg = requested.optString(i, "");
                if (!isSafeFfmpegArg(arg)) throw new IllegalArgumentException("argumento ffmpeg bloqueado: " + arg);
                args.add(arg);
            }
        }
        File dir = tempDir("ffmpeg");
        File src = new File(dir, "input." + inputExt);
        File dst = new File(dir, "output." + outputExt);
        writeBytes(src, data);
        try {
            List<String> command = new ArrayList<>();
            Collections.addAll(command, ffmpeg.getAbsolutePath(), "-hide_banner", "-loglevel", "error", "-y", "-i", src.getAbsolutePath());
            command.addAll(args);
            command.add(dst.getAbsolutePath());
            ProcessResult result = runProcess(command.toArray(new String[0]), dir, clamp(body.optInt("timeout_seconds", 45), 3, 180) * 1000L, 1024 * 1024);
            if (result.exitCode != 0 || !dst.isFile()) throw new IllegalStateException("ffmpeg falhou: " + limit(result.stderr, 800));
            byte[] output = readBytes(dst, MAX_OUTPUT_BYTES);
            return new JSONObject()
                    .put("ok", true)
                    .put("output_ext", outputExt)
                    .put("input_size", data.length)
                    .put("size", output.length)
                    .put("sha256", sha256(output))
                    .put("data_b64", Base64.encodeToString(output, Base64.NO_WRAP));
        } finally {
            deleteTree(dir);
        }
    }

    private JSONObject ttsStatus() throws Exception {
        JSONObject status = tts == null ? new JSONObject().put("ok", false).put("available", false).put("error", "Android TTS indisponível") : tts.statusJson();
        status.put("worker_profile", prefs.getString("profile", "midia"));
        status.put("worker_version", BuildConfig.VERSION_NAME);
        status.put("worker_id", workerId());
        status.put("available_engines", new JSONArray().put("android_native"));
        status.put("route", "apk");
        status.put("runtime_mode", "apk-native-direct");
        status.put("synth_ready", status.optBoolean("ready", status.optBoolean("available", false)));
        status.put("preferred_engine", "android_native");
        status.put("active", 0);
        status.put("concurrency_limit", 1);
        return status;
    }

    private JSONObject ttsVoices(JSONObject body) throws Exception {
        if (tts == null) throw new IllegalStateException("Android TTS indisponível");
        JSONObject out = tts.voicesJson(body);
        out.put("worker_id", workerId());
        out.put("worker_version", BuildConfig.VERSION_NAME);
        return out;
    }

    private JSONObject ttsSynthesize(JSONObject body) throws Exception {
        if (tts == null) throw new IllegalStateException("Android TTS indisponível");
        long started = System.nanoTime();
        JSONObject out = tts.synthesize(body);
        out.put("worker_id", workerId());
        out.put("worker_version", BuildConfig.VERSION_NAME);
        out.put("worker_profile", prefs.getString("profile", "midia"));
        out.put("available_engines", new JSONArray().put("android_native"));
        out.put("total_ms", elapsedMs(started));
        out.put("worker_total_ms", elapsedMs(started));
        return out;
    }

    private JSONObject ttsCacheLookup(JSONObject body) throws Exception {
        String key = cacheKey(body);
        File file = new File(ttsCacheDir(), key + ".bin");
        File meta = new File(ttsCacheDir(), key + ".json");
        if (!file.isFile() || !meta.isFile()) return new JSONObject().put("ok", true).put("hit", false).put("cache_hit", false).put("key", key);
        byte[] data = readBytes(file, MAX_OUTPUT_BYTES);
        JSONObject metadata = readJson(meta);
        return new JSONObject()
                .put("ok", true).put("hit", true).put("cache_hit", true).put("key", key)
                .put("audio_format", metadata.optString("audio_format", "wav"))
                .put("size", data.length).put("sha256", sha256(data))
                .put("data_b64", Base64.encodeToString(data, Base64.NO_WRAP));
    }

    private JSONObject ttsCacheStore(JSONObject body) throws Exception {
        String key = cacheKey(body);
        byte[] data = decodeBodyData(body);
        if (data.length == 0) throw new IllegalArgumentException("áudio vazio");
        File file = new File(ttsCacheDir(), key + ".bin");
        File meta = new File(ttsCacheDir(), key + ".json");
        writeBytes(file, data);
        writeText(meta, new JSONObject()
                .put("key", key)
                .put("audio_format", safeExtension(body.optString("audio_format", "wav"), "wav"))
                .put("size", data.length)
                .put("sha256", sha256(data))
                .put("updated_at", System.currentTimeMillis()).toString());
        trimCache(ttsCacheDir(), 96, 96L * 1024L * 1024L);
        return new JSONObject().put("ok", true).put("stored", true).put("key", key).put("size", data.length).put("sha256", sha256(data));
    }

    private JSONObject workerLogs(JSONObject body) throws Exception {
        int lines = Math.max(10, Math.min(500, body.optInt("lines", 140)));
        JSONArray history;
        try { history = new JSONArray(prefs.getString("internal_job_history", "[]")); }
        catch (Throwable ignored) { history = new JSONArray(); }
        JSONArray limited = new JSONArray();
        for (int i = 0; i < Math.min(lines, history.length()); i++) limited.put(history.opt(i));
        return new JSONObject()
                .put("ok", true)
                .put("runtime", "apk-native")
                .put("history", limited)
                .put("last_error", prefs.getString("agent_last_error", ""))
                .put("last_summary", prefs.getString("internal_light_jobs_last_summary", ""));
    }

    private JSONObject bootStatus() throws Exception {
        return new JSONObject()
                .put("ok", true)
                .put("runtime", "android-boot-receiver")
                .put("agent_enabled", prefs.getBoolean("agent_enabled", false))
                .put("last_boot_event_at", prefs.getLong("native_boot_last_event_at", 0L))
                .put("termux_required", false)
                .put("summary", "boot gerenciado pelo APK");
    }

    private JSONObject serviceStatus(JSONObject body) throws Exception {
        String service = normalizeTask(body.optString("service", "core-worker"));
        boolean active;
        if (service.contains("bedrock")) active = prefs.getBoolean("bedrock_runtime_service_active", false);
        else active = prefs.getBoolean("foreground_runtime_active", false);
        return new JSONObject()
                .put("ok", true)
                .put("service", service)
                .put("active", active)
                .put("state", active ? "running" : "stopped")
                .put("runtime", "android-service");
    }

    private File findBinary(String name) {
        List<File> candidates = new ArrayList<>();
        String nativeDir = context.getApplicationInfo().nativeLibraryDir;
        if (nativeDir != null) {
            candidates.add(new File(nativeDir, "libcoreworker_" + name + ".so"));
            candidates.add(new File(nativeDir, "lib" + name + ".so"));
        }
        candidates.add(new File(context.getFilesDir(), "core-linux/bin/" + name));
        candidates.add(new File(context.getFilesDir(), "core-linux/rootfs/usr/bin/" + name));
        candidates.add(new File(context.getFilesDir(), "bin/" + name));
        for (File file : candidates) {
            if (file.isFile() && file.canRead()) {
                if (!file.canExecute()) file.setExecutable(true, true);
                if (file.canExecute()) return file;
            }
        }
        return null;
    }

    private File requireBinary(String name) {
        File binary = findBinary(name);
        if (binary == null) throw new IllegalStateException(name + " não encontrado nos binários privados do APK");
        return binary;
    }

    private ProcessResult runProcess(String[] command, File directory, long timeoutMs, int maxOutput) throws Exception {
        ProcessBuilder builder = new ProcessBuilder(command);
        if (directory != null) builder.directory(directory);
        Map<String, String> env = builder.environment();
        String nativeDir = context.getApplicationInfo().nativeLibraryDir;
        if (nativeDir != null && !nativeDir.isEmpty()) env.put("LD_LIBRARY_PATH", nativeDir);
        Process process = builder.start();
        StreamCollector stdout = new StreamCollector(process.getInputStream(), maxOutput);
        StreamCollector stderr = new StreamCollector(process.getErrorStream(), maxOutput);
        Thread outThread = new Thread(stdout, "core-worker-process-out");
        Thread errThread = new Thread(stderr, "core-worker-process-err");
        outThread.start();
        errThread.start();
        boolean finished = process.waitFor(timeoutMs, TimeUnit.MILLISECONDS);
        if (!finished) {
            process.destroy();
            if (!process.waitFor(1200, TimeUnit.MILLISECONDS)) process.destroyForcibly();
        }
        outThread.join(1500);
        errThread.join(1500);
        return new ProcessResult(finished ? process.exitValue() : 124, stdout.text(), stderr.text());
    }

    private byte[] decodeBodyData(JSONObject body) {
        String b64 = body.optString("data_b64", "");
        if (!b64.isEmpty()) {
            byte[] data = Base64.decode(b64, Base64.DEFAULT);
            if (data.length > MAX_BODY_BYTES) throw new IllegalArgumentException("entrada grande demais");
            return data;
        }
        String text = body.optString("text", "");
        byte[] data = text.getBytes(StandardCharsets.UTF_8);
        if (data.length > MAX_BODY_BYTES) throw new IllegalArgumentException("entrada grande demais");
        return data;
    }

    private String limitedText(String text) {
        String value = text == null ? "" : text;
        if (value.getBytes(StandardCharsets.UTF_8).length > MAX_TEXT_BYTES) throw new IllegalArgumentException("texto grande demais");
        return value;
    }

    private String cacheKey(JSONObject body) throws Exception {
        String explicit = body.optString("key", body.optString("cache_key", "")).trim();
        if (!explicit.isEmpty()) return sha256(explicit.getBytes(StandardCharsets.UTF_8));
        JSONObject material = new JSONObject();
        material.put("text", body.optString("text", ""));
        material.put("voice", body.optString("voice", ""));
        material.put("locale", body.optString("locale", body.optString("language", "")));
        material.put("rate", body.optString("rate", ""));
        material.put("pitch", body.optString("pitch", ""));
        material.put("engine", body.optString("engine", "android_native"));
        return sha256(material.toString().getBytes(StandardCharsets.UTF_8));
    }

    private File ttsCacheDir() {
        File dir = new File(context.getCacheDir(), "direct-tts-cache");
        if (!dir.exists()) dir.mkdirs();
        return dir;
    }

    private File tempDir(String prefix) {
        File dir = new File(context.getCacheDir(), "direct-" + prefix + "-" + UUID.randomUUID());
        if (!dir.mkdirs()) throw new IllegalStateException("não consegui criar diretório temporário");
        return dir;
    }

    private JSONObject readJson(File file) {
        try { return new JSONObject(new String(readBytes(file, 1024 * 1024), StandardCharsets.UTF_8)); }
        catch (Throwable ignored) { return new JSONObject(); }
    }

    private void trimCache(File dir, int maxFiles, long maxBytes) {
        File[] files = dir.listFiles();
        if (files == null) return;
        List<File> list = new ArrayList<>();
        long total = 0L;
        for (File file : files) if (file.isFile()) { list.add(file); total += file.length(); }
        list.sort(Comparator.comparingLong(File::lastModified));
        while (list.size() > maxFiles || total > maxBytes) {
            File file = list.remove(0);
            total -= file.length();
            file.delete();
        }
    }

    private static String normalizeTask(String value) {
        return (value == null ? "" : value.trim().toLowerCase(Locale.ROOT).replace('-', '_')).replaceAll("[^a-z0-9_]+", "_");
    }

    private String workerId() {
        String value = CoreWorkerRuntimeIdentity.runtimeWorkerId(context);
        if (!value.isEmpty()) return value;
        return "apk-" + prefs.getString("install_id", "unknown").replace("-", "");
    }

    private String normalizedServerUrl() {
        String value = prefs.getString("server_url", "").trim();
        if (value.isEmpty()) value = BuildConfig.CORE_WORKER_VPS_URL == null ? "" : BuildConfig.CORE_WORKER_VPS_URL.trim();
        return value.replaceAll("/+$", "");
    }

    private static String safeName(String value) {
        String clean = value == null ? "" : value.replace('\\', '_').replace('/', '_').replaceAll("[^a-zA-Z0-9._ -]+", "_").trim();
        return clean.isEmpty() ? "file.bin" : limit(clean, 120);
    }

    private static String safeZipPath(String value) {
        String clean = value == null ? "" : value.replace('\\', '/').replaceAll("^/+", "");
        if (isSuspiciousZipPath(clean)) throw new IllegalArgumentException("caminho ZIP suspeito");
        return clean.isEmpty() ? "file.bin" : limit(clean, 180);
    }

    private static boolean isSuspiciousZipPath(String value) {
        String clean = value == null ? "" : value.replace('\\', '/');
        return clean.startsWith("/") || clean.matches("^[A-Za-z]:.*") || clean.contains("../") || clean.equals("..") || clean.indexOf('\0') >= 0;
    }

    private static String safeExtension(String value, String fallback) {
        String clean = value == null ? "" : value.toLowerCase(Locale.ROOT).replaceAll("[^a-z0-9]+", "");
        return clean.isEmpty() ? fallback : limit(clean, 12);
    }

    private static boolean isSafeFfmpegArg(String arg) {
        if (arg == null || arg.isEmpty() || arg.length() > 120) return false;
        if (arg.contains(";") || arg.contains("&&") || arg.contains("||") || arg.contains("`") || arg.contains("\n") || arg.contains("\r")) return false;
        if (arg.startsWith("file:") || arg.startsWith("http:") || arg.startsWith("https:") || arg.startsWith("tcp:") || arg.startsWith("udp:")) return false;
        return arg.matches("[a-zA-Z0-9_+.,:=/@%\\-]+") && !arg.contains("../");
    }

    private static String sha256(byte[] data) throws Exception {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        byte[] hash = digest.digest(data == null ? new byte[0] : data);
        StringBuilder out = new StringBuilder();
        for (byte value : hash) out.append(String.format(Locale.ROOT, "%02x", value & 0xff));
        return out.toString();
    }

    private static void writeBytes(File file, byte[] data) throws Exception {
        File parent = file.getParentFile();
        if (parent != null && !parent.exists()) parent.mkdirs();
        try (FileOutputStream output = new FileOutputStream(file, false)) { output.write(data); output.flush(); }
    }

    private static void writeText(File file, String text) throws Exception {
        writeBytes(file, (text == null ? "" : text).getBytes(StandardCharsets.UTF_8));
    }

    private static byte[] readBytes(File file, int maxBytes) throws Exception {
        if (file == null || !file.isFile()) throw new IllegalArgumentException("arquivo ausente");
        if (file.length() > maxBytes) throw new IllegalArgumentException("arquivo grande demais");
        try (FileInputStream input = new FileInputStream(file); ByteArrayOutputStream output = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[64 * 1024];
            int read;
            while ((read = input.read(buffer)) >= 0) {
                if (read == 0) continue;
                if (output.size() + read > maxBytes) throw new IllegalArgumentException("arquivo grande demais");
                output.write(buffer, 0, read);
            }
            return output.toByteArray();
        }
    }

    private static long directorySize(File file) {
        if (file == null || !file.exists()) return 0L;
        if (file.isFile()) return file.length();
        long total = 0L;
        File[] children = file.listFiles();
        if (children != null) for (File child : children) total += directorySize(child);
        return total;
    }

    private static void deleteTree(File file) {
        if (file == null || !file.exists()) return;
        File[] children = file.listFiles();
        if (children != null) for (File child : children) deleteTree(child);
        file.delete();
    }

    private static int clamp(int value, int min, int max) { return Math.max(min, Math.min(max, value)); }
    private static String firstLine(String value) { String[] lines = (value == null ? "" : value).split("\\R", 2); return lines.length == 0 ? "" : limit(lines[0], 240); }
    private static String limit(String value, int max) { String clean = value == null ? "" : value; return clean.length() <= max ? clean : clean.substring(0, max); }
    private static String shortThrowable(Throwable error) { return error == null ? "erro desconhecido" : limit(error.getClass().getSimpleName() + (error.getMessage() == null ? "" : ": " + error.getMessage()), 240); }
    private static long elapsedMs(long startedNanos) { return Math.max(0L, Math.round((System.nanoTime() - startedNanos) / 1_000_000.0)); }

    private static final class ProcessResult {
        final int exitCode;
        final String stdout;
        final String stderr;
        ProcessResult(int exitCode, String stdout, String stderr) { this.exitCode = exitCode; this.stdout = stdout == null ? "" : stdout; this.stderr = stderr == null ? "" : stderr; }
    }

    private static final class StreamCollector implements Runnable {
        private final InputStream input;
        private final int maxBytes;
        private final ByteArrayOutputStream output = new ByteArrayOutputStream();
        StreamCollector(InputStream input, int maxBytes) { this.input = input; this.maxBytes = Math.max(1024, maxBytes); }
        @Override public void run() {
            try {
                byte[] buffer = new byte[8192];
                int read;
                while ((read = input.read(buffer)) >= 0) {
                    if (read == 0) continue;
                    int allowed = Math.min(read, maxBytes - output.size());
                    if (allowed > 0) output.write(buffer, 0, allowed);
                    // Continua drenando o pipe para o processo não bloquear quando a saída é truncada.
                }
            } catch (Throwable ignored) { }
            try { input.close(); } catch (Throwable ignored) { }
        }
        String text() { return new String(output.toByteArray(), StandardCharsets.UTF_8); }
    }
}
