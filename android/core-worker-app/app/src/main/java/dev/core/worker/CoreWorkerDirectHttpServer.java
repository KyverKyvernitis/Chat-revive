package dev.core.worker;

import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.ByteArrayOutputStream;
import java.io.EOFException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * Servidor HTTP compatível com o antigo phone-worker, executado inteiramente
 * dentro do APK. A API é autenticada e só encaminha tarefas allowlist ao
 * CoreWorkerDirectTaskExecutor; não há endpoint de shell ou comando livre.
 */
final class CoreWorkerDirectHttpServer {
    static final int DEFAULT_PORT = 8766;
    private static final String PREFS = "core_worker_private";
    private static final int MAX_HEADER_BYTES = 32 * 1024;
    private static final int MAX_JSON_BODY_BYTES = 32 * 1024 * 1024;
    private static final int SOCKET_TIMEOUT_MS = 30_000;

    private final Context context;
    private final SharedPreferences prefs;
    private final CoreWorkerDirectTaskExecutor executor;
    private final AtomicBoolean running = new AtomicBoolean(false);
    private final ExecutorService clients = Executors.newFixedThreadPool(3, runnable -> {
        Thread thread = new Thread(runnable, "core-worker-direct-client");
        thread.setDaemon(true);
        return thread;
    });

    private volatile ServerSocket serverSocket;
    private volatile Thread acceptThread;

    CoreWorkerDirectHttpServer(Context context, SharedPreferences prefs, NativeTtsManager tts) {
        this.context = context.getApplicationContext();
        this.prefs = prefs == null ? this.context.getSharedPreferences(PREFS, Context.MODE_PRIVATE) : prefs;
        this.executor = new CoreWorkerDirectTaskExecutor(this.context, this.prefs, tts);
    }

    synchronized void start() throws Exception {
        if (isRunning()) return;
        int requestedPort = normalizePort(CoreWorkerRuntimeIdentity.directHttpPort(context));
        int effectivePort = requestedPort;
        String state = "listening";
        String reason = "";
        ServerSocket socket = null;
        try {
            socket = bind(requestedPort);
        } catch (java.net.BindException conflict) {
            // O APK compartilhado já pede 8767. Para um pareamento dedicado que
            // preferia 8766, conflito é tratado conservadoramente: nunca tomamos
            // a porta de um Termux/processo existente e usamos 8767 explicitamente.
            if (requestedPort != 8766) throw conflict;
            effectivePort = 8767;
            state = "port_conflict_fallback_8767";
            reason = "8766 ocupada; APK preservou o listener existente";
            socket = bind(effectivePort);
        }
        serverSocket = socket;
        running.set(true);
        prefs.edit()
                .putBoolean("direct_http_active", true)
                .putInt("direct_http_requested_port", requestedPort)
                .putInt("direct_http_effective_port", effectivePort)
                .putString("direct_http_state", state)
                .putString("direct_http_error", reason)
                .putLong("direct_http_started_at", System.currentTimeMillis())
                .apply();
        Thread thread = new Thread(this::acceptLoop, "core-worker-direct-http");
        thread.setDaemon(true);
        acceptThread = thread;
        thread.start();
    }

    private static ServerSocket bind(int port) throws Exception {
        ServerSocket socket = new ServerSocket();
        try {
            socket.setReuseAddress(true);
            socket.bind(new InetSocketAddress("0.0.0.0", port), 16);
            return socket;
        } catch (Throwable error) {
            try { socket.close(); } catch (Throwable ignored) { }
            throw error;
        }
    }

    synchronized void stop() {
        running.set(false);
        ServerSocket socket = serverSocket;
        serverSocket = null;
        if (socket != null) {
            try { socket.close(); } catch (Throwable ignored) { }
        }
        Thread thread = acceptThread;
        acceptThread = null;
        if (thread != null) {
            try { thread.interrupt(); } catch (Throwable ignored) { }
        }
        try { clients.shutdownNow(); } catch (Throwable ignored) { }
        prefs.edit()
                .putBoolean("direct_http_active", false)
                .putString("direct_http_state", "stopped")
                .putLong("direct_http_stopped_at", System.currentTimeMillis())
                .apply();
    }

    boolean isRunning() {
        ServerSocket socket = serverSocket;
        return running.get() && socket != null && !socket.isClosed();
    }

    private void acceptLoop() {
        try {
            while (running.get()) {
                ServerSocket socket = serverSocket;
                if (socket == null) break;
                Socket client = socket.accept();
                client.setSoTimeout(SOCKET_TIMEOUT_MS);
                clients.execute(() -> handleClient(client));
            }
        } catch (Throwable error) {
            if (running.get()) recordFailure(error);
        } finally {
            running.set(false);
            prefs.edit().putBoolean("direct_http_active", false).apply();
        }
    }

    private void handleClient(Socket socket) {
        try (Socket client = socket;
             BufferedInputStream input = new BufferedInputStream(client.getInputStream());
             BufferedOutputStream output = new BufferedOutputStream(client.getOutputStream())) {
            HttpRequest request = readRequest(input);
            HttpResponse response = route(request);
            writeResponse(output, response);
        } catch (Throwable ignored) {
            // O cliente pode ter fechado a conexão; isso não derruba o serviço.
        }
    }

    private HttpResponse route(HttpRequest request) {
        try {
            if (!authorized(request.headers)) {
                return json(401, new JSONObject().put("ok", false).put("error", "token inválido ou ausente"));
            }
            if ("GET".equals(request.method)
                    && ("/".equals(request.path) || "/health".equals(request.path) || "/status".equals(request.path))) {
                return json(200, executor.health());
            }
            if ("GET".equals(request.method)
                    && ("/tts-agent/health".equals(request.path) || "/tts-agent/status".equals(request.path))) {
                JSONObject health = executor.health();
                return json(200, new JSONObject()
                        .put("ok", true)
                        .put("worker_id", health.optString("worker_id", ""))
                        .put("version", health.optString("version", ""))
                        .put("tts_agent", health.optJSONObject("tts_agent"))
                        .put("voice_agent", new JSONObject().put("ok", false).put("available", false).put("reason", "voice_agent_vps_local")));
            }
            if ("POST".equals(request.method) && "/task".equals(request.path)) {
                JSONObject body = request.jsonBody();
                String task = body.optString("task", body.optString("type", ""));
                if (!CoreWorkerDirectTaskExecutor.supports(task)) {
                    return json(400, new JSONObject().put("ok", false).put("error", "task não suportada pelo APK"));
                }
                // Compatibilidade com o phone-worker: falhas funcionais continuam em HTTP 200
                // e são representadas por ok=false no JSON. Erros de payload viram 400.
                return json(200, executor.execute(body));
            }
            if ("POST".equals(request.method) && "/tts-agent/synthesize.raw".equals(request.path)) {
                long started = System.nanoTime();
                NativeTtsManager.SynthesisResult result = executor.synthesizeRaw(request.jsonBody());
                Map<String, String> headers = new LinkedHashMap<>();
                headers.put("Content-Type", mimeForAudio(result.audioFormat));
                headers.put("X-Core-Worker-Audio-Format", result.audioFormat);
                headers.put("X-Core-Worker-Sha256", result.sha256);
                headers.put("X-Core-Worker-Engine", "android_native");
                headers.put("X-Core-Worker-Selected-Engine", "android_native");
                headers.put("X-Core-Worker-Cache-Hit", "false");
                headers.put("X-Core-Worker-Android-Synth-Ms", String.valueOf(result.synthMs));
                headers.put("X-Core-Worker-Id", workerId());
                headers.put("X-Core-Worker-Version", BuildConfig.VERSION_NAME);
                headers.put("X-Core-Worker-Worker-Synth-Ms", String.valueOf(result.synthMs));
                headers.put("X-Core-Worker-Worker-Total-Ms", String.valueOf(elapsedMs(started)));
                return new HttpResponse(200, headers, result.data);
            }
            return json(404, new JSONObject().put("ok", false).put("error", "rota não encontrada"));
        } catch (IllegalArgumentException error) {
            return json(400, errorJson(error));
        } catch (Throwable error) {
            return json(500, errorJson(error));
        }
    }

    private boolean authorized(Map<String, String> headers) {
        String registryToken = prefs.getString("worker_token", "").trim();
        String directToken = prefs.getString("direct_http_token", "").trim();
        if (registryToken.isEmpty() && directToken.isEmpty()) return false;

        String supplied = "";
        String authorization = headers.get("authorization");
        if (authorization != null && authorization.regionMatches(true, 0, "Bearer ", 0, 7)) {
            supplied = authorization.substring(7).trim();
        }
        if (supplied.isEmpty()) supplied = safeHeader(headers, "x-phone-worker-token");
        if (supplied.isEmpty()) supplied = safeHeader(headers, "x-core-worker-token");
        return (!directToken.isEmpty() && constantTimeEquals(directToken, supplied))
                || (!registryToken.isEmpty() && constantTimeEquals(registryToken, supplied));
    }

    private static String safeHeader(Map<String, String> headers, String key) {
        String value = headers == null ? null : headers.get(key);
        return value == null ? "" : value.trim();
    }

    private HttpRequest readRequest(InputStream input) throws Exception {
        byte[] rawHeaders = readUntilHeadersEnd(input);
        String headerText = new String(rawHeaders, StandardCharsets.ISO_8859_1);
        String[] lines = headerText.split("\\r?\\n");
        if (lines.length == 0) throw new IllegalArgumentException("requisição HTTP vazia");
        String[] requestLine = lines[0].trim().split("\\s+");
        if (requestLine.length < 2) throw new IllegalArgumentException("linha HTTP inválida");
        String method = requestLine[0].trim().toUpperCase(Locale.ROOT);
        String rawPath = requestLine[1].trim();
        String path = rawPath.split("\\?", 2)[0];
        Map<String, String> headers = new LinkedHashMap<>();
        for (int i = 1; i < lines.length; i++) {
            String line = lines[i];
            int colon = line.indexOf(':');
            if (colon <= 0) continue;
            headers.put(line.substring(0, colon).trim().toLowerCase(Locale.ROOT), line.substring(colon + 1).trim());
        }
        if (headers.containsKey("transfer-encoding")) throw new IllegalArgumentException("Transfer-Encoding não suportado");
        int contentLength = parseContentLength(headers.get("content-length"));
        if (contentLength > MAX_JSON_BODY_BYTES) throw new IllegalArgumentException("corpo grande demais");
        byte[] body = readExact(input, contentLength);
        return new HttpRequest(method, path, headers, body);
    }

    private byte[] readUntilHeadersEnd(InputStream input) throws Exception {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        int matched = 0;
        while (out.size() < MAX_HEADER_BYTES) {
            int value = input.read();
            if (value < 0) throw new EOFException("cabeçalhos HTTP incompletos");
            out.write(value);
            if ((matched == 0 || matched == 2) && value == '\r') matched++;
            else if ((matched == 1 || matched == 3) && value == '\n') matched++;
            else matched = value == '\r' ? 1 : 0;
            if (matched == 4) return out.toByteArray();
        }
        throw new IllegalArgumentException("cabeçalhos HTTP grandes demais");
    }

    private byte[] readExact(InputStream input, int size) throws Exception {
        if (size <= 0) return new byte[0];
        byte[] data = new byte[size];
        int offset = 0;
        while (offset < size) {
            int read = input.read(data, offset, size - offset);
            if (read < 0) throw new EOFException("corpo HTTP incompleto");
            if (read > 0) offset += read;
        }
        return data;
    }

    private void writeResponse(OutputStream output, HttpResponse response) throws Exception {
        byte[] body = response.body == null ? new byte[0] : response.body;
        StringBuilder head = new StringBuilder();
        head.append("HTTP/1.1 ").append(response.status).append(' ').append(statusText(response.status)).append("\r\n");
        head.append("Content-Length: ").append(body.length).append("\r\n");
        head.append("Connection: close\r\n");
        head.append("Cache-Control: no-store\r\n");
        head.append("X-Content-Type-Options: nosniff\r\n");
        for (Map.Entry<String, String> entry : response.headers.entrySet()) {
            head.append(entry.getKey()).append(": ").append(entry.getValue()).append("\r\n");
        }
        head.append("\r\n");
        output.write(head.toString().getBytes(StandardCharsets.ISO_8859_1));
        output.write(body);
        output.flush();
    }

    private HttpResponse json(int status, JSONObject body) {
        Map<String, String> headers = new LinkedHashMap<>();
        headers.put("Content-Type", "application/json; charset=utf-8");
        return new HttpResponse(status, headers, body.toString().getBytes(StandardCharsets.UTF_8));
    }

    private JSONObject errorJson(Throwable error) {
        String message = error == null ? "erro desconhecido" : String.valueOf(error.getMessage());
        if (message == null || message.trim().isEmpty()) message = error == null ? "erro desconhecido" : error.getClass().getSimpleName();
        if (message.length() > 240) message = message.substring(0, 240);
        try {
            return new JSONObject().put("ok", false).put("error", message).put("runtime_mode", "apk-native-direct");
        } catch (Throwable ignored) {
            return new JSONObject();
        }
    }

    private void recordFailure(Throwable error) {
        String message = error == null ? "erro desconhecido" : error.getClass().getSimpleName() + ": " + String.valueOf(error.getMessage());
        if (message.length() > 240) message = message.substring(0, 240);
        prefs.edit()
                .putBoolean("direct_http_active", false)
                .putString("direct_http_error", message)
                .putLong("direct_http_last_failure_at", System.currentTimeMillis())
                .apply();
    }

    private String workerId() {
        String value = CoreWorkerRuntimeIdentity.runtimeWorkerId(context);
        return value.isEmpty() ? "core-worker-apk" : value;
    }

    private static boolean constantTimeEquals(String expected, String supplied) {
        byte[] left = expected == null ? new byte[0] : expected.getBytes(StandardCharsets.UTF_8);
        byte[] right = supplied == null ? new byte[0] : supplied.getBytes(StandardCharsets.UTF_8);
        return MessageDigest.isEqual(left, right);
    }

    private static int parseContentLength(String raw) {
        if (raw == null || raw.trim().isEmpty()) return 0;
        try {
            int value = Integer.parseInt(raw.trim());
            if (value < 0) throw new NumberFormatException();
            return value;
        } catch (Throwable ignored) {
            throw new IllegalArgumentException("Content-Length inválido");
        }
    }

    private static int normalizePort(int port) {
        return port >= 1024 && port <= 65535 ? port : DEFAULT_PORT;
    }

    private static String mimeForAudio(String format) {
        String normalized = format == null ? "wav" : format.trim().toLowerCase(Locale.ROOT);
        if (normalized.equals("mp3")) return "audio/mpeg";
        if (normalized.equals("ogg") || normalized.equals("opus")) return "audio/ogg";
        return "audio/wav";
    }

    private static long elapsedMs(long startedNanos) {
        return Math.max(0L, Math.round((System.nanoTime() - startedNanos) / 1_000_000.0));
    }

    private static String statusText(int status) {
        if (status == 200) return "OK";
        if (status == 400) return "Bad Request";
        if (status == 401) return "Unauthorized";
        if (status == 404) return "Not Found";
        if (status == 413) return "Payload Too Large";
        if (status == 422) return "Unprocessable Entity";
        return "Internal Server Error";
    }

    private static final class HttpRequest {
        final String method;
        final String path;
        final Map<String, String> headers;
        final byte[] body;

        HttpRequest(String method, String path, Map<String, String> headers, byte[] body) {
            this.method = method;
            this.path = path;
            this.headers = headers;
            this.body = body == null ? new byte[0] : body;
        }

        JSONObject jsonBody() throws Exception {
            if (body.length == 0) return new JSONObject();
            return new JSONObject(new String(body, StandardCharsets.UTF_8));
        }
    }

    private static final class HttpResponse {
        final int status;
        final Map<String, String> headers;
        final byte[] body;

        HttpResponse(int status, Map<String, String> headers, byte[] body) {
            this.status = status;
            this.headers = headers == null ? new LinkedHashMap<>() : headers;
            this.body = body == null ? new byte[0] : body;
        }
    }
}
